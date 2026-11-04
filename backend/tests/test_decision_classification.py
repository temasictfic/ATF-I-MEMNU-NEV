"""Regression tests for verification decision classification.

Run with: `python backend/tests/test_decision_classification.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from models.source import ParsedSource
from models.verification_result import MatchDetails, MatchResult
from services.match_scorer import (
    TITLE_MATCH_THRESHOLD,
    classify_decision,
    determine_verification_status,
)


def _source(year: int | None) -> ParsedSource:
    return ParsedSource(
        raw_text="Ada Lovelace. Computing Notes.",
        title="Computing Notes",
        authors=["Lovelace, Ada"],
        year=year,
        journal="Analytical Journal",
    )


def _match(year: int | None) -> MatchResult:
    return MatchResult(
        database="Test database",
        title="Computing Notes",
        authors=["Ada Lovelace"],
        year=year,
        journal="Analytical Journal",
        score=1.0,
        raw_score=1.0,
        match_details=MatchDetails(title_similarity=TITLE_MATCH_THRESHOLD),
    )


def test_year_variations_do_not_change_a_valid_decision() -> None:
    for source_year, match_year in (
        (2024, 2024),  # matching years
        (2024, 2020),  # mismatching years
        (2024, None),  # candidate year missing
        (None, 2024),  # source year missing
        (None, None),  # both years missing
    ):
        assert classify_decision(_source(source_year), _match(match_year)) == "valid"


def test_year_only_mismatch_remains_visible_as_a_problem_chip() -> None:
    source = _source(2024)
    match = _match(2020)

    assert classify_decision(source, match) == "valid"
    _, problem_tags = determine_verification_status(source, match)
    assert problem_tags == ["!year"]


if __name__ == "__main__":
    test_year_variations_do_not_change_a_valid_decision()
    test_year_only_mismatch_remains_visible_as_a_problem_chip()
    print("All decision classification tests passed.")
