"""Head-to-head evaluation: SIRIS baseline vs fine-tuned fp32 vs fine-tuned INT8.

Computes per-label seqeval F1 on kaynaklar_test and public_test, plus a
downstream match rate (fraction of sources for which the resulting
ParsedSource has title + authors + year populated at confidence >= 0.3) using
the live app's own grouping logic from `backend/services/ner_extractor.py`.

Appends a markdown table to `backend/training/reports/eval_<timestamp>.md`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MERGED_DIR = Path("backend/training/data/merged")
DEFAULT_REPORTS_DIR = Path("backend/training/reports")
DEFAULT_FINETUNED_FP32 = Path("backend/training/models/finetuned")
DEFAULT_FINETUNED_INT8 = Path("backend/training/models/finetuned-onnx-int8")

BASELINE_MODEL_ID = "SIRIS-Lab/citation-parser-ENTITY"


def _ensure_repo_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    backend_root = repo_root / "backend"
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))


def build_hf_pipeline(model_id_or_path: str | Path):
    from transformers import (
        AutoModelForTokenClassification,
        AutoTokenizer,
    )
    from transformers import (
        pipeline as hf_pipeline,
    )

    tokenizer = AutoTokenizer.from_pretrained(str(model_id_or_path))
    model = AutoModelForTokenClassification.from_pretrained(str(model_id_or_path))
    return hf_pipeline(
        "ner",
        model=model,
        tokenizer=tokenizer,
        aggregation_strategy="simple",
    )


def build_ort_pipeline(model_path: Path):
    from optimum.onnxruntime import ORTModelForTokenClassification
    from transformers import AutoTokenizer
    from transformers import pipeline as hf_pipeline

    tokenizer = AutoTokenizer.from_pretrained(str(model_path))
    # Quantized dirs have `model_quantized.onnx`, plain dirs have `model.onnx`.
    # Auto-detect so both load cleanly without warnings.
    onnx_files = sorted(model_path.glob("*.onnx"))
    if not onnx_files:
        raise FileNotFoundError(f"no .onnx files in {model_path}")
    file_name = onnx_files[0].name
    model = ORTModelForTokenClassification.from_pretrained(str(model_path), file_name=file_name)
    return hf_pipeline(
        "ner",
        model=model,
        tokenizer=tokenizer,
        aggregation_strategy="simple",
    )


def seqeval_on_split(
    pipeline_obj,
    split,
    id2label: dict[int, str],
) -> dict:
    """Run the pipeline on a tokenized split and compute per-label seqeval.

    `split` is a datasets Dataset with `tokens` and `ner_tags` columns.
    """
    import evaluate
    import numpy as np
    import torch

    metric = evaluate.load("seqeval")
    tokenizer = pipeline_obj.tokenizer
    model = pipeline_obj.model
    device = getattr(model, "device", None)

    true_labels: list[list[str]] = []
    pred_labels: list[list[str]] = []

    for example in split:
        tokens = example["tokens"]
        gold_ids = example["ner_tags"]
        enc = tokenizer(
            tokens,
            is_split_into_words=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        if device is not None and hasattr(device, "type"):
            enc = {k: v.to(device) for k, v in enc.items()}
        else:
            enc = {k: v for k, v in enc.items()}

        with torch.no_grad():
            outputs = model(**enc)
        logits = outputs.logits[0].cpu().numpy()
        preds = np.argmax(logits, axis=-1)

        word_ids = tokenizer(
            tokens, is_split_into_words=True, truncation=True, max_length=512
        ).word_ids()

        gold_row: list[str] = []
        pred_row: list[str] = []
        prev_word = None
        for tok_idx, word_idx in enumerate(word_ids):
            if word_idx is None or word_idx == prev_word:
                prev_word = word_idx
                continue
            if word_idx >= len(gold_ids):
                break
            gold_row.append(id2label[int(gold_ids[word_idx])])
            pred_row.append(id2label[int(preds[tok_idx])])
            prev_word = word_idx

        if gold_row:
            true_labels.append(gold_row)
            pred_labels.append(pred_row)

    results = metric.compute(predictions=pred_labels, sources=true_labels)
    return results


def downstream_match_rate(
    pipeline_obj,
    raw_records: list[dict],
    extract_fn: Callable,
) -> tuple[float, int]:
    """Run the pipeline on raw kaynaklar source texts and count how many
    produce a ParsedSource with title + authors + year and confidence >= 0.3.
    """
    ok = 0
    total = 0
    for rec in raw_records:
        raw_text = rec["text"]
        if not raw_text:
            continue
        total += 1
        try:
            parsed = extract_fn(pipeline_obj, raw_text)
        except Exception:  # noqa: BLE001, S112
            continue
        title = getattr(parsed, "title", None)
        authors = getattr(parsed, "authors", None) or []
        year = getattr(parsed, "year", None)
        confidence = getattr(parsed, "parse_confidence", 0.0) or 0.0
        if title and authors and year and confidence >= 0.3:
            ok += 1
    rate = (ok / total) if total else 0.0
    return rate, total


def summarize_seqeval(results: dict) -> dict:
    return {
        "precision": results.get("overall_precision", 0.0),
        "recall": results.get("overall_recall", 0.0),
        "f1": results.get("overall_f1", 0.0),
        "accuracy": results.get("overall_accuracy", 0.0),
    }


def format_per_label(results: dict) -> str:
    rows = []
    for key, value in sorted(results.items()):
        if key.startswith("overall_") or not isinstance(value, dict):
            continue
        rows.append(
            f"  {key:30s}  P={value.get('precision', 0):.3f}"
            f"  R={value.get('recall', 0):.3f}"
            f"  F1={value.get('f1', 0):.3f}"
            f"  n={value.get('number', 0)}"
        )
    return "\n".join(rows)


def main() -> int:
    _ensure_repo_on_path()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merged-dir", type=Path, default=DEFAULT_MERGED_DIR)
    parser.add_argument("--finetuned-fp32", type=Path, default=DEFAULT_FINETUNED_FP32)
    parser.add_argument("--finetuned-int8", type=Path, default=DEFAULT_FINETUNED_INT8)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument(
        "--skip",
        choices=["baseline", "fp32", "int8"],
        action="append",
        default=[],
        help="Skip a specific model (useful during development).",
    )
    args = parser.parse_args()

    from datasets import load_from_disk

    merged = load_from_disk(str(args.merged_dir))
    label_maps = json.loads((args.merged_dir / "label_maps.json").read_text(encoding="utf-8"))
    id2label = {int(k): v for k, v in label_maps["id2label"].items()}

    sidecar = args.merged_dir / "kaynaklar_test_raw.jsonl"
    raw_records: list[dict] = []
    if sidecar.exists():
        for line in sidecar.read_text(encoding="utf-8").splitlines():
            if line.strip():
                raw_records.append(json.loads(line))
    else:
        print(f"warning: {sidecar} not found; downstream match rate will be skipped", file=sys.stderr)

    # Import the app's real extraction path so the downstream metric reflects
    # exactly what happens in production.
    from services import ner_extractor as ner_extractor_module

    extract_fn = ner_extractor_module._extract  # (pipeline, raw_text) -> ParsedSource

    models_to_test: list[tuple[str, Callable[[], object]]] = []
    if "baseline" not in args.skip:
        models_to_test.append(("baseline", lambda: build_hf_pipeline(BASELINE_MODEL_ID)))
    if "fp32" not in args.skip:
        models_to_test.append(("finetuned-fp32", lambda: build_hf_pipeline(args.finetuned_fp32)))
    if "int8" not in args.skip:
        models_to_test.append(("finetuned-int8", lambda: build_ort_pipeline(args.finetuned_int8)))

    rows: list[dict] = []
    detail_blocks: list[str] = []

    for name, builder in models_to_test:
        print(f"\n=== {name} ===")
        t0 = time.perf_counter()
        try:
            pipeline_obj = builder()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! failed to load: {exc}", file=sys.stderr)
            continue

        print(f"  loaded in {time.perf_counter() - t0:.1f}s")

        print("  seqeval on kaynaklar_test...")
        kaynaklar_results = seqeval_on_split(pipeline_obj, merged["kaynaklar_test"], id2label)
        kaynaklar_summary = summarize_seqeval(kaynaklar_results)
        print(f"    F1={kaynaklar_summary['f1']:.4f}")

        print("  seqeval on public_test...")
        public_results = seqeval_on_split(pipeline_obj, merged["public_test"], id2label)
        public_summary = summarize_seqeval(public_results)
        print(f"    F1={public_summary['f1']:.4f}")

        downstream_ok = 0.0
        downstream_n = 0
        if raw_records:
            print("  downstream match rate on kaynaklar_test raw...")
            downstream_ok, downstream_n = downstream_match_rate(
                pipeline_obj, raw_records, extract_fn
            )
            print(f"    {downstream_ok * 100:.1f}% ({downstream_n} refs)")

        rows.append(
            {
                "model": name,
                "f1_kaynaklar": kaynaklar_summary["f1"],
                "f1_public": public_summary["f1"],
                "downstream_ok": downstream_ok,
                "downstream_n": downstream_n,
            }
        )

        detail_blocks.append(
            f"### {name}\n\n"
            f"**kaynaklar_test**: F1={kaynaklar_summary['f1']:.4f}  "
            f"P={kaynaklar_summary['precision']:.4f}  R={kaynaklar_summary['recall']:.4f}\n\n"
            "```\n" + format_per_label(kaynaklar_results) + "\n```\n\n"
            f"**public_test**: F1={public_summary['f1']:.4f}  "
            f"P={public_summary['precision']:.4f}  R={public_summary['recall']:.4f}\n\n"
            "```\n" + format_per_label(public_results) + "\n```\n"
        )

    # Write the report
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.reports_dir / f"eval_{ts}.md"

    lines = [
        f"# Citation NER evaluation — {ts}",
        "",
        "| Model | F1 kaynaklar | F1 public | Downstream OK% | n |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['model']} "
            f"| {r['f1_kaynaklar']:.4f} "
            f"| {r['f1_public']:.4f} "
            f"| {r['downstream_ok'] * 100:.1f}% "
            f"| {r['downstream_n']} |"
        )
    lines.append("")
    lines.append("## Per-label detail")
    lines.append("")
    lines.extend(detail_blocks)

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
