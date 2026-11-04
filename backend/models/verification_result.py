from typing import Literal

from pydantic import BaseModel

# Verifier-assigned outcome of a source. "pending"/"in_progress" are
# transient (orchestrator lifecycle); "high"/"medium"/"low" are the three
# settled bands set by `determine_verification_status`.
VerifyStatus = Literal["pending", "in_progress", "high", "medium", "low"]

# Three-state outcome from `classify_decision`. The same set is also the
# allowed values for the user override (with `None` meaning "clear override").
DecisionTag = Literal["valid", "citation", "fabricated"]

# Per-tag override keys for the verification card chips.
TagKey = Literal["authors", "year", "title", "journal", "doi/arXiv"]


class MatchDetails(BaseModel):
    title_similarity: float = 0.0
    author_match: float = 0.0
    year_match: float = 0.0
    journal_similarity: float = 0.0
    doi_arxiv_similarity: float = 0.0
    url_match: bool = False  # unified: doi, arXiv, or other URL matches


class MatchResult(BaseModel):
    database: str
    title: str = ""
    authors: list[str] = []
    year: int | None = None
    doi: str | None = None
    journal: str = ""
    url: str = ""
    search_url: str = ""
    score: float = 0.0
    # Unclamped composite (base + bonus). Identical to ``score`` whenever the
    # sum stays in [0, 1]; only diverges (>1.0) when bonuses push it over the
    # cap. Used to rank "best match" so two saturated 1.00 candidates can
    # still be ordered by underlying signal strength.
    raw_score: float = 0.0
    match_details: MatchDetails = MatchDetails()
    # Bibliographic extras — populated by verifiers when the underlying API
    # returns the field. Display-only; never used in scoring.
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    publisher: str = ""
    editor: list[str] = []
    document_type: str = ""  # article / book / chapter / thesis / preprint / report
    language: str = ""  # ISO 639-1
    issn: list[str] = []
    isbn: list[str] = []


class VerificationResult(BaseModel):
    source_id: str
    status: VerifyStatus = "pending"
    # Problem tags — values:
    # "!authors", "!doi/arXiv", "!year", "!journal", "!title"
    problem_tags: list[str] = []
    decision_tag: DecisionTag = "valid"
    # Three-state user override; None = use classify_decision() result.
    # Cycled from the UI via POST /api/verify/decision-override.
    decision_tag_override: DecisionTag | None = None
    # Per-tag user overrides for the card's clickable chips.
    # true = force ON, false = force OFF, missing key = use default logic.
    tag_overrides: dict[TagKey, bool] = {}
    # URL -> liveness map (for non-doi/arXiv URLs that were checked)
    url_liveness: dict[str, bool] = {}
    best_match: MatchResult | None = None
    all_results: list[MatchResult] = []
    databases_searched: list[str] = []
    # NER-extracted title — used to derive scholar/google search URLs and
    # per-database manual-search links on cache load (those fields are
    # stripped from disk).
    parsed_title: str = ""
    # Pre-built Google Scholar / Google Search URLs using the NER-extracted title
    scholar_url: str = ""
    google_url: str = ""
