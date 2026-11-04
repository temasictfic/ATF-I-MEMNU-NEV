from config import settings as app_config
from pydantic import BaseModel


DEFAULT_EXCLUSION_LIST_URL = (
    "https://docs.google.com/spreadsheets/d/e/2PACX-1vS-DWUY4jAnn2b-DSVeLw7-kVT3pE77ZP-lnP47yt73WojeJJuUTmUPDoxLQQ3lIc0kvdUV8c87fWAY/pub?gid=355872624&single=true&output=tsv"
)
DEFAULT_EXCLUSION_EDIT_URL = (
    "https://docs.google.com/spreadsheets/d/1-d9yoAdZJAD64XZOIPy2vMc314s9jsUGVwZV5uqnx1E/edit?usp=sharing"
)


class DatabaseConfig(BaseModel):
    id: str
    name: str
    enabled: bool = True


class AppSettings(BaseModel):
    # Folder that Parsing-page exports and Verification-page reports are
    # written to. Blank means "prompt me each time" via the renderer's
    # native file-save dialog.
    exported_pdf_dir: str = ""
    # The Electron main process fetches this URL on startup
    # and caches the result locally for offline use.
    exclusion_list_url: str = DEFAULT_EXCLUSION_LIST_URL
    exclusion_edit_url: str = DEFAULT_EXCLUSION_EDIT_URL
    databases: list[DatabaseConfig] = []
    api_keys: dict[str, str] = {}
    # Contact email advertised to Crossref / arXiv / OpenAlex polite pools.
    # Blank = stay in the anonymous public pool (stricter rate limits).
    polite_pool_email: str = ""
    # ISO-8601 date (UTC) when the user's OpenAIRE refresh token in
    # `api_keys["openaire"]` was last written. Refresh tokens expire 1 month
    # after issuance, so the UI uses this to warn the user before it dies.
    openaire_token_saved_at: str = ""
    # Persisted only — read by the renderer (i18n.changeLanguage). The backend
    # never consults this; all backend logs/messages are English.
    language: str = "tr"
    search_timeout: int = app_config.search_timeout
    max_concurrent_apis: int = app_config.max_concurrent_apis
    max_concurrent_sources_per_pdf: int = app_config.max_concurrent_sources_per_pdf
    # Persisted only — frontend-driven. The renderer decides whether to
    # auto-open Scholar after a verification finishes; backend has no knob.
    auto_scholar_after_verify: bool = True
    # When True, the verification orchestrator cancels still-pending Tier 1
    # verifiers as soon as one of them returns a saturated composite score
    # (>= 1.0) with a plausible title. Off → every enabled DB is always
    # queried, even when an early result already proves the citation.
    strong_match_enabled: bool = False
    # Persisted only — frontend-driven. The renderer decides whether to render
    # the "Bibliographic details" block in best-match cards on the verification
    # report PDF.
    report_include_bibliographic: bool = True
    # Callout text used by the Parsing page's per-decision-tag auto-annotate
    # buttons. Persisted so a user's edits survive app restarts.
    auto_callout_text_fabricated: str = "Literatürde bulunmamaktadır."
    auto_callout_text_citation: str = (
        "Künye bilgilerinde eksik/hatalı bilgiler bulunmaktadır."
    )

    @classmethod
    def default(cls) -> "AppSettings":
        return cls(
            exclusion_list_url=DEFAULT_EXCLUSION_LIST_URL,
            exclusion_edit_url=DEFAULT_EXCLUSION_EDIT_URL,
            databases=[
                DatabaseConfig(id="crossref", name="Crossref"),
                DatabaseConfig(id="openalex", name="OpenAlex"),
                DatabaseConfig(id="openaire", name="OpenAIRE"),
                DatabaseConfig(id="europe_pmc", name="Europe PMC"),
                DatabaseConfig(id="arxiv", name="arXiv"),
                DatabaseConfig(id="pubmed", name="PubMed"),
                DatabaseConfig(id="semantic_scholar", name="Semantic Scholar"),
                DatabaseConfig(id="trdizin", name="TRDizin"),
                DatabaseConfig(id="open_library", name="Open Library"),
                # BASE requires IP allowlist via base-search.net/about/en/contact.php
                # ("Access BASE's HTTP API"), so it's off by default. Users opt in
                # from Settings once they have allowlist access.
                DatabaseConfig(id="base", name="BASE", enabled=False),
                # Web of Science requires a Clarivate API key (Starter free token
                # or Expanded institutional key from developer.clarivate.com), so
                # off by default — same opt-in pattern as BASE.
                DatabaseConfig(id="wos", name="Web of Science", enabled=False),
            ],
            search_timeout=app_config.search_timeout,
            max_concurrent_apis=app_config.max_concurrent_apis,
            max_concurrent_sources_per_pdf=app_config.max_concurrent_sources_per_pdf,
        )
