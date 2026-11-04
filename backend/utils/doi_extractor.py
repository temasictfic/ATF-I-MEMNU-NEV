"""Extract and normalize DOIs from source text."""

import re

DOI_BODY_PATTERN = r"10\.\d{4,9}/[A-Za-z0-9._;()/:+\-]+"
_DOI_WRAP_TAIL = re.compile(r"\s+([A-Za-z0-9._;()/:+\-]+)")

DOI_PATTERNS = [
    # Kurallar DOI Rule 4: "doi:" or "doi.org" format
    re.compile(rf"doi[:\s]+\s*({DOI_BODY_PATTERN})", re.IGNORECASE),
    # Kurallar DOI Rule 4: URL format
    re.compile(rf"https?://(?:dx\.)?doi\.org/({DOI_BODY_PATTERN})", re.IGNORECASE),
    # Kurallar DOI Rule 1-2: bare DOI pattern (linked or numbered)
    re.compile(rf"({DOI_BODY_PATTERN})"),
]

# Allow optional whitespace inside the ID body so wrap-broken arXiv IDs
# (`2403. 12345`) still match. Whitespace is stripped after capture.
ARXIV_PATTERNS = [
    re.compile(r"10\.48550/arxiv\.(\d{4}\s*\.\s*\d{4,5}(?:v\d+)?)", re.IGNORECASE),
    re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\s*\.\s*\d{4,5}(?:v\d+)?)", re.IGNORECASE),
    re.compile(r"arXiv[:\s]*(\d{4}\s*\.\s*\d{4,5}(?:v\d+)?)", re.IGNORECASE),
]


def extract_doi(text: str) -> str | None:
    """Extract a DOI from text, if present.

    Rejoins one wrap-broken tail when the DOI body ends with `-` or `/`
    (the chars where PDF line wraps are commonly hidden). Avoids the
    greedier behavior of an unconditional `(\\s+\\w+)*` continuation,
    which used to glue prose like ". Foo" into the DOI.
    """
    if not text:
        return None
    for pattern in DOI_PATTERNS:
        match = pattern.search(text)
        if match:
            doi_body = match.group(1)
            if doi_body.endswith(("-", "/")):
                tail = _DOI_WRAP_TAIL.match(text, match.end(1))
                if tail:
                    doi_body += tail.group(1)
            return normalize_doi(doi_body)
    return None


def normalize_doi(doi: str) -> str:
    """Normalize DOI for consistent comparison."""
    doi = doi.strip().lower()
    # Remove URL prefix
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    # Remove "doi:" prefix
    doi = re.sub(r"^doi:\s*", "", doi)
    # Remove accidental whitespace within DOI body (OCR/wrapped text)
    doi = re.sub(r"\s+", "", doi)
    # Strip trailing punctuation
    doi = doi.rstrip(".,;:)]}\"'")
    return doi


def extract_arxiv_id(text: str) -> str | None:
    """Extract arXiv ID from URL or text."""
    for pattern in ARXIV_PATTERNS:
        match = pattern.search(text)
        if match:
            return re.sub(r"\s+", "", match.group(1))
    return None
