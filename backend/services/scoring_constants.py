"""Score thresholds and weights shared across the backend.

The status-band thresholds at the top of this file are mirrored in
``src/renderer/src/lib/constants/scoring.ts`` (UI needs them to render
status pills). The scoring weights below are backend-only — the renderer
displays composite scores produced here but never recomputes them.

Bands:
- score >= STATUS_HIGH_THRESHOLD   → "high"   (UI: High / Yüksek)
- score >= STATUS_MEDIUM_THRESHOLD → "medium" (UI: Medium / Orta)
- otherwise                         → "low"    (UI: Low / Düşük)
"""

# --- Status-band thresholds (mirrored in scoring.ts) -----------------------

STATUS_HIGH_THRESHOLD = 0.75
STATUS_MEDIUM_THRESHOLD = 0.50
TITLE_MATCH_THRESHOLD = 0.85
DOI_MATCH_MIN_SCORE = 0.50

# --- Parse-confidence gates -------------------------------------------------
# A source's ``parse_confidence`` reflects how cleanly source-extraction
# pulled fields out of raw text. Two well-known cutoffs drive downstream
# behaviour:
#   < LOW  → NER fallback to regex extractor; orchestrator searches with
#            extracted title only; match_scorer drops the author signal
#            from the composite to avoid penalising on misparsed authors.
#   ≥ HIGH → crossref enables the disambiguating ``query.author`` filter;
#            below this, author tokens are too noisy to filter on.

LOW_PARSE_CONFIDENCE_THRESHOLD = 0.3
HIGH_PARSE_CONFIDENCE_THRESHOLD = 0.7

# --- Composite-score weights (the SCORING.md formula) ----------------------
# Composite = title·COMPOSITE_TITLE_WEIGHT + author·COMPOSITE_AUTHOR_WEIGHT
#             + Σ FIELD_MATCH_BONUS for {year, venue, doi/arXiv} that match
# Title sub-score blends order-insensitive token_sort with order-sensitive
# ratio so reordered titles still match but exact matches score higher.

COMPOSITE_TITLE_WEIGHT = 0.75
COMPOSITE_AUTHOR_WEIGHT = 0.25
FIELD_MATCH_BONUS = 0.10
TITLE_TOKEN_SORT_WEIGHT = 0.6
TITLE_SEQUENTIAL_WEIGHT = 0.4

# --- Per-signal sub-scores --------------------------------------------------
# Year sub-score: exact match scores full, ±1 year scores half (absorbs
# print-vs-online publication-date drift). Off by more than 1 → 0.

YEAR_EXACT_SCORE = 1.0
YEAR_OFF_BY_ONE_SCORE = 0.5

# Venue (journal/conference) match — minimum normalised fuzzy ratio for the
# !journal chip to clear. Set to 0.75 to align with title's strictness band:
# at 0.60 character-coincidence on shared technical word-stems (e.g.
# "computing" vs "computer") could push unrelated venues over the line.

VENUE_FUZZY_MATCH_THRESHOLD = 0.75
