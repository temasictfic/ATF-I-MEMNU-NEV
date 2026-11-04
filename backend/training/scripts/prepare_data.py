"""Merge the public SIRIS citation NER dataset with the kaynaklar span labels
into a single HF DatasetDict ready for Trainer.

Both datasets are span-format (text + character-offset entity spans). This
script normalizes them to word-level BIO tokens using a single regex word
tokenizer, so the training notebook only needs one tokenize-and-align step.

Output layout under `data/merged/`:
  - train          : public_train + kaynaklar_train
  - validation     : kaynaklar_val (what we care about: in-domain non-APA)
  - kaynaklar_test : held-out kaynaklar, primary benchmark set
  - public_test    : upstream SIRIS test set, catastrophic-forgetting guard
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

try:
    from backend.training.scripts.validate_labels import (
        CANONICAL_LABELS,
        ValidationError,
        validate_line,
    )
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from backend.training.scripts.validate_labels import (  # type: ignore
        CANONICAL_LABELS,
        ValidationError,
        validate_line,
    )


MODEL_ID = "SIRIS-Lab/citation-parser-ENTITY"
PUBLIC_DATASET_ID = "SIRIS-Lab/citation-parser-ENTITY"

DEFAULT_KAYNAKLAR_JSONL = Path("backend/training/data/kaynaklar/labeled.jsonl")
DEFAULT_MERGED_DIR = Path("backend/training/data/merged")
DEFAULT_PUBLIC_CACHE = Path("backend/training/data/public")

# word-or-punctuation tokenizer: matches CoNLL-style word segmentation, which
# is also what typical BIO token-classification datasets look like.
_WORD_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)

SPLIT_RATIOS = (0.70, 0.15, 0.15)  # train / val / test, applied to unique source_pdfs
RANDOM_SEED = 20260411


def word_tokenize_with_offsets(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    tokens: list[str] = []
    offsets: list[tuple[int, int]] = []
    for m in _WORD_RE.finditer(text):
        tokens.append(m.group())
        offsets.append((m.start(), m.end()))
    return tokens, offsets


def spans_to_word_bio(
    text: str,
    spans: list[dict],
    label2id: dict[str, int],
) -> tuple[list[str], list[int]]:
    """Convert char-level spans -> word tokens + BIO tag ids.

    Each span must have keys: start, end, label. Labels not in the model's
    label space (B-<label>) are silently dropped as O.
    """
    tokens, offsets = word_tokenize_with_offsets(text)
    tag_ids = [label2id["O"]] * len(tokens)
    sorted_spans = sorted(spans, key=lambda e: (e["start"], e["end"]))
    for span in sorted_spans:
        ent_start, ent_end, label = span["start"], span["end"], span["label"]
        first = True
        for idx, (tok_s, tok_e) in enumerate(offsets):
            if tok_s >= ent_end:
                break
            if tok_e <= ent_start:
                continue
            prefix = "B-" if first else "I-"
            tag = f"{prefix}{label}"
            if tag in label2id:
                tag_ids[idx] = label2id[tag]
            first = False
    return tokens, tag_ids


def load_kaynaklar(jsonl_path: Path) -> list[dict]:
    """Load kaynaklar span-format JSONL, validate it, and return as list of
    {text, spans, source_pdf} dicts (spans uses same shape as public's
    annotation field for uniform downstream handling).
    """
    if not jsonl_path.exists():
        raise FileNotFoundError(f"{jsonl_path} does not exist")
    records: list[dict] = []
    errors: list[ValidationError] = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            line_errors = validate_line(obj, line_no)
            if line_errors:
                errors.extend(line_errors)
                continue
            records.append(
                {
                    "text": obj["text"],
                    "spans": obj["entities"],
                    "source_pdf": obj.get("source_pdf", ""),
                    "id": obj.get("id", ""),
                }
            )
    if errors:
        for e in errors[:20]:
            ref = f" ref={e.ref_id}" if e.ref_id else ""
            print(f"  ! line {e.line_no}{ref}: {e.reason}", file=sys.stderr)
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more", file=sys.stderr)
        raise SystemExit(f"validation failed: {len(errors)} error(s) in {jsonl_path}")
    return records


def split_by_source_pdf(
    records: list[dict], ratios: tuple[float, float, float], seed: int
) -> tuple[list[dict], list[dict], list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        grouped[r["source_pdf"]].append(r)

    pdfs = sorted(grouped.keys())
    rng = random.Random(seed)
    rng.shuffle(pdfs)

    n = len(pdfs)
    n_train = max(1, round(n * ratios[0]))
    n_val = max(1, round(n * ratios[1]))
    if n_train + n_val >= n:
        n_val = max(1, n - n_train - 1) if n - n_train > 1 else 0
    train_pdfs = set(pdfs[:n_train])
    val_pdfs = set(pdfs[n_train : n_train + n_val])
    test_pdfs = set(pdfs[n_train + n_val :])

    def collect(pdf_set: set[str]) -> list[dict]:
        out: list[dict] = []
        for p in pdf_set:
            out.extend(grouped[p])
        return out

    return collect(train_pdfs), collect(val_pdfs), collect(test_pdfs)


def build_label_maps() -> tuple[dict[str, int], dict[int, str]]:
    from transformers import AutoConfig

    cfg = AutoConfig.from_pretrained(MODEL_ID)
    id2label: dict[int, str] = {int(k): v for k, v in cfg.id2label.items()}
    labels_in_cfg = set(id2label.values())
    missing = []
    for lbl in CANONICAL_LABELS:
        for prefix in ("B-", "I-"):
            if f"{prefix}{lbl}" not in labels_in_cfg:
                missing.append(f"{prefix}{lbl}")
    if missing:
        raise RuntimeError(f"model config is missing expected BIO labels: {missing[:5]}...")
    label2id = {v: k for k, v in id2label.items()}
    return label2id, id2label


def records_to_features(
    records: Iterable[dict], label2id: dict[str, int]
) -> dict[str, list]:
    tokens_col: list[list[str]] = []
    tags_col: list[list[int]] = []
    for r in records:
        tokens, tag_ids = spans_to_word_bio(r["text"], r["spans"], label2id)
        if not tokens:
            continue
        tokens_col.append(tokens)
        tags_col.append(tag_ids)
    return {"tokens": tokens_col, "ner_tags": tags_col}


def public_to_records(ds_split) -> list[dict]:
    """Convert the public dataset to our uniform {text, spans} format."""
    out: list[dict] = []
    for ex in ds_split:
        spans = [
            {"start": int(a["start"]), "end": int(a["end"]), "label": a["label"]}
            for a in ex["annotation"]
            if a.get("label") in CANONICAL_LABELS
        ]
        out.append({"text": ex["text"], "spans": spans, "source_pdf": "public", "id": ""})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kaynaklar-jsonl", type=Path, default=DEFAULT_KAYNAKLAR_JSONL)
    parser.add_argument("--merged-dir", type=Path, default=DEFAULT_MERGED_DIR)
    parser.add_argument("--public-cache", type=Path, default=DEFAULT_PUBLIC_CACHE)
    parser.add_argument(
        "--skip-kaynaklar",
        action="store_true",
        help="Dry-run using only the public dataset (no kaynaklar labels required).",
    )
    parser.add_argument("--seed", type=int, default=RANDOM_SEED)
    args = parser.parse_args()

    from datasets import Dataset, DatasetDict, concatenate_datasets, load_dataset

    print("Building label maps from model config...")
    label2id, id2label = build_label_maps()
    num_labels = len(id2label)
    print(f"  {num_labels} labels")

    print(f"Loading public dataset '{PUBLIC_DATASET_ID}'...")
    args.public_cache.mkdir(parents=True, exist_ok=True)
    public = load_dataset(PUBLIC_DATASET_ID, cache_dir=str(args.public_cache))
    print(f"  splits: {list(public.keys())}")
    for split_name, split in public.items():
        print(f"  public[{split_name}]: {len(split)} examples")

    public_test_key = "test" if "test" in public else ("dev" if "dev" in public else "validation")

    print("Converting public dataset to unified span format...")
    public_train_records = public_to_records(public["train"])
    public_test_records = public_to_records(public[public_test_key])

    if args.skip_kaynaklar:
        print("Skipping kaynaklar (dry run).")
        kaynaklar_records: list[dict] = []
        train_recs: list[dict] = []
        val_recs: list[dict] = []
        test_recs: list[dict] = []
    else:
        print(f"Loading and validating kaynaklar labels from {args.kaynaklar_jsonl}...")
        kaynaklar_records = load_kaynaklar(args.kaynaklar_jsonl)
        print(f"  {len(kaynaklar_records)} valid records")

        train_recs, val_recs, test_recs = split_by_source_pdf(
            kaynaklar_records, SPLIT_RATIOS, args.seed
        )

        train_pdfs = {r["source_pdf"] for r in train_recs}
        val_pdfs = {r["source_pdf"] for r in val_recs}
        test_pdfs = {r["source_pdf"] for r in test_recs}
        assert not (train_pdfs & test_pdfs), "train/test source_pdf leakage"
        assert not (train_pdfs & val_pdfs), "train/val source_pdf leakage"
        assert not (val_pdfs & test_pdfs), "val/test source_pdf leakage"

        print(
            f"  split by source_pdf: train={len(train_recs)} ({len(train_pdfs)} pdfs)"
            f"  val={len(val_recs)} ({len(val_pdfs)} pdfs)"
            f"  test={len(test_recs)} ({len(test_pdfs)} pdfs)"
        )

    # Convert everything to tokens + ner_tags
    print("Converting spans to word-level BIO...")
    public_train_feats = records_to_features(public_train_records, label2id)
    public_test_feats = records_to_features(public_test_records, label2id)
    kaynaklar_train_feats = records_to_features(train_recs, label2id)
    kaynaklar_val_feats = records_to_features(val_recs, label2id)
    kaynaklar_test_feats = records_to_features(test_recs, label2id)

    public_train = Dataset.from_dict(public_train_feats)
    public_test = Dataset.from_dict(public_test_feats)
    kaynaklar_train = Dataset.from_dict(kaynaklar_train_feats)
    kaynaklar_val = Dataset.from_dict(kaynaklar_val_feats)
    kaynaklar_test = Dataset.from_dict(kaynaklar_test_feats)

    if len(kaynaklar_train) > 0:
        train = concatenate_datasets([public_train, kaynaklar_train]).shuffle(seed=args.seed)
    else:
        train = public_train.shuffle(seed=args.seed)

    validation = kaynaklar_val if len(kaynaklar_val) > 0 else public_test

    merged = DatasetDict(
        {
            "train": train,
            "validation": validation,
            "kaynaklar_test": kaynaklar_test,
            "public_test": public_test,
        }
    )

    args.merged_dir.mkdir(parents=True, exist_ok=True)
    merged.save_to_disk(str(args.merged_dir))

    (args.merged_dir / "label_maps.json").write_text(
        json.dumps(
            {"label2id": label2id, "id2label": {str(k): v for k, v in id2label.items()}},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if not args.skip_kaynaklar and test_recs:
        sidecar_path = args.merged_dir / "kaynaklar_test_raw.jsonl"
        with sidecar_path.open("w", encoding="utf-8") as f:
            for r in test_recs:
                out = {
                    "id": r.get("id", ""),
                    "source_pdf": r.get("source_pdf", ""),
                    "text": r["text"],
                    "entities": r["spans"],
                }
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
        print(f"  wrote {sidecar_path} ({len(test_recs)} records)")

    print()
    print(f"Saved merged DatasetDict to {args.merged_dir}")
    for split_name, split in merged.items():
        print(f"  {split_name}: {len(split)} examples")
    return 0


if __name__ == "__main__":
    sys.exit(main())
