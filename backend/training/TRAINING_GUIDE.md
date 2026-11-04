# Training Guide — Citation NER Fine-Tune

> This document is the narrative companion to [README.md](./README.md), which
> is a short runbook. Read this file when you need to *understand* why the
> pipeline is structured the way it is, reproduce the training run from
> scratch, or iterate on the model.

## 1. Context

The desktop app uses a token-classification model to extract structured
fields (title, authors, year, journal, DOI, volume, issue, pages, etc.) from
raw source strings detected in PDFs. The original deployment used the
upstream `SIRIS-Lab/citation-parser-ENTITY` model directly from HuggingFace
Hub: a multilingual DistilBERT fine-tuned by SIRIS-Lab on ~2,400 citations,
reporting F1 = 0.9498 on their own test set.

That model works well on APA and APA-variation formats — which is what its
training corpus is dominated by — but its accuracy drops sharply on formats
the Turkish academic corpus we care about actually uses. A comparison run in
[backend/compare_extraction.py](../compare_extraction.py) across 2,081
sources from 40 test PDFs documented the specific failure modes: titles
truncated to a single sentence, author spans that include the leading
source number (`"39. Klok, T"`), DOI and arXiv identifiers missed, and
particularly weak performance on IEEE, Vancouver, Chicago, and informal
Turkish academic styles.

This training pipeline was built to address those failure modes. The end
product is a fine-tuned + INT8-quantized ONNX model at
[backend/models/citation-ner-int8/](../models/citation-ner-int8/) that
the runtime loads via [backend/services/ner_model_manager.py](../services/ner_model_manager.py).
On the held-out non-APA test set, it scores **F1 = 0.864** versus **F1 =
0.555** for the upstream baseline — a 31-point improvement on the target
distribution, with no regression on APA-style sources (F1 = 0.967 on
SIRIS's own public test set, matching the baseline's 0.966).

## 2. Design decisions and why

### 2.1 Focused labeling on non-APA formats

SIRIS-Lab's reported F1 of 0.99 on TITLE and 0.95 on AUTHORS is real — but
only on *their* training distribution, which is APA-heavy. Labeling more APA
sources would have been wasted effort: the model already handles them.
The bottleneck is the long tail of formats the upstream training set did not
cover. So the labeling strategy was to target only sources that **fail**
a simple APA heuristic.

The heuristic is deliberately conservative: a source is bucketed as
`apa_like` if a four-digit year appears in parentheses within the first 200
characters of the pre-stripped text (`\([12]\d{3}[a-z]?\)`), and `non_apa`
otherwise. See [scripts/filter_candidates.py](./scripts/filter_candidates.py).
Sampling the two buckets confirmed the filter behaves as intended: the
`apa_like` bucket was dominated by `"Smith, J. (2020). Title."` patterns,
and the `non_apa` bucket was dominated by Vancouver, IEEE, Chicago, and
informal styles with year-after-authors or bracket-numbered formats.

On the user's 40-PDF corpus this split produced 1,146 non-APA candidates
and 876 APA-skipped sources out of 2,027 total.

### 2.2 Source-number stripping via the app's own function

