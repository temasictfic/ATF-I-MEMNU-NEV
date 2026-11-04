# Citation NER training pipeline

Double fine-tunes `SIRIS-Lab/citation-parser-ENTITY` on the public SIRIS corpus plus a focused set of non-APA sources labeled from the app's own parse cache, then exports the result to INT8 ONNX for fast CPU inference in the desktop backend.

Why non-APA only: SIRIS's reported F1 is 0.99 on TITLE and 0.95 on AUTHORS because its training set is APA-heavy. Labeling more APA examples would be wasted effort. Non-APA formats (IEEE, Vancouver, Chicago notes, book chapters, informal Turkish styles) are where it loses points, and that is exactly what we target.

## Prerequisites

- Python 3.12, from the repo root: `python -m venv backend/training/.venv && backend/training/.venv/Scripts/activate`
- `pip install -r backend/training/requirements.txt`
- Google/Kaggle account for the Colab training step
- The backend package importable on `PYTHONPATH` (the scripts import `backend.utils.text_cleaning` and `backend.services.ner_extractor`). From the repo root, everything works as long as you run scripts with `python -m backend.training.scripts.<name>`.

## Step-by-step runbook

### 1. Drop cached parse JSONs into the input directory

```
backend/training/data/kaynaklar/input/<pdf_name>.json
```

Each file is the app's per-PDF parse cache. The filter script reads whatever shape it finds as long as each source has a `raw_text` field — it duck-types common wrappers (`sources`, `sources`, top-level list).

### 2. Filter candidates

```
python -m backend.training.scripts.filter_candidates
```

Applies `strip_source_noise` (from `backend/utils/text_cleaning.py` — the same function the live app uses) to every source, buckets each stripped text as `apa_like` or `non_apa`, and writes:
- `data/kaynaklar/to_label.jsonl` — non-APA candidates for labeling
- `data/kaynaklar/skipped_apa.jsonl` — APA-bucket sources, for your audit

Sanity-check the split by eyeballing 10 random lines from each file. If APA is eating non-APA cases, tighten the regex in `filter_candidates.py` and rerun.

### 3. Label the non-APA candidates (Claude)

In a Claude Code session, ask Claude to label `to_label.jsonl` in batches of 20. Claude appends to `data/kaynaklar/labeled.jsonl` and runs the validator after each batch so malformed spans are corrected before they accumulate.

### 4. Prepare the merged dataset

```
python -m backend.training.scripts.prepare_data
```

Downloads the public SIRIS dataset, converts the kaynaklar span-format labels to BIO using the model tokenizer, splits kaynaklar 70/15/15 stratified by `source_pdf`, and writes an HF `DatasetDict` to `data/merged/`. The kaynaklar test split never appears in training.

Dry run (public only, no kaynaklar needed): `python -m backend.training.scripts.prepare_data --skip-kaynaklar`

### 5. Train on Colab

Upload `notebooks/train_citation_ner.ipynb` to Colab with the T4 runtime. The notebook expects `data/merged/` to be uploaded alongside (via Drive or direct upload). It runs a conservative continued fine-tune (lr 2e-5, 5 epochs, early stopping on eval F1) and saves the best checkpoint.

Download `models/finetuned/` back to `backend/training/models/finetuned/` locally.

### 6. Export and quantize

```
python -m backend.training.scripts.export_onnx
```

Produces `models/finetuned-onnx/` (fp32) and `models/finetuned-onnx-int8/` (dynamic INT8, the production target).

### 7. Benchmark head-to-head against SIRIS baseline

```
python -m backend.training.scripts.evaluate
python -m backend.training.scripts.bench_latency
```

Both scripts append to the same markdown report under `reports/eval_<timestamp>.md`. The report compares three models: upstream `SIRIS-Lab/citation-parser-ENTITY`, our fine-tuned fp32 checkpoint, and our fine-tuned INT8 ONNX model, on both kaynaklar and public test sets. The INT8 model is the one that matters — if it beats the baseline on kaynaklar F1 while staying within 2 points on public F1 and running at least 1.5× faster, the training run was a success and the model is ready to bundle into the app.

## Layout

```
backend/training/
  data/
    kaynaklar/input/        <- you drop cached JSONs here
    kaynaklar/to_label.jsonl
    kaynaklar/labeled.jsonl
    public/                 <- HF cache
    merged/                 <- HF DatasetDict produced by prepare_data
  models/
    baseline/               <- pinned copy of upstream SIRIS
    finetuned/              <- HF checkpoint from Colab
    finetuned-onnx/         <- fp32 ONNX
    finetuned-onnx-int8/    <- INT8 ONNX, production target
  notebooks/
    train_citation_ner.ipynb
  scripts/
    filter_candidates.py
    validate_labels.py
    prepare_data.py
    export_onnx.py
    evaluate.py
    bench_latency.py
  reports/
    eval_<timestamp>.md
```

## Label schema

14 canonical entities from `SIRIS-Lab/citation-parser-ENTITY`. Do not invent new labels — the validator rejects anything else.

```
TITLE, AUTHORS, PUBLICATION_YEAR, JOURNAL, DOI, ISBN,
LOCATION, LINK_ONLINE_AVAILABILITY, ISSN, PUBLISHER,
PAGE_FIRST, PAGE_LAST, ISSUE, VOLUME
```

Span format (end-exclusive offsets into the pre-stripped `text`):

```json
{"id":"126E146_ref_001","source_pdf":"126E146.pdf","text":"Smith J, Jones K. Title. Lancet 2020;395:1014-28.","entities":[{"start":0,"end":17,"label":"AUTHORS"},{"start":19,"end":24,"label":"TITLE"},{"start":26,"end":32,"label":"JOURNAL"},{"start":33,"end":37,"label":"PUBLICATION_YEAR"},{"start":38,"end":41,"label":"VOLUME"},{"start":42,"end":46,"label":"PAGE_FIRST"},{"start":47,"end":49,"label":"PAGE_LAST"}]}
```
