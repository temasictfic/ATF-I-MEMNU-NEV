"""Robust author name matching for citation verification.

Handles format variations that the previous regex-based matcher missed:
  - IEEE vs Standard ("G. Liu" vs "Liu, G.")
  - Vancouver ("Smith JA") vs display name ("John Anthony Smith")
  - Accented / Turkish names (Öztürk, Müller, Şeyma, Yılmaz)
  - Multi-part surnames with particles (van der Berg, de la Cruz)
  - Mixed shapes between sources (Crossref "Family, Given" vs
    OpenAlex/Semantic Scholar "Given Family")

Public API:
  normalize_name(s)
  parse_author(raw) -> ParsedName
  authors_match(source_authors, candidate_authors) -> bool
  author_score(source_authors, candidate_authors) -> float
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

PARTICLES: frozenset[str] = frozenset({
    "van", "von", "de", "der", "den", "del", "della", "di", "da", "du",
    "le", "la", "el", "bin", "ibn", "af", "av", "zu", "ten", "ter",
})

_COMBINING_RE = re.compile(r"[\u0300-\u036f]")

# Letters NFKD does not decompose — applied before NFKD.
# Turkish dotted/dotless i collapse cleanly: İ/ı -> i.
_SPECIAL_MAP = str.maketrans({
    "ø": "o", "Ø": "o",
    "ł": "l", "Ł": "l",
    "ß": "ss",
    "æ": "ae", "Æ": "ae",
    "œ": "oe", "Œ": "oe",
    "đ": "d", "Đ": "d",
    "ı": "i", "İ": "i",
})

_NON_ALNUM_SPACE_RE = re.compile(r"[^0-9a-z\s]+")
_WS_RE = re.compile(r"\s+")

_INITIAL_TOKEN_RE = re.compile(r"[A-Za-zÇĞİÖŞÜçğıöşü]\.?")
_VANCOUVER_TAIL_RE = re.compile(r"[A-ZÇĞİÖŞÜ]{1,3}")
# Dotted initial cluster: "J.", "J.M.", "J.M", "JM." — at most 3 letters.
_DOTTED_INITIALS_RE = re.compile(r"(?:[A-ZÇĞİÖŞÜ]\.?){1,3}")
# Bare initials cluster without dots: "JM", "PR", "KJ" (2-3 uppercase).
_BARE_INITIALS_RE = re.compile(r"[A-ZÇĞİÖŞÜ]{2,3}")

# Google Scholar authors sometimes carry publication metadata after a
# non-breaking-space hyphen: "JM Keller\xa0- …\xa0learning and performance".
# This regex captures the separator so we can strip everything after it.
_GS_METADATA_RE = re.compile(r"[\u00a0\s]-\s.*$|\s-\s.*$", re.DOTALL)
_GS_TRAILING_ELLIPSIS_RE = re.compile(r"[\u2026…\.]+\s*$")
_GS_YEAR_ONLY_RE = re.compile(r"^\(?\s*(?:19|20)\d{2}[a-z]?\s*\)?$")
_GS_DIGITS_ONLY_RE = re.compile(r"^\d+$")


def clean_scholar_author(raw: str) -> str:
    """Strip Google Scholar publication-metadata leakage from an author
    string. Returns '' when the entry is not a plausible author name
    (pure year, digits-only, empty after cleanup).

    Examples:
        "JH Lee\xa0- Cells"                      -> "JH Lee"
        "S Niknazar…\xa0- Journal of Lasers in…" -> "S Niknazar"
        "2024"                                    -> ""
        "T Nairuz"                                -> "T Nairuz"
    """
    if not raw:
        return ""
    s = _GS_METADATA_RE.sub("", raw)
    s = _GS_TRAILING_ELLIPSIS_RE.sub("", s)
    s = s.strip().strip(".,;")
    if not s or _GS_YEAR_ONLY_RE.match(s) or _GS_DIGITS_ONLY_RE.match(s):
        return ""
    return s


def clean_scholar_authors(authors: list[str] | None) -> list[str]:
    """Clean a Google Scholar author list, dropping non-name entries."""
    if not authors:
        return []
    return [c for c in (clean_scholar_author(a) for a in authors) if c]


def _normalize_initial(ch: str) -> str:
    """Map a single letter to its ASCII-lowercase form (e.g. 'Ş' -> 's')."""
    if not ch:
        return ""
    n = normalize_name(ch)
    return n[0] if n else ""


def normalize_name(s: str) -> str:
    """NFKD-normalize, strip diacritics, lowercase, collapse whitespace.

    Examples:
        "Öztürk"    -> "ozturk"
        "Müller"    -> "muller"
        "Łukasz"    -> "lukasz"
        "Ş. Yılmaz" -> "s yilmaz"
    """
    if not s:
        return ""
    s = s.translate(_SPECIAL_MAP)
    s = unicodedata.normalize("NFKD", s)
    s = _COMBINING_RE.sub("", s)
    s = s.lower()
    s = _NON_ALNUM_SPACE_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


@dataclass(frozen=True)
class ParsedName:
    """Structured representation of an author name."""

    last: str
    first_initials: tuple[str, ...]
    given_full: str | None
    raw: str


def _initials_from_given(given_part: str) -> tuple[tuple[str, ...], str | None]:
    """From a given-name fragment, pull (initials, full-given-or-None)."""
    tokens = [t for t in re.split(r"[\s.]+", given_part.strip()) if t]
    initials: list[str] = []
    full_parts: list[str] = []
    for tok in tokens:
        first = tok[0]
        if first.isalpha():
            norm = _normalize_initial(first)
            if norm:
                initials.append(norm)
        if len(tok) > 1 and any(c.isalpha() for c in tok[1:]):
            full_parts.append(tok)
    full = normalize_name(" ".join(full_parts)) or None
    return tuple(initials), full


def parse_author(raw: str) -> ParsedName:
    """Parse an author string into a normalized ParsedName.

    Handles the common citation shapes. Falls back to a safe empty
    ParsedName when the input is unparseable.
    """
    if not raw:
        return ParsedName("", (), None, raw)

    # Google Scholar leaks publication metadata into author strings
    # ("JM Keller\xa0- …\xa0learning and performance"). Strip after the
    # separator and drop trailing ellipsis/et-al markers.
    s = _GS_METADATA_RE.sub("", raw)
    s = re.sub(r"[\u2026…]+\s*$", "", s)  # trailing unicode ellipsis
    s = s.strip().strip(".,;")
    if not s:
        return ParsedName("", (), None, raw)

    # Case A — comma form: "Last, First [Middle]" or "Last, F. M."
    if "," in s:
        last_part, given_part = s.split(",", 1)
        initials, given_full = _initials_from_given(given_part)
        return ParsedName(
            last=normalize_name(last_part),
            first_initials=initials,
            given_full=given_full,
            raw=raw,
        )

    tokens = s.split()
    if len(tokens) == 1:
        return ParsedName(normalize_name(tokens[0]), (), None, raw)

    # Case B — Vancouver-ish "Smith JA" / "Öztürk Ş" / "Poehlman J.M.":
    # last token is 1-3 uppercase letters, optionally dot-separated.
    if _VANCOUVER_TAIL_RE.fullmatch(tokens[-1]) or _DOTTED_INITIALS_RE.fullmatch(tokens[-1]):
        letters = [c for c in tokens[-1] if c.isalpha()]
        initials = tuple(
            i for i in (_normalize_initial(ch) for ch in letters) if i
        )
        last = normalize_name(" ".join(tokens[:-1]))
        return ParsedName(last, initials, None, raw)

    # Case B' — Google Scholar "JM Keller" / "PR Pintrich" / "KJ Räihä":
    # first token is 2-3 uppercase letters (bare initials, no dots),
    # remaining tokens are the surname.
    if _BARE_INITIALS_RE.fullmatch(tokens[0]):
        initials = tuple(
            i for i in (_normalize_initial(ch) for ch in tokens[0]) if i
        )
        last = normalize_name(" ".join(tokens[1:]))
        return ParsedName(last, initials, None, raw)

    # Case C — IEEE "G. Liu" / "K. Y. Lee":
    # one or more leading initial tokens, then the surname.
    def is_initial_token(tok: str) -> bool:
        return bool(_INITIAL_TOKEN_RE.fullmatch(tok))

    i = 0
    while i < len(tokens) - 1 and is_initial_token(tokens[i]):
        i += 1
    if i > 0:
        initials = tuple(
            x for x in (_normalize_initial(tokens[k][0]) for k in range(i)) if x
        )
        last = normalize_name(" ".join(tokens[i:]))
        return ParsedName(last, initials, None, raw)

    # Case D — display name "First [Middle] Last" with optional particles.
    # Walk from the right; attach particles (van, de, von, ...) to the surname.
    last_tokens = [tokens[-1]]
    j = len(tokens) - 2
    while j >= 0 and tokens[j].lower() in PARTICLES:
        last_tokens.insert(0, tokens[j])
        j -= 1
    given = tokens[: j + 1]
    last = normalize_name(" ".join(last_tokens))
    initials = tuple(
        x for x in (_normalize_initial(t[0]) for t in given if t and t[0].isalpha()) if x
    )
    given_full = normalize_name(" ".join(given)) or None
    return ParsedName(last, initials, given_full, raw)


def _last_names_match(a: str, b: str) -> bool:
    """Tiered surname comparison on already-normalized strings."""
    if not a or not b:
        return False
    if a == b:
        return True
    # Short surnames (min length <= 6) require a tighter fuzz.ratio: a single
    # edit on a 3-6 char name is a large fraction of the string and produces
    # false positives like Wang/Wan or Miyake/Miyatake.
    threshold = 90 if min(len(a), len(b)) <= 6 else 85
    if fuzz.ratio(a, b) >= threshold:
        return True
    # Multi-word surname salvage: handles "van der berg" vs "berg"
    # or "de la cruz" vs "cruz".
    if (" " in a or " " in b) and fuzz.token_set_ratio(a, b) >= 90:
        a_tokens = {t for t in a.split() if len(t) >= 3}
        b_tokens = {t for t in b.split() if len(t) >= 3}
        if a_tokens & b_tokens:
            return True
    return False


def _contains_whole_token(haystack: str | None, token: str) -> bool:
    """True when every whitespace-delimited word of `token` appears as a
    whole word in `haystack`. Handles multi-word surnames like
    `"bo cai"` matching a candidate's given_full `"bo cai"`.
    """
    if not haystack or not token:
        return False
    hay_words = set(haystack.split())
    tok_words = token.split()
    if not tok_words:
        return False
    return all(w in hay_words for w in tok_words)


def _name_pair_matches(s: ParsedName, c: ParsedName) -> bool:
    """True when two parsed names refer to the same person (best effort)."""
    last_ok = _last_names_match(s.last, c.last)
    compound_fallback = False
    # Compound-surname fallback: some APIs return the "primary" family
    # name only, with the rest of the surname in the given-name field
    # (e.g. Crossref "Sorkhabi, Majid Memarian" vs source "M. Memarian",
    #  or "Gao, Bo-cai" vs wrongly-cited "G. Bo-Cai").
    # Accept when one side's surname appears as a whole token (or token
    # sequence) in the other side's given-name string.
    if not last_ok and (
        _contains_whole_token(c.given_full, s.last)
        or _contains_whole_token(s.given_full, c.last)
    ):
        last_ok = True
        compound_fallback = True
    # Typo tolerance: accepted when both sides carry overlapping
    # initials (very unlikely to be a different person).  Two
    # flavors:
    #   1. Near-match: fuzz.ratio >= 78 ("Dewor" vs "Devor")
    #   2. Shared prefix >= 4 chars for longer names ("Akalin" vs
    #      "Akalm" — OCR error on Turkish diacritics)
    if (
        not last_ok
        and s.first_initials
        and c.first_initials
        and (set(s.first_initials) & set(c.first_initials))
        and (
            fuzz.ratio(s.last, c.last) >= 78
            or (
                len(s.last) >= 5
                and len(c.last) >= 5
                and s.last[:4] == c.last[:4]
            )
        )
    ):
        last_ok = True
    if not last_ok:
        return False
    # Initial disambiguation: skip when we matched via the compound-surname
    # fallback (the source and candidate clearly split the name differently,
    # so strict initial checks are noise).  Only apply when BOTH sides have
    # initials.
    if compound_fallback:
        return True
    return not (s.first_initials and c.first_initials and not set(s.first_initials) & set(c.first_initials))


def _parse_nonempty(authors: list[str]) -> list[ParsedName]:
    out: list[ParsedName] = []
    for a in authors:
        if not a or not a.strip():
            continue
        p = parse_author(a)
        if p.last:
            out.append(p)
    return out


def authors_match(source_authors: list[str], candidate_authors: list[str]) -> bool:
    """Verify source and candidate author lists are consistent.

    Matches against the SMALLER list so that truncated candidate records
    (e.g. book catalogs listing only the first author) don't falsely
    reject an otherwise-correct source.

      - Empty source -> vacuously True.
      - Empty candidate -> False.
      - smaller_len <= 2: every name on the smaller side must be found
        on the larger side.
      - smaller_len >  2: at least 50% of the smaller side must match.

    Initial disambiguation (rejecting Smith,J vs Smith,K) is applied
    only when there is a single surname match.  When >=2 surnames match,
    multiple co-author overlap makes coincidence near-impossible, so
    wrong initials from abbreviated citations are tolerated.
    """
    if not source_authors:
        return True
    if not candidate_authors:
        return False

    src = _parse_nonempty(source_authors)
    cand = _parse_nonempty(candidate_authors)
    if not src or not cand:
        return False

    if len(src) <= len(cand):
        smaller, larger = src, cand
    else:
        smaller, larger = cand, src

    # First pass: count surname-only matches (ignoring initials).
    surname_matches = 0
    for s in smaller:
        for c in larger:
            if _last_names_match(s.last, c.last):
                surname_matches += 1
                break

    # Decide whether to enforce initial disambiguation.
    # With >=2 surname matches the risk of coincidence is negligible;
    # wrong initials are far more likely extraction noise.
    use_initials = surname_matches < 2

    matched = 0
    for s in smaller:
        for c in larger:
            if use_initials:
                if _name_pair_matches(s, c):
                    matched += 1
                    break
            else:
                if _last_names_match(s.last, c.last):
                    matched += 1
                    break

    if len(smaller) <= 2:
        return matched == len(smaller)
    return matched / len(smaller) >= 0.5


def author_score(source_authors: list[str], candidate_authors: list[str]) -> float:
    """Fraction of source authors found in the candidate list (0.0-1.0)."""
    if not source_authors or not candidate_authors:
        return 0.0

    src = _parse_nonempty(source_authors)
    cand = _parse_nonempty(candidate_authors)
    if not src or not cand:
        return 0.0

    matched = 0
    for s in src:
        for c in cand:
            if _name_pair_matches(s, c):
                matched += 1
                break

    return matched / len(src)