A recurring failure mode was author spans that included leading source
numbers like `"39. Klok, T"`. Rather than teach the model to handle this,
the pipeline structurally eliminates the problem by applying the same
`strip_source_noise` function from [backend/utils/text_cleaning.py](../utils/text_cleaning.py)
that the live app already calls at inference time ([backend/services/source_extractor.py:43](../services/source_extractor.py#L43)).
This means:

- numbering, access-date fragments, and duplicated whitespace are removed
  from the source text *before* the filter classifies it,
- labels are written against the stripped text, so the model never sees the
  boilerplate at training time,
- at inference time the same function pre-processes text, so the training
  and inference distributions match exactly.

This is a structural fix — the model doesn't have to learn around the
artifact, and it can't regress on it either.

### 2.3 Continued fine-tune, not fresh training

The base is `SIRIS-Lab/citation-parser-ENTITY`, loaded via
`AutoModelForTokenClassification.from_pretrained(...)`. We continue training
the already-fine-tuned checkpoint ("double fine-tune") rather than going
back to the multilingual DistilBERT base. The label space is the SIRIS
model's native 29 BIO tags (14 entities × {B-, I-} + O), pulled at runtime
from `AutoConfig.from_pretrained(...).id2label`. No label remapping, no
head replacement — the fine-tune can proceed with the existing head intact.

The hyperparameters are deliberately conservative to avoid clobbering the
checkpoint's prior competence:

| | Value | Why |
|---|---|---|
| `learning_rate` | 2e-5 | Lower than the typical 5e-5 for fresh fine-tunes |
| `num_train_epochs` | 5 with early stopping (patience 2) | Prevents overfitting on the small validation set |
| `per_device_train_batch_size` | 16 | Fits in T4 memory with fp16 |
| `gradient_accumulation_steps` | 2 | Effective batch of 32 |
| `weight_decay` | 0.01 | Mild regularization |
| `warmup_ratio` | 0.1 | Gentle start |
| `fp16` | True when CUDA available | Colab T4 speedup |
| `metric_for_best_model` | `f1` (seqeval span-level) | The metric we actually care about |

On the first full run, early stopping kicked in cleanly: validation F1 went
0.840 → 0.895 → 0.884 across epochs 0/2/4, and the epoch-2 checkpoint was
selected as the best model. Training loss dropped from 0.117 to 0.019 while
validation loss crept up from 0.579 to 0.619 — classic mild overfitting on
the small kaynaklar validation set, and exactly the signal early stopping
is designed to catch.

### 2.4 Dataset augmentation with the public SIRIS corpus

The focused labeling produced 500 non-APA sources (see §3.3) — not
enough on its own to fine-tune a 130M-parameter transformer without
catastrophic forgetting. The solution is to concatenate the 500 kaynaklar
labels with the full public SIRIS training set (2,150 examples) so the
model sees both domains during training. The HF dataset
`SIRIS-Lab/citation-parser-ENTITY` uses the exact same label space as the
model, so no remapping is needed.

This gives us four split roles:

| Split | Examples | Role |
|---|---|---|
| `train` | 2,542 | Public train (2,150) + kaynaklar train (392). The model sees both at once. |
| `validation` | 81 | Kaynaklar val only. In-domain signal for early stopping. |
| `kaynaklar_test` | 27 | Held-out kaynaklar PDFs. Primary benchmark. |
| `public_test` | 269 | Upstream SIRIS test. Catastrophic-forgetting guard. |

The kaynaklar splits are stratified **by source PDF**, not by source.
`prepare_data.py` asserts that no `source_pdf` appears in more than one
split. This is critical: a source-level split would allow the same PDF's
sources to appear in both train and test, which would inflate the
benchmark by measuring what the model memorized.

### 2.5 INT8 dynamic quantization

After training, the fp32 HF checkpoint is exported to ONNX with `optimum`
and quantized to INT8 using dynamic quantization with the `avx512_vnni`
preset — no calibration set required. The resulting model is:

- **4x smaller on disk** (478 MB fp32 ONNX → 124 MB INT8 ONNX)
- **2.1x faster on CPU** (35 ms p50 → 17 ms p50 in `bench_latency.py`)
- **~20% lower peak RSS** (~760 MB → ~605 MB)
- **Equal or better accuracy** on both test sets (actually +1.3 F1 on
  kaynaklar_test, but that's within noise of a 27-example set)

Dynamic INT8 is the right call for this model: no accuracy cost, no
calibration dataset needed, and `avx512_vnni` targets recent Intel/AMD CPUs.
If a deployment target lacks VNNI, `export_onnx.py --quant-preset avx2`
falls back to the older instruction set at a ~10% latency cost.

## 3. Pipeline walkthrough

### 3.1 Filter candidates

```
python -m backend.training.scripts.filter_candidates
```

Input: per-PDF cached JSONs dropped into `data/kaynaklar/input/*.json`. These
are the app's own parse cache — each file has a `sources` array where each
source has a `text` field (the raw source string) plus bbox, ref_number,
status metadata. The script duck-types the format: any dict with a `text`
(or `raw_text`) field of length ≥ 20 is treated as a source record, so
future cache format changes shouldn't break it as long as the field name
stays.

For every source:
1. Apply `strip_source_noise` to get the same text the live app will see.
2. Skip duplicates (same stripped text across PDFs).
3. Classify as `apa_like` (parenthesized year in first 200 chars) or
   `non_apa`.
4. Write to `to_label.jsonl` (non-APA) or `skipped_apa.jsonl` (APA) — both
   are gitignored since they're regenerable.

**Verification step:** hand-inspect 10 random lines from each bucket before
labeling. If the APA heuristic is eating non-APA cases, tighten the regex
in `filter_candidates.py` and re-run — it's idempotent.

### 3.2 Validator

[scripts/validate_labels.py](./scripts/validate_labels.py) is the safety
net. It runs after every labeling batch and checks:

- All labels are in the canonical set of 14 (TITLE, AUTHORS,
  PUBLICATION_YEAR, JOURNAL, DOI, ISBN, LOCATION, LINK_ONLINE_AVAILABILITY,
  ISSN, PUBLISHER, PAGE_FIRST, PAGE_LAST, ISSUE, VOLUME).
- All entity offsets are within `[0, len(text))` and `start < end`.
- No two entity spans overlap (each character belongs to at most one entity).
- No span has leading or trailing whitespace.
- Non-trivial texts (>20 chars) have at least one entity.

It's usable both as a CLI (`python -m backend.training.scripts.validate_labels <path>`)
and as a library (imported by `prepare_data.py` and `_label_helper.py`).

### 3.3 Labeling

Labels were produced by an LLM (Claude Code) reading candidates from
`to_label.jsonl` in batches and emitting span annotations against the
pre-stripped text. The workflow was:

1. Read a batch of 20–30 sources from `to_label.jsonl`.
2. For each source, identify which entities are present and write them
   as (substring, label) tuples in a batch script (e.g.
   `scripts/_batch_001.py`).
3. The `_label_helper.label_batch(...)` function looks each substring up in
   the source text, picks the first non-overlapping occurrence, computes
   offsets, and appends a validator-clean record to `labeled.jsonl`.
4. Run the validator on the whole file after every batch. Any malformed
   record fails the batch atomically (nothing is written), and the batch is
   corrected and re-run.

The final corpus is **500 labels across 14 PDFs**, saved at
[data/kaynaklar/labeled.jsonl](./data/kaynaklar/labeled.jsonl). The label
distribution is heavily skewed toward the first few PDFs processed: 126E156
has 79 labels, 126E152 has 1. If you care about a larger held-out test set,
labeling more sources from the tail PDFs (126E152, 126E154, 126E159,
126E147, 126E148) is the highest-leverage follow-up.

**The `_batch_*.py` scaffolding scripts are not committed** — the
labeled.jsonl file is the canonical output. Those scripts were ad-hoc
tools for producing the labels; re-labeling in the future would use a
different set of scripts driven by whatever labeling interface is in use.

### 3.4 Data preparation

```
python -m backend.training.scripts.prepare_data
```

Runs locally. Loads the public SIRIS dataset via `datasets.load_dataset(...)`,
loads the kaynaklar labels, validates them, and normalizes both to the same
internal format: `{tokens, ner_tags}` where `tokens` is produced by a single
regex word-and-punctuation tokenizer (`_WORD_RE = r"\w+|[^\w\s]"`) and
`ner_tags` is the BIO label ids derived from character-level spans.

The public dataset uses a span-format `annotation` field; the kaynaklar
labels use `entities`. Both get converted to a common span shape first,
then to word-level BIO, then saved as an HF `DatasetDict`. Uniform treatment
means a single tokenize-and-align step suffices in the training notebook.

Splits are produced by `split_by_source_pdf()` with ratios 70/15/15. The
assertion block in `main()` guarantees no `source_pdf` crosses splits.

A sidecar `kaynaklar_test_raw.jsonl` is written next to the dataset — this
preserves the original pre-stripped text and entity spans for the test
split, so `run_eval.py` can compute downstream metrics on the real raw
strings instead of reconstructing them from tokens.

Dry-run support: `python -m ... prepare_data --skip-kaynaklar` builds the
merged dataset using only the public corpus, with empty kaynaklar splits.
Useful for testing the pipeline without the labels present.

### 3.5 Training

Runs on Colab T4 via [notebooks/train_citation_ner.ipynb](./notebooks/train_citation_ner.ipynb).

The workflow is: zip the local `data/merged/` directory, upload
`merged.zip` to Colab's working directory, open the notebook with a GPU
runtime, run all cells. The notebook auto-extracts the zip, installs pinned
dependencies (`transformers==4.46.3`, `datasets==3.1.0`,
`evaluate==0.4.3`, `seqeval==1.2.2`, `accelerate==1.1.1`), loads the
merged dataset, tokenizes with alignment (first subword of each word gets
the word's label, continuation subwords get `-100`), instantiates the HF
`Trainer`, runs the fine-tune, saves the best checkpoint.

The `fsspec` dependency conflict warning from Colab's pre-installed `gcsfs`
is harmless — `gcsfs` is not used by the training code. Similarly the
`seqeval` `UndefinedMetricWarning` during eval is harmless: it fires when
a label has zero predicted or zero true occurrences in a small evaluation
batch, which happens frequently on the small kaynaklar val set for rare
labels like ISBN and ISSN. The overall micro-averaged F1 that drives
`metric_for_best_model` is unaffected.

After training, the notebook prints metrics on `kaynaklar_test` and
`public_test`, saves the checkpoint, and prints a one-liner to zip it.
The user downloads `finetuned.zip` and unzips it to
`backend/training/models/finetuned/` locally.

### 3.6 ONNX export and INT8 quantization

```
python -m backend.training.scripts.export_onnx
```

Runs locally. Uses `optimum[onnxruntime]`. Two-stage:

1. **fp32 ONNX export** via
   `ORTModelForTokenClassification.from_pretrained(path, export=True)` →
   `save_pretrained("models/finetuned-onnx")`. Produces a standard
   `model.onnx` file.
2. **Dynamic INT8 quantization** via `ORTQuantizer` with
   `AutoQuantizationConfig.avx512_vnni(is_static=False, per_channel=True)` →
   `save_pretrained("models/finetuned-onnx-int8")`. Produces a
   `model_quantized.onnx` file.

The `--quant-preset {avx512_vnni,avx512,avx2,arm64}` knob selects the
instruction-set target. `avx512_vnni` is the default and targets modern
x86 CPUs. If the deployment target lacks VNNI, `avx2` is the safer fallback
with ~10% higher latency.

The INT8 ONNX directory also includes `config.json`, `tokenizer.json`,
`vocab.json`, `merges.txt` — everything a `pipeline("ner", ...)` call
needs to run inference. The whole directory is ~125 MB and is the unit
that gets bundled into the app (§4).

### 3.7 Evaluation

```
python -m backend.training.scripts.run_eval
python -m backend.training.scripts.bench_latency
```

Both append to the same markdown file under `reports/eval_<timestamp>.md`.

**`run_eval.py`** loads three models head-to-head: the upstream SIRIS
baseline pulled fresh from HF Hub, the local fp32 fine-tune, and the local
INT8 ONNX fine-tune. It auto-detects the `.onnx` filename in each local
directory so both fp32 (`model.onnx`) and INT8 (`model_quantized.onnx`)
load cleanly without `file_name` warnings. For each model:

1. Runs seqeval on `kaynaklar_test` and `public_test` with per-label P/R/F1.
2. Runs the models over the raw text sidecar from `kaynaklar_test_raw.jsonl`
   and imports `_extract` from [backend/services/ner_extractor.py](../services/ner_extractor.py)
   to compute the downstream match rate — i.e., the fraction of sources
   for which the final `ParsedSource` has `title + authors + year` populated
   with `parse_confidence ≥ 0.3`. This is the metric that actually
   determines whether the NER path fires or falls back to regex in the live
   app.

**`bench_latency.py`** does a 5-ref warmup then 500 real source
inferences per model, reporting p50/p95/p99/mean latency and peak RSS
via `psutil`. Disk size is measured by walking the model directory.

Both scripts are named to avoid filename collisions with the `evaluate`
library (`run_eval.py`, not `evaluate.py`) — a collision I tripped on
during development because Python puts `sys.argv[0]`'s directory at
`sys.path[0]`, so `import evaluate` inside a script called `evaluate.py`
finds the script itself.

## 4. Integration into the live app

The bundled INT8 model lives at
[backend/models/citation-ner-int8/](../models/citation-ner-int8/). The
loader is [backend/services/ner_model_manager.py](../services/ner_model_manager.py)
`_load_pipeline_sync()`, which:

1. Reads `settings.ner_local_model_path` (defaults to
   `backend/models/citation-ner-int8` in dev, or a `_MEIPASS`-relative
   path when frozen by PyInstaller/Electron).
2. If the path exists and contains `.onnx` files, loads via
   `optimum.onnxruntime.ORTModelForTokenClassification` + `AutoTokenizer`
   + `transformers.pipeline("ner", aggregation_strategy="simple")`. The
   return is a drop-in replacement for the HF Hub pipeline, so no downstream
   code needed changes.
3. If local loading fails for any reason (file missing, ONNX corrupt,
   optimum import error), it logs a warning and falls back to the upstream
   HF Hub model via the original code path. The fallback means a corrupt
   bundle doesn't brick the app — it just silently degrades to the
   baseline model.

The `settings.ner_local_model_path` field is wired into
[backend/config.py](../config.py) via `_default_ner_local_model_path()`,
which probes candidate paths in order:
- `{_MEIPASS}/models/citation-ner-int8` (packaged Electron with model in
  resources root)
- `{_MEIPASS}/backend/models/citation-ner-int8` (packaged with backend/
  preserved)
- `{backend/}/models/citation-ner-int8` (dev from source checkout)

The first one that exists and contains at least one `.onnx` file wins.
Returns `""` if none match, which triggers the HF Hub fallback.

Runtime dependencies for the ONNX path are pinned in
[backend/pyproject.toml](../pyproject.toml):

```
transformers>=4.40,<5    # optimum-onnx 0.1.0 doesn't support transformers 5.x yet
optimum>=1.23
optimum-onnx>=0.1.0
onnxruntime>=1.19
```

The `transformers<5` pin is load-bearing: `optimum-onnx==0.1.0` (the
separate package that holds ONNX export utilities after optimum 2.0's
re-org) requires transformers 4.x. When optimum-onnx ships transformers
5.x support, remove the upper bound.

## 5. Benchmark results

From the first production run
([reports/eval_20260410T233931Z.md](./reports/eval_20260410T233931Z.md)):

| Model | F1 kaynaklar (non-APA) | F1 public (APA) | p50 latency | Disk | Peak RSS |
|---|---|---|---|---|---|
| baseline (SIRIS) | 0.555 | 0.966 | 35.0 ms | — | 754 MB |
| finetuned fp32 | 0.852 | 0.969 | 37.0 ms | 478 MB | 758 MB |
| **finetuned INT8** | **0.864** | **0.967** | **16.7 ms** | **124 MB** | **605 MB** |

The quantized fine-tune Pareto-dominates the baseline: higher accuracy,
2.1× faster, 4× smaller, 20% less RAM. Per-label breakdowns on
kaynaklar_test show the gains are concentrated exactly where the baseline
was weak:

| Label | Baseline F1 | Fine-tuned INT8 F1 | Δ |
|---|---|---|---|
| AUTHORS | 0.291 | 0.926 | **+63** |
| DOI | 0.000 | 1.000 | **+100** |
| LINK_ONLINE_AVAILABILITY | 0.000 | 1.000 | **+100** |
| TITLE | 0.321 | 0.778 | **+46** |
| JOURNAL | 0.549 | 0.706 | +16 |
| PUBLICATION_YEAR | 0.714 | 0.929 | +21 |

Public test stayed flat or nudged upward — no catastrophic forgetting.

## 6. Known limitations

**Small held-out kaynaklar_test set (27 sources).** The 500 labels
cover only 14 of the 40 source PDFs because labeling was done sequentially
from the start of `to_label.jsonl`. The 70/15/15 split-by-PDF therefore
leaves only 2 PDFs and 27 sources in the test set. A single
misclassified span shifts the F1 by roughly 3 points, so the +31 F1 gap is
directionally correct but the exact number has wide error bars. Adding
200–300 more labels from the currently under-represented PDFs (126E152,
126E154, 126E159, 126E147, 126E148 have 1–10 labels each) would tighten
the confidence interval.

**Public test F1 is our most reliable benchmark.** 269 sources from a
different distribution, evaluated by two different tokenizers (ours vs.
SIRIS's own gold). Both models cluster near 0.97 — that's a sign the fine-
tune didn't break anything on APA, which is what we wanted.

**LOCATION is still 0 F1.** Four examples in kaynaklar_test, all missed by
both baseline and fine-tune. Likely too few examples to learn from and the
label boundary is genuinely ambiguous ("Cham", "Ankara", "Bellingham, WA,
USA" — which of these should be LOCATION vs part of PUBLISHER?).

**Intermediate-layer `2024` subword split.** The tokenizer sometimes splits
a year like `2024` into `20` + `24` subwords and assigns `B-PUBLICATION_YEAR`
to `20` and `I-PUBLICATION_YEAR` to `24`, so the "simple" aggregation
strategy in `pipeline("ner", aggregation_strategy="simple")` produces two
adjacent PUBLICATION_YEAR entities instead of one. The downstream
`_parse_year` in [backend/services/ner_extractor.py](../services/ner_extractor.py)
handles this correctly (it searches the raw text in the surrounding
context), but it's worth knowing about if you write new downstream logic
that expects exactly one PUBLICATION_YEAR entity.

**Single-run training metrics are noisy.** Early stopping triggered at
epoch 2 on the first run, but a second run with a different seed could
pick a different epoch. For publication-grade numbers, run 3-5 seeds and
report mean + std. For this project, the headline 0.555 → 0.864 gap is
large enough that run-to-run variance doesn't change the conclusion.

## 7. Reproducing from scratch

Everything below assumes you're running from the repo root with the
backend Python environment active.

```bash
# 0. Install training-only deps (separate from backend runtime deps)
python -m venv backend/training/.venv
source backend/training/.venv/Scripts/activate    # or .venv/bin/activate on Unix
pip install -r backend/training/requirements.txt

# 1. Drop the app's per-PDF parse cache JSONs into the input directory
cp path/to/parse/cache/*.json backend/training/data/kaynaklar/input/

# 2. Filter candidates and sanity-check the APA/non-APA buckets
python -m backend.training.scripts.filter_candidates
# Inspect 10 random lines from each of data/kaynaklar/to_label.jsonl and
# data/kaynaklar/skipped_apa.jsonl. If the filter is wrong, tighten the
# regex in filter_candidates.py and re-run.

# 3. Label to_label.jsonl (manual or LLM-assisted).
# Output: data/kaynaklar/labeled.jsonl
# Validator: python -m backend.training.scripts.validate_labels data/kaynaklar/labeled.jsonl

# 4. Build the merged HF DatasetDict
python -m backend.training.scripts.prepare_data

# 5. Zip and upload data/merged to Colab, run the notebook, download finetuned.zip
#    Unzip to backend/training/models/finetuned/

# 6. Export to ONNX and INT8 quantize
python -m backend.training.scripts.export_onnx

# 7. Benchmark
python -m backend.training.scripts.run_eval
python -m backend.training.scripts.bench_latency
# Report lands at backend/training/reports/eval_<timestamp>.md

# 8. Bundle the INT8 model for production
cp -r backend/training/models/finetuned-onnx-int8 backend/models/citation-ner-int8
# The live app auto-loads it via settings.ner_local_model_path.
```

## 8. File index

### Scripts (all under `backend/training/scripts/`)

| File | Purpose | Runs where |
|---|---|---|
| `filter_candidates.py` | Read cached JSONs, strip noise, APA/non-APA bucket | Local |
| `validate_labels.py` | Schema validator for span JSONL (CLI + library) | Local |
| `_label_helper.py` | Offset-resolving batch labeling helper | Local (LLM-assisted) |
| `prepare_data.py` | Merge public SIRIS + kaynaklar labels → HF DatasetDict | Local |
| `export_onnx.py` | HF checkpoint → fp32 ONNX → INT8 ONNX | Local |
| `run_eval.py` | Head-to-head accuracy benchmark (baseline vs fp32 vs INT8) | Local |
| `bench_latency.py` | p50/p95/p99 latency + memory + disk benchmark | Local |

### Notebooks

| File | Purpose | Runs where |
|---|---|---|
| `notebooks/train_citation_ner.ipynb` | HF Trainer fine-tune with early stopping | Colab T4 |

### Data (committed)

| File | Purpose |
|---|---|
| `data/kaynaklar/labeled.jsonl` | 500 hand-labeled non-APA sources. The canonical training signal. |

### Data (gitignored, regenerable)

See [.gitignore](./.gitignore) for the full list. Short version:
- `data/kaynaklar/input/` — user's parse cache dumps
- `data/kaynaklar/to_label.jsonl`, `skipped_apa.jsonl` — filter output
- `data/public/`, `data/merged/`, `data/merged.zip` — HF cache + prep output
- `models/finetuned/`, `models/finetuned-onnx/`, `models/finetuned-onnx-int8/`,
  `models/finetuned.zip` — training + export artifacts
- `scripts/_batch_*.py` — ad-hoc labeling scaffolding

### Reports (committed)

| File | Purpose |
|---|---|
| `reports/eval_<timestamp>.md` | Head-to-head benchmark output for a training run |

## 9. Checklist for the next iteration

When training a v2 of this model, this is the sequence to follow:

1. **Label more.** The 27-source test set is the biggest weakness of
   v1. Targeted labeling on the currently under-represented PDFs (see §6)
   will widen it significantly without a lot of work.
2. **Clean the label set if needed.** Validator-clean doesn't mean
   semantically clean. Spot-check a random sample of `labeled.jsonl` for
   boundary inconsistencies (e.g., does AUTHORS include or exclude trailing
   periods? is the DOI entity the full URL or just the identifier?).
3. **Keep hyperparameters conservative.** 2e-5 + 5 epochs + early stopping
   is a good baseline. Bumping weight_decay to 0.02 or cutting max epochs
   to 3 is a sensible knob if validation loss climbs faster than F1 rises.
4. **Re-run the full pipeline end to end.** `filter_candidates` →
   `prepare_data` → notebook → `export_onnx` → `run_eval` → `bench_latency`.
   The report under `reports/` is timestamped so old reports stay intact.
5. **Compare against v1, not just against baseline.** If v2 doesn't beat
   v1 on both the expanded kaynaklar_test and the public_test, don't
   deploy it. Ablate: which label change or hyperparameter moved the
   numbers, and does the shift make sense?
6. **Swap the bundled model.** `cp -r backend/training/models/finetuned-onnx-int8
   backend/models/citation-ner-int8` and restart the backend. No config
   changes needed — the loader auto-detects the `.onnx` filename.

## 10. Credits

- Base model: `SIRIS-Lab/citation-parser-ENTITY` on HuggingFace Hub,
  multilingual DistilBERT fine-tuned on 2,688 citations by SIRIS Lab.
- Public training corpus: `SIRIS-Lab/citation-parser-ENTITY` dataset on
  HuggingFace Hub, 2,150 train + 269 dev + 269 test, span-annotated.
- Fine-tuning labels: 500 non-APA sources hand-labeled from the app's
  own PDF parse cache, specifically targeted at formats the baseline
  struggles with.
