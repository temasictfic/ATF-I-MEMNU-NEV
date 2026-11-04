"""Repair URLs that were split across PDF line wraps.

PDF text extraction inserts a literal space at every line break. When a URL
wraps a line the space lands inside the URL — almost always right after a
hyphen or slash. NER and regex extractors both happily keep that space,
producing dead URLs like `https://eur- lex.europa.eu/...`.

This module rejoins wrap-broken pieces without gluing genuine trailing prose
(`hdl.handle.net/2142/121447, Son`) onto the URL.
"""

import re

_URL_START = re.compile(r"https?://", re.IGNORECASE)
_URL_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%\-]+$")
_URL_SEPARATORS = re.compile(r"[./\-_]")
_TRAILING_PUNCT = ".,;:)]}\"'"


def clean_extracted_url(text: str | None) -> str | None:
    """Normalize a URL string that may contain PDF wrap-induced spaces."""
    if not text:
        return None
    m = _URL_START.search(text)
    if not m:
        return None
    tokens = text[m.start():].split()
    if not tokens:
        return None

    url = tokens[0]
    for tok in tokens[1:]:
        if not _URL_TOKEN_CHARS.fullmatch(tok):
            break
        # Don't glue a separate URL onto this one — that's two refs, not a wrap.
        if _URL_START.match(tok):
            break
        # Trailing "-" is almost always a wrap point — URLs rarely end on a
        # hyphen — so accept any URL-shaped continuation. For other ambiguous
        # endings (slash, query separators) require the continuation to
        # itself contain a URL separator, otherwise we'd glue prose like
        # "/ 23" onto the URL.
        if url.endswith("-"):
            url += tok
        elif url.endswith(("/", "_", "=", "?", "&")):
            if _URL_SEPARATORS.search(tok):
                url += tok
            else:
                break
        else:
            break

    url = url.rstrip(_TRAILING_PUNCT)
    return url or None


def find_first_url(text: str | None) -> str | None:
    """Find the first URL in free text, repairing wrap breaks."""
    if not text:
        return None
    m = _URL_START.search(text)
    if not m:
        return None
    return clean_extracted_url(text[m.start():])


def find_best_url(text: str | None) -> str | None:
    """Find the most specific URL in free text.

    A source often contains multiple URLs (a bare domain followed by a
    deeper link, or several archive copies). The longest one is almost
    always the most useful — bare domains contribute nothing the deeper
    link doesn't.
    """
    if not text:
        return None
    best: str | None = None
    pos = 0
    while True:
        m = _URL_START.search(text, pos)
        if not m:
            break
        cleaned = clean_extracted_url(text[m.start():])
        if cleaned and (best is None or len(cleaned) > len(best)):
            best = cleaned
        pos = m.end()
    return best
