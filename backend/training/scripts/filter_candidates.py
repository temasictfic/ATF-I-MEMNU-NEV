"""Read cached per-PDF JSONs, strip source noise, and bucket each source
as APA-like (skip) or non-APA (candidate for labeling).

Writes `to_label.jsonl` (non-APA) and `skipped_apa.jsonl` (audit trail) under
`backend/training/data/kaynaklar/`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

try:
    from backend.utils.text_cleaning import strip_source_noise
except ModuleNotFoundError:
    # Allow running with cwd = repo root even if backend isn't installed as package
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from backend.utils.text_cleaning import strip_source_noise  # type: ignore


DEFAULT_INPUT_DIR = Path("backend/training/data/kaynaklar/input")
DEFAULT_OUTPUT_DIR = Path("backend/training/data/kaynaklar")

# APA-like signals — a parenthesized 4-digit year appearing reasonably early
# in the (already pre-stripped) source text. Conservative: we only bucket
# into apa_like when we positively match this, otherwise default to non_apa.
_APA_PAREN_YEAR = re.compile(r"\([12]\d{3}[a-z]?\)")
_APA_WINDOW_CHARS = 200


_TEXT_KEYS = ("raw_text", "text")
_MIN_REF_TEXT_LEN = 20


def _get_ref_text(d: dict) -> str | None:
    """Return the first non-empty source-text-like field, or None."""
    for key in _TEXT_KEYS:
        v = d.get(key)
        if isinstance(v, str) and len(v.strip()) >= _MIN_REF_TEXT_LEN:
            return v
    return None


def walk_sources(obj: object) -> Iterator[dict]:
    """Yield every dict in the tree that looks like a source record.

    A source record is a dict with a `raw_text` or `text` field containing
    a string at least 20 characters long. The length threshold keeps us from
    accidentally picking up nested structures (bboxes, metadata) that happen
    to carry a `text` key.
    """
    if isinstance(obj, dict):
        if _get_ref_text(obj) is not None:
            yield obj
            return  # don't recurse into a record's own children
        for v in obj.values():
            yield from walk_sources(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_sources(item)


def classify(stripped_text: str) -> tuple[str, str]:
    """Return (bucket, reason) for a pre-stripped source text."""
    window = stripped_text[:_APA_WINDOW_CHARS]
    if _APA_PAREN_YEAR.search(window):
        return "apa_like", "paren_year_early"
    return "non_apa", "no_paren_year"


def process_file(
    json_path: Path,
    seen_texts: set[str],
) -> tuple[list[dict], list[dict], int]:
    """Returns (non_apa, apa_like, duplicates_skipped) for one cached JSON."""
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"  ! skipping {json_path.name}: {exc}", file=sys.stderr)
        return [], [], 0

    non_apa: list[dict] = []
    apa_like: list[dict] = []
    dupes = 0
    pdf_name = json_path.stem

    for idx, ref in enumerate(walk_sources(data)):
        raw = _get_ref_text(ref)
        if raw is None:
            continue
        stripped = strip_source_noise(raw)
        if not stripped or len(stripped) < 10:
            continue

        # Global dedup — same stripped text can appear across PDFs (boilerplate
        # citation appendices, for example). Keep the first occurrence only.
        if stripped in seen_texts:
            dupes += 1
            continue
        seen_texts.add(stripped)

        bucket, reason = classify(stripped)
        record = {
            "id": f"{pdf_name}_ref_{idx:04d}",
            "source_pdf": json_path.name,
            "text": stripped,
            "bucket": bucket,
            "bucket_reason": reason,
        }
        if bucket == "apa_like":
            apa_like.append(record)
        else:
            non_apa.append(record)

    return non_apa, apa_like, dupes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    input_dir: Path = args.input_dir
    output_dir: Path = args.output_dir

    if not input_dir.exists():
        print(f"error: {input_dir} does not exist. Create it and drop cached parse JSONs inside.", file=sys.stderr)
        return 2

    json_files = sorted(input_dir.glob("*.json"))
    if not json_files:
        print(f"error: no .json files found in {input_dir}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    to_label_path = output_dir / "to_label.jsonl"
    skipped_path = output_dir / "skipped_apa.jsonl"

    seen_texts: set[str] = set()
    total_non_apa = 0
    total_apa = 0
    total_dupes = 0

    with to_label_path.open("w", encoding="utf-8") as f_label, skipped_path.open(
        "w", encoding="utf-8"
    ) as f_skip:
        for json_path in json_files:
            non_apa, apa_like, dupes = process_file(json_path, seen_texts)
            for r in non_apa:
                f_label.write(json.dumps(r, ensure_ascii=False) + "\n")
            for r in apa_like:
                f_skip.write(json.dumps(r, ensure_ascii=False) + "\n")
            total_non_apa += len(non_apa)
            total_apa += len(apa_like)
            total_dupes += dupes
            print(
                f"  {json_path.name}: non_apa={len(non_apa)} apa_like={len(apa_like)} duplicates_skipped={dupes}"
            )

    total = total_non_apa + total_apa
    pct_non_apa = (100.0 * total_non_apa / total) if total else 0.0
    print()
    print(f"processed {len(json_files)} files")
    print(f"  non_apa (to label): {total_non_apa} ({pct_non_apa:.1f}%)  -> {to_label_path}")
    print(f"  apa_like (skipped): {total_apa}                          -> {skipped_path}")
    print(f"  duplicates dropped: {total_dupes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
