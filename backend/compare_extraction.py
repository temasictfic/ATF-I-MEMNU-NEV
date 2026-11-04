"""NER-vs-regex extractor comparison harness.

Walks user-approved cache JSONs (`%APPDATA%/atfi-memnu-app/output/cache/*.json`),
runs both extractors on every source `text`, and reports per-field divergence.

Treats NER as the oracle and regex as the hypothesis; entries where NER itself
is unconfident (`parse_confidence < --ner-conf-min`, default 0.3) are skipped
so the oracle stays trustworthy.

Run from repo root:
    python backend/compare_extraction.py
    python backend/compare_extraction.py --csv out.csv --examples 10
    python backend/compare_extraction.py --cache-dir path/to/cache
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from io import TextIOWrapper
from pathlib import Path


def _ensure_backend_on_path() -> None:
    backend_root = Path(__file__).resolve().parent
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))


_ensure_backend_on_path()

# Windows consoles default to cp1254 (Turkish) which can't encode em-dashes,
# ellipses, and other glyphs that show up in PDF-extracted source text.
# Force UTF-8 with `replace` so a stray glyph never aborts a long run.
for _stream in (sys.stdout, sys.stderr):
    if isinstance(_stream, TextIOWrapper):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001, S110
            pass

from models.source import ParsedSource
from rapidfuzz import fuzz
from services.ner_extractor import extract_fields_ner
from services.source_extractor import _extract_source_fields_regex
from utils.text_cleaning import strip_source_noise

TITLE_SIM_THRESHOLD = 90
JOURNAL_SIM_THRESHOLD = 85
AUTHORS_JACCARD_THRESHOLD = 0.8

# Trailing volume/issue/page tokens that NER trims off journal names but
# regex sometimes leaves attached. Strip before comparing journals so the
# similarity score reflects actual journal-name divergence, not formatting.
_JOURNAL_TRAILING_RE = re.compile(
    r"\s*(?:,?\s*vol(?:ume)?\.?\s*\d+.*"
    r"|,?\s*no\.?\s*\d+.*"
    r"|,?\s*issue\s*\d+.*"
    r"|,?\s*pp?\.?\s*\d+.*"
    r"|,?\s*\(\d+\).*"
    r"|,?\s*\d+\s*\(\d+\).*)$",
    flags=re.IGNORECASE,
)


def _normalize_for_compare(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _strip_journal_tail(s: str) -> str:
    return _JOURNAL_TRAILING_RE.sub("", s or "").strip(" ,.;")


def _last_name(author: str) -> str:
    """Heuristic last-name extraction for set comparison.

    Handles "Smith, J." → "smith", "John Smith" → "smith",
    "Smith J" → "smith". Returns lowercased last name.
    """
    a = author.strip().rstrip(",.")
    if not a:
        return ""
    if "," in a:
        # "Smith, J." form: last name is before the comma.
        return _normalize_for_compare(a.split(",", 1)[0])
    # No comma: assume "First Last" or "F. Last" — last token is the surname.
    parts = a.split()
    if not parts:
        return ""
    # If the last token is just initials ("J.", "AB"), back off to the
    # previous one (Vancouver "Smith J" form).
    last = parts[-1]
    if re.fullmatch(r"[A-Z](?:\.?[A-Z]){0,3}\.?", last) and len(parts) >= 2:
        return _normalize_for_compare(parts[-2])
    return _normalize_for_compare(last)


def _author_set(authors: list[str]) -> set[str]:
    return {ln for ln in (_last_name(a) for a in authors or []) if ln}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class FieldStats:
    name: str
    diverged: int = 0
    examples: list[dict] = field(default_factory=list)

    def add_example(self, ex: dict, cap: int) -> None:
        if len(self.examples) < cap:
            self.examples.append(ex)


@dataclass
class CompareResult:
    pdf_id: str
    source_id: str
    ref_number: int | None
    raw_text: str
    ner: ParsedSource
    regex: ParsedSource
    title_sim: float
    journal_sim: float | None
    authors_jacc: float
    year_match: bool
    doi_match: bool


def compare_one(
    pdf_id: str,
    src: dict,
    ner: ParsedSource,
    regex: ParsedSource,
) -> CompareResult:
    title_sim = fuzz.token_set_ratio(
        _normalize_for_compare(ner.title or ""),
        _normalize_for_compare(regex.title or ""),
    )

    if ner.journal or regex.journal:
        journal_sim = fuzz.token_set_ratio(
            _normalize_for_compare(_strip_journal_tail(ner.journal or "")),
            _normalize_for_compare(_strip_journal_tail(regex.journal or "")),
        )
    else:
        journal_sim = None

    authors_jacc = _jaccard(_author_set(ner.authors), _author_set(regex.authors))

    year_match = ner.year == regex.year

    # DOI mismatch: both must be present for a real conflict; "regex missed
    # what NER found" is a divergence, but "NER missed what regex found"
    # is also worth surfacing. Equality on lowercased DOI.
    ner_doi = (ner.doi or "").lower() or None
    rgx_doi = (regex.doi or "").lower() or None
    doi_match = ner_doi == rgx_doi

    return CompareResult(
        pdf_id=pdf_id,
        source_id=src.get("id", ""),
        ref_number=src.get("ref_number"),
        raw_text=src.get("text", ""),
        ner=ner,
        regex=regex,
        title_sim=title_sim,
        journal_sim=journal_sim,
        authors_jacc=authors_jacc,
        year_match=year_match,
        doi_match=doi_match,
    )


def _format_example(r: CompareResult, field_name: str) -> dict:
    snippet = r.raw_text.replace("\n", " ").strip()
    if len(snippet) > 220:
        snippet = snippet[:220] + "..."
    base = {
        "pdf_id": r.pdf_id,
        "ref": r.ref_number,
        "source_id": r.source_id,
        "text": snippet,
    }
    if field_name == "title":
        base["sim"] = r.title_sim
        base["ner"] = r.ner.title or ""
        base["regex"] = r.regex.title or ""
    elif field_name == "journal":
        base["sim"] = r.journal_sim
        base["ner"] = r.ner.journal or ""
        base["regex"] = r.regex.journal or ""
    elif field_name == "authors":
        base["jacc"] = round(r.authors_jacc, 2)
        base["ner"] = r.ner.authors
        base["regex"] = r.regex.authors
    elif field_name == "year":
        base["ner"] = r.ner.year
        base["regex"] = r.regex.year
    elif field_name == "doi":
        base["ner"] = r.ner.doi
        base["regex"] = r.regex.doi
    return base


def _print_examples(stats: FieldStats) -> None:
    if not stats.examples:
        return
    print(f"\n  -- {stats.name} examples --")
    for ex in stats.examples:
        head = f"[{ex['pdf_id']} ref={ex['ref']}]"
        if "sim" in ex:
            head += f" sim={ex['sim']}"
        elif "jacc" in ex:
            head += f" jacc={ex['jacc']}"
        print(f"  {head}")
        print(f"    NER:   {ex['ner']!r}")
        print(f"    REGEX: {ex['regex']!r}")
        print(f"    TEXT:  {ex['text']}")


def _resolve_cache_dir(cli_arg: str | None) -> Path:
    if cli_arg:
        return Path(cli_arg)
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit(
            "APPDATA env var missing. Pass --cache-dir explicitly on non-Windows."
        )
    return Path(appdata) / "atfi-memnu-app" / "output" / "cache"


async def run(args: argparse.Namespace) -> int:
    cache_dir = _resolve_cache_dir(args.cache_dir)
    if not cache_dir.is_dir():
        print(f"[err] cache dir not found: {cache_dir}", file=sys.stderr)
        return 2

    cache_files = sorted(cache_dir.glob("*.json"))
    if args.pdfs:
        wanted = set(args.pdfs)
        cache_files = [p for p in cache_files if p.stem in wanted]
    print(f"[info] cache dir: {cache_dir}")
    print(f"[info] cache files: {len(cache_files)}")

    title_stats = FieldStats("title")
    journal_stats = FieldStats("journal")
    authors_stats = FieldStats("authors")
    year_stats = FieldStats("year")
    doi_stats = FieldStats("doi")

    processed = 0
    skipped_low_conf = 0
    skipped_ner_unavailable = 0
    skipped_empty = 0

    csv_writer = None
    csv_handle = None
    if args.csv:
        csv_handle = open(  # noqa: ASYNC230, SIM115
            args.csv, "w", newline="", encoding="utf-8"
        )
        csv_writer = csv.writer(csv_handle)
        csv_writer.writerow([
            "pdf_id", "ref", "source_id",
            "title_sim", "journal_sim", "authors_jacc", "year_match", "doi_match",
            "ner_title", "regex_title",
            "ner_authors", "regex_authors",
            "ner_journal", "regex_journal",
            "ner_year", "regex_year",
            "ner_doi", "regex_doi",
            "ner_conf",
            "raw_text",
        ])

    for cache_path in cache_files:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {cache_path.name}: {type(e).__name__}: {e}")
            continue

        pdf_id = cache.get("pdf_id") or cache_path.stem
        sources = cache.get("sources") or []

        for src in sources:
            if args.limit and processed >= args.limit:
                break
            text = src.get("text") or ""
            if not text.strip():
                skipped_empty += 1
                continue

            cleaned = strip_source_noise(text)

            ner = await extract_fields_ner(cleaned)
            if ner is None:
                skipped_ner_unavailable += 1
                continue
            if ner.parse_confidence < args.ner_conf_min:
                skipped_low_conf += 1
                continue

            regex = _extract_source_fields_regex(cleaned)
            r = compare_one(pdf_id, src, ner, regex)
            processed += 1

            if r.title_sim < TITLE_SIM_THRESHOLD:
                title_stats.diverged += 1
                title_stats.add_example(_format_example(r, "title"), args.examples)
            if r.journal_sim is not None and r.journal_sim < JOURNAL_SIM_THRESHOLD:
                journal_stats.diverged += 1
                journal_stats.add_example(_format_example(r, "journal"), args.examples)
            if r.authors_jacc < AUTHORS_JACCARD_THRESHOLD:
                authors_stats.diverged += 1
                authors_stats.add_example(_format_example(r, "authors"), args.examples)
            if not r.year_match:
                year_stats.diverged += 1
                year_stats.add_example(_format_example(r, "year"), args.examples)
            if not r.doi_match:
                doi_stats.diverged += 1
                doi_stats.add_example(_format_example(r, "doi"), args.examples)

            if csv_writer is not None:
                csv_writer.writerow([
                    pdf_id, r.ref_number, r.source_id,
                    r.title_sim, r.journal_sim, round(r.authors_jacc, 3),
                    r.year_match, r.doi_match,
                    ner.title, regex.title,
                    "; ".join(ner.authors), "; ".join(regex.authors),
                    ner.journal or "", regex.journal or "",
                    ner.year, regex.year,
                    ner.doi or "", regex.doi or "",
                    ner.parse_confidence,
                    text.replace("\n", " "),
                ])

        if args.limit and processed >= args.limit:
            break

    if csv_handle is not None:
        csv_handle.close()
        print(f"[info] csv written: {args.csv}")

    print()
    print(f"[summary] processed: {processed}")
    print(
        f"[summary] skipped: {skipped_low_conf} low NER conf, "
        f"{skipped_ner_unavailable} NER unavailable, {skipped_empty} empty"
    )
    if processed == 0:
        print("[warn] no entries processed — nothing to report.")
        return 0

    def pct(stats: FieldStats) -> str:
        return f"{stats.diverged}/{processed} ({100 * stats.diverged / processed:.1f}%)"

    print()
    print(f"  title    diverged (sim<{TITLE_SIM_THRESHOLD}):     {pct(title_stats)}")
    print(f"  journal  diverged (sim<{JOURNAL_SIM_THRESHOLD}):     {pct(journal_stats)}")
    print(f"  authors  diverged (jacc<{AUTHORS_JACCARD_THRESHOLD}):  {pct(authors_stats)}")
    print(f"  year     mismatched:           {pct(year_stats)}")
    print(f"  doi      mismatched:           {pct(doi_stats)}")

    _print_examples(title_stats)
    _print_examples(journal_stats)
    _print_examples(authors_stats)
    _print_examples(year_stats)
    _print_examples(doi_stats)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache-dir", help="Override approved-cache dir (defaults to %%APPDATA%%/atfi-memnu-app/output/cache)")
    parser.add_argument("--csv", help="Write full per-source comparison rows to this CSV path")
    parser.add_argument("--examples", type=int, default=10, help="Examples to print per pain field (default 10)")
    parser.add_argument("--ner-conf-min", type=float, default=0.3, help="Skip entries where NER conf < this (default 0.3)")
    parser.add_argument("--limit", type=int, default=0, help="Stop after this many processed entries (0 = no limit)")
    parser.add_argument("pdfs", nargs="*", help="Optional list of pdf_ids (cache filename stems) to restrict to")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
