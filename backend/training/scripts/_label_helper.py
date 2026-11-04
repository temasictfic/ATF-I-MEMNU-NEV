"""Labeling helper (internal tool, not part of the runbook).

Takes a dict mapping source id -> list of (substring, label) annotations,
looks each substring up in the pre-stripped text of `to_label.jsonl`,
computes offsets, ensures no overlap, and appends validator-clean records
to `labeled.jsonl`. Run the validator on the output file at the end.

Usage (from labeling batch modules):
    from backend.training.scripts._label_helper import label_batch
    label_batch({
        "126E147_ref_0018": [
            ("Chennamma, H. R., Yuan, X", "AUTHORS"),
            ("2013", "PUBLICATION_YEAR"),
            ("A Survey on Eye-Gaze Tracking Techniques", "TITLE"),
            ("arXiv", "JOURNAL"),
            ("10.48550/arXiv.1312.6410", "DOI"),
        ],
        ...
    })
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from backend.training.scripts.validate_labels import CANONICAL_LABELS, validate_line
except ModuleNotFoundError:
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from backend.training.scripts.validate_labels import (  # type: ignore
        CANONICAL_LABELS,
        validate_line,
    )


TO_LABEL = Path("backend/training/data/kaynaklar/to_label.jsonl")
LABELED = Path("backend/training/data/kaynaklar/labeled.jsonl")
UNLABELABLE = Path("backend/training/data/kaynaklar/unlabelable.jsonl")


def _load_to_label() -> dict[str, dict]:
    out: dict[str, dict] = {}
    with TO_LABEL.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            out[obj["id"]] = obj
    return out


def _load_existing_ids() -> set[str]:
    ids: set[str] = set()
    for path in (LABELED, UNLABELABLE):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ref_id = obj.get("id")
                if isinstance(ref_id, str):
                    ids.add(ref_id)
    return ids


def _find_span(text: str, substring: str, taken: list[tuple[int, int]]) -> tuple[int, int] | None:
    """Find first occurrence of `substring` in `text` that doesn't collide with `taken`."""
    search_start = 0
    while True:
        idx = text.find(substring, search_start)
        if idx < 0:
            return None
        start, end = idx, idx + len(substring)
        if all(end <= s or start >= e for s, e in taken):
            return (start, end)
        search_start = idx + 1


def _build_entities(text: str, annotations: list[tuple[str, str]]) -> tuple[list[dict], list[str]]:
    entities: list[dict] = []
    errors: list[str] = []
    taken: list[tuple[int, int]] = []
    for substring, label in annotations:
        if label not in CANONICAL_LABELS:
            errors.append(f"unknown label '{label}' for substring {substring!r}")
            continue
        span = _find_span(text, substring, taken)
        if span is None:
            errors.append(f"substring not found (or all occurrences overlap): {substring!r}  in  {text[:120]!r}...")
            continue
        start, end = span
        # Trim whitespace from the edges by shifting start/end inward
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start >= end:
            errors.append(f"empty after whitespace trim: {substring!r}")
            continue
        entities.append({"start": start, "end": end, "label": label})
        taken.append((start, end))
    entities.sort(key=lambda e: e["start"])
    return entities, errors


def label_batch(
    annotations_by_id: dict[str, list[tuple[str, str]]],
    unlabelable: dict[str, str] | None = None,
) -> None:
    unlabelable = unlabelable or {}
    to_label = _load_to_label()
    existing = _load_existing_ids()

    labeled_out: list[dict] = []
    skipped_out: list[dict] = []
    total_errors = 0

    for ref_id, ann in annotations_by_id.items():
        if ref_id in existing:
            print(f"  = {ref_id}: already labeled, skipping")
            continue
        if ref_id not in to_label:
            print(f"  ! {ref_id}: not found in to_label.jsonl")
            total_errors += 1
            continue
        rec = to_label[ref_id]
        text = rec["text"]
        entities, errors = _build_entities(text, ann)
        if errors:
            for e in errors:
                print(f"  ! {ref_id}: {e}")
            total_errors += len(errors)
            continue
        out = {
            "id": ref_id,
            "source_pdf": rec["source_pdf"],
            "text": text,
            "entities": entities,
        }
        line_errors = validate_line(out, line_no=0)
        if line_errors:
            for e in line_errors:
                print(f"  ! {ref_id}: validator: {e.reason}")
            total_errors += len(line_errors)
            continue
        labeled_out.append(out)

    for ref_id, reason in unlabelable.items():
        if ref_id in existing:
            continue
        if ref_id not in to_label:
            continue
        rec = to_label[ref_id]
        skipped_out.append(
            {
                "id": ref_id,
                "source_pdf": rec["source_pdf"],
                "text": rec["text"],
                "reason": reason,
            }
        )

    if total_errors:
        print(f"  ! batch has {total_errors} error(s) — nothing written. Fix and retry.")
        return

    if labeled_out:
        with LABELED.open("a", encoding="utf-8") as f:
            for rec in labeled_out:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  + wrote {len(labeled_out)} labeled records -> {LABELED}")

    if skipped_out:
        with UNLABELABLE.open("a", encoding="utf-8") as f:
            for rec in skipped_out:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"  + wrote {len(skipped_out)} unlabelable records -> {UNLABELABLE}")


def show_batch(batch_index: int, batch_size: int = 20) -> None:
    """Pretty-print a batch of to_label records with their texts, for labeling."""
    to_label = _load_to_label()
    keys = list(to_label.keys())
    start = batch_index * batch_size
    end = min(start + batch_size, len(keys))
    print(f"=== batch {batch_index} ({start}..{end} of {len(keys)}) ===")
    for i in range(start, end):
        rec = to_label[keys[i]]
        print(f"[{rec['id']}]")
        print(f"  {rec['text']}")
        print()


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--show", type=int, help="Show batch N (0-indexed)")
    p.add_argument("--size", type=int, default=20, help="Batch size")
    args = p.parse_args()
    if args.show is not None:
        show_batch(args.show, args.size)
