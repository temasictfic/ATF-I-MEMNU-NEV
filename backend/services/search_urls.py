"""Search-URL templates per database.

Single source of truth for the manual "search again on X" URLs that appear
on each MatchResult, plus the Google Scholar / Google Search URLs derived
from the NER-extracted title. Used both by the verification orchestrator
(to fill in fallback URLs) and by the verify-cache loader (to rebuild
search_url, scholar_url, google_url which are stripped from disk).

Templates use Python's ``str.format`` with a single ``{q}`` placeholder.
Encoding mirrors the convention historically used by each verifier:
``quote_plus`` for query-style endpoints that expect ``+`` between words
(Crossref, Google), ``quote`` for path/query endpoints that prefer
``%20`` (everyone else).
"""

from urllib.parse import quote, quote_plus

# Templates use Python str.format with {q}. Two encoding styles → two tables.
_TEMPLATES_PLUS = {
    "Crossref":       "https://search.crossref.org/search/works?q={q}&from_ui=yes",
    "Google Scholar": "https://scholar.google.com/scholar?q={q}",
    "Google Search":  "https://www.google.com/search?q={q}",
}

_TEMPLATES_PCT = {
    "OpenAlex":         "https://openalex.org/works?search={q}",
    "arXiv":            "https://arxiv.org/search/?query={q}&searchtype=all",
    "Semantic Scholar": "https://www.semanticscholar.org/search?q={q}",
    "Europe PMC":       "https://europepmc.org/search?query={q}",
    "TRDizin":          "https://search.trdizin.gov.tr/tr/yayin/ara?q={q}&order=relevance-DESC&page=1&limit=20",
    "PubMed":           "https://pubmed.ncbi.nlm.nih.gov/?term={q}",
    "OpenAIRE":         "https://explore.openaire.eu/search/find?fv0={q}&f0=q",
    "Open Library":     "https://openlibrary.org/search?q={q}",
    "BASE":             "https://www.base-search.net/Search/Results?lookfor={q}",
    "Web of Science":   "https://www.webofscience.com/wos/alldb/basic-search?q={q}",
}


def build_search_url(database: str, title: str) -> str:
    """Return the manual-search URL for ``database`` querying ``title``.

    Returns "" when title is empty or the database is unknown.
    """
    if not title:
        return ""
    if database in _TEMPLATES_PLUS:
        return _TEMPLATES_PLUS[database].format(q=quote_plus(title))
    if database in _TEMPLATES_PCT:
        # TRDizin allows commas in its query
        if database == "TRDizin":
            return _TEMPLATES_PCT[database].format(q=quote(title, safe=","))
        return _TEMPLATES_PCT[database].format(q=quote(title))
    return ""


def build_google_urls(title: str) -> tuple[str, str]:
    """Return (scholar_url, google_url) for the given title.

    Both endpoints use ``quote_plus`` → ``+`` between words.
    """
    if not title:
        return "", ""
    return (
        build_search_url("Google Scholar", title),
        build_search_url("Google Search", title),
    )
