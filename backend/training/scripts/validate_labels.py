"""Validate span-format labeled JSONL against the SIRIS 14-label schema.

Run standalone to validate an existing file, or import `validate_line` /
`validate_file` as a library from other scripts.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

CANONICAL_LABELS: frozenset[str] = frozenset({
    "TITLE",
    "AUTHORS",
    "PUBLICATION_YEAR",
    "JOURNAL",
    "DOI",
    "ISBN",
    "LOCATION",
    "LINK_ONLINE_AVAILABILITY",
    "ISSN",
    "PUBLISHER",
    "PAGE_FIRST",
    "PAGE_LAST",
    "ISSUE",
    "VOLUME",
})


@dataclass
class ValidationError:
    line_no: int
    ref_id: str | None
    reason: str


def _iter_entities(raw: dict) -> list[dict]:
    ents = raw.get("entities")
    if not isinstance(ents, list):
        raise TypeError("missing or non-list 'entities'")
    return ents


def validate_line(raw: dict, line_no: int) -> list[ValidationError]:
    errors: list[ValidationError] = []
    ref_id = raw.get("id") if isinstance(raw.get("id"), str) else None

    def err(reason: str) -> None:
        errors.append(ValidationError(line_no, ref_id, reason))

    text = raw.get("text")
    if not isinstance(text, str) or not text:
        err("missing or empty 'text'")
        return errors

    try:
        entities = _iter_entities(raw)
    except ValueError as exc:
        err(str(exc))
        return errors

    if not entities and len(text) > 20:
        err("empty entities on non-trivial text (>20 chars)")

    text_len = len(text)
    occupied = [False] * text_len

    for i, ent in enumerate(entities):
        if not isinstance(ent, dict):
            err(f"entity[{i}] is not an object")
            continue
        label = ent.get("label")
        start = ent.get("start")
        end = ent.get("end")

        if label not in CANONICAL_LABELS:
            err(f"entity[{i}] unknown label '{label}'")
            continue
        if not isinstance(start, int) or not isinstance(end, int):
            err(f"entity[{i}] non-integer offsets")
            continue
        if start < 0 or end > text_len or start >= end:
            err(f"entity[{i}] offsets [{start},{end}) out of range or empty (text_len={text_len})")
            continue

        span_text = text[start:end]
        stripped = span_text.strip()
        if not stripped:
            err(f"entity[{i}] span is whitespace only")
            continue
        if span_text != span_text.strip():
            err(f"entity[{i}] span has leading/trailing whitespace: {span_text!r}")

        for j in range(start, end):
            if occupied[j]:
                err(f"entity[{i}] overlaps a previous span at position {j}")
                break
            occupied[j] = True

    return errors


def validate_file(path: Path) -> Iterator[ValidationError]:
    with path.open("r", encoding="utf-8") as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                yield ValidationError(line_no, None, f"invalid JSON: {exc.msg}")
                continue
            yield from validate_line(obj, line_no)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate span-format labeled JSONL.")
    parser.add_argument("path", type=Path, help="Path to labeled.jsonl (or any span-format JSONL)")
    parser.add_argument("--max-errors", type=int, default=50, help="Stop reporting after N errors")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"error: {args.path} does not exist", file=sys.stderr)
        return 2

    errors = list(validate_file(args.path))
    if not errors:
        print(f"OK  {args.path}: no validation errors")
        return 0

    for e in errors[: args.max_errors]:
        ref = f" ref={e.ref_id}" if e.ref_id else ""
        print(f"line {e.line_no}{ref}: {e.reason}")
    if len(errors) > args.max_errors:
        print(f"... and {len(errors) - args.max_errors} more")
    print(f"FAIL  {args.path}: {len(errors)} error(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
