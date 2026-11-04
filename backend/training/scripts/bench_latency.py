"""Latency, disk, and memory benchmark: SIRIS baseline vs fine-tuned fp32 vs INT8 ONNX.

Measures p50/p95/p99 ms per source on real kaynaklar_test raw texts, peak
RSS during inference, and on-disk model size. Appends a markdown table to the
most recent `eval_<timestamp>.md` under `backend/training/reports/`, or creates
a new report if none exists.
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
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

WARMUP_REFS = 5
BENCH_REFS = 500


def dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    if path.is_file():
        return path.stat().st_size / (1024 * 1024)
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / (1024 * 1024)


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
    return hf_pipeline("ner", model=model, tokenizer=tokenizer, aggregation_strategy="simple")


def build_ort_pipeline(model_path: Path):
    from optimum.onnxruntime import ORTModelForTokenClassification
    from transformers import AutoTokenizer
    from transformers import pipeline as hf_pipeline

    tokenizer = AutoTokenizer.from_pretrained(str(model_path))
    onnx_files = sorted(model_path.glob("*.onnx"))
    if not onnx_files:
        raise FileNotFoundError(f"no .onnx files in {model_path}")
    file_name = onnx_files[0].name
    model = ORTModelForTokenClassification.from_pretrained(str(model_path), file_name=file_name)
    return hf_pipeline("ner", model=model, tokenizer=tokenizer, aggregation_strategy="simple")


def bench_pipeline(pipeline_obj, refs: list[str]) -> dict:
    import psutil

    process = psutil.Process()
    rss_before = process.memory_info().rss

    # Warmup
    for text in refs[:WARMUP_REFS]:
        pipeline_obj(text)

    # Measurement
    latencies_ms: list[float] = []
    peak_rss = rss_before
    for text in refs[:BENCH_REFS]:
        t0 = time.perf_counter_ns()
        pipeline_obj(text)
        latencies_ms.append((time.perf_counter_ns() - t0) / 1_000_000)
        peak_rss = max(peak_rss, process.memory_info().rss)

    latencies_ms.sort()
    n = len(latencies_ms)
    p50 = latencies_ms[int(n * 0.50)]
    p95 = latencies_ms[min(int(n * 0.95), n - 1)]
    p99 = latencies_ms[min(int(n * 0.99), n - 1)]
    mean = statistics.mean(latencies_ms)

    return {
        "n": n,
        "p50_ms": p50,
        "p95_ms": p95,
        "p99_ms": p99,
        "mean_ms": mean,
        "peak_rss_mb": peak_rss / (1024 * 1024),
        "delta_rss_mb": (peak_rss - rss_before) / (1024 * 1024),
    }


def load_source_texts(merged_dir: Path, limit: int) -> list[str]:
    sidecar = merged_dir / "kaynaklar_test_raw.jsonl"
    texts: list[str] = []
    if sidecar.exists():
        for line in sidecar.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            t = obj.get("text")
            if isinstance(t, str) and t.strip():
                texts.append(t)
    else:
        # Fallback: reconstruct from the tokenized split by joining tokens
        from datasets import load_from_disk

        ds = load_from_disk(str(merged_dir))
        for example in ds["kaynaklar_test"]:
            texts.append(" ".join(example["tokens"]))

    # Cycle the list if it's shorter than BENCH_REFS so we always get a full sample
    while len(texts) < limit and texts:
        texts = texts + texts
    return texts[:limit]


def find_latest_report(reports_dir: Path) -> Path | None:
    if not reports_dir.exists():
        return None
    candidates = sorted(reports_dir.glob("eval_*.md"))
    return candidates[-1] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merged-dir", type=Path, default=DEFAULT_MERGED_DIR)
    parser.add_argument("--finetuned-fp32", type=Path, default=DEFAULT_FINETUNED_FP32)
    parser.add_argument("--finetuned-int8", type=Path, default=DEFAULT_FINETUNED_INT8)
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    parser.add_argument("--report", type=Path, default=None, help="Specific report file to append to.")
    parser.add_argument(
        "--skip",
        choices=["baseline", "fp32", "int8"],
        action="append",
        default=[],
    )
    args = parser.parse_args()

    refs = load_source_texts(args.merged_dir, BENCH_REFS + WARMUP_REFS)
    if not refs:
        print("error: no source texts available for benchmarking", file=sys.stderr)
        return 2
    print(f"Loaded {len(refs)} source texts for benchmarking")

    models_to_test: list[tuple[str, Callable[[], object], Path | None]] = []
    if "baseline" not in args.skip:
        models_to_test.append(("baseline", lambda: build_hf_pipeline(BASELINE_MODEL_ID), None))
    if "fp32" not in args.skip:
        models_to_test.append(
            ("finetuned-fp32", lambda: build_hf_pipeline(args.finetuned_fp32), args.finetuned_fp32)
        )
    if "int8" not in args.skip:
        models_to_test.append(
            ("finetuned-int8", lambda: build_ort_pipeline(args.finetuned_int8), args.finetuned_int8)
        )

    results: list[dict] = []
    for name, builder, disk_path in models_to_test:
        print(f"\n=== {name} ===")
        try:
            pipeline_obj = builder()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! failed to load: {exc}", file=sys.stderr)
            continue

        stats = bench_pipeline(pipeline_obj, refs)
        disk_mb = dir_size_mb(disk_path) if disk_path else 0.0  # baseline lives in HF cache, not a tracked dir
        stats["disk_mb"] = disk_mb
        stats["model"] = name
        results.append(stats)
        print(
            f"  p50={stats['p50_ms']:.1f}ms  p95={stats['p95_ms']:.1f}ms  "
            f"p99={stats['p99_ms']:.1f}ms  peak_rss={stats['peak_rss_mb']:.0f}MB  "
            f"disk={disk_mb:.0f}MB"
        )

        del pipeline_obj
        gc.collect()

    report_path = args.report or find_latest_report(args.reports_dir)
    if report_path is None:
        args.reports_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = args.reports_dir / f"eval_{ts}.md"
        report_path.write_text(f"# Citation NER evaluation — {ts}\n\n", encoding="utf-8")

    lines = [
        "",
        "## Latency & footprint",
        "",
        "| Model | p50 ms | p95 ms | p99 ms | Mean ms | Peak RSS MB | Disk MB |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        disk = f"{r['disk_mb']:.0f}" if r["disk_mb"] else "n/a"
        lines.append(
            f"| {r['model']} | {r['p50_ms']:.1f} | {r['p95_ms']:.1f} "
            f"| {r['p99_ms']:.1f} | {r['mean_ms']:.1f} | {r['peak_rss_mb']:.0f} | {disk} |"
        )
    lines.append("")
    lines.append(f"Bench sample size: {BENCH_REFS} sources (after {WARMUP_REFS}-ref warmup)")
    lines.append("")

    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    report_path.write_text(existing + "\n".join(lines), encoding="utf-8")
    print(f"\nAppended latency results to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
