"""Standalone assert-based tests for services.author_matcher.

Run with: `python backend/tests/test_author_matcher.py` (or via the
backend venv). No pytest dependency — each test is a plain function
called from main() and prints pass/fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a script from repo root or backend/.
_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.author_matcher import (
    author_score,
    authors_match,
    normalize_name,
    parse_author,
)


def test_normalize_name_strips_diacritics() -> None:
    assert normalize_name("Müller") == "muller"
    assert normalize_name("Öztürk") == "ozturk"
    assert normalize_name("Şeyma") == "seyma"
    assert normalize_name("Yılmaz") == "yilmaz"
    assert normalize_name("Łukasz") == "lukasz"
    assert normalize_name("Ångström") == "angstrom"
    assert normalize_name("İstanbul") == "istanbul"


def test_parse_standard_comma_form() -> None:
    p = parse_author("Smith, John")
    assert p.last == "smith"
    assert p.first_initials == ("j",)
    assert p.given_full == "john"

    p2 = parse_author("Liu, G.")
    assert p2.last == "liu"
    assert p2.first_initials == ("g",)


def test_parse_ieee() -> None:
    p = parse_author("G. Liu")
    assert p.last == "liu"
    assert p.first_initials == ("g",)

    p2 = parse_author("K. Y. Lee")
    assert p2.last == "lee"
    assert p2.first_initials == ("k", "y")

    p3 = parse_author("Ş. Öztürk")
    assert p3.last == "ozturk", f"expected ozturk, got {p3.last!r}"
    assert p3.first_initials == ("s",)


def test_parse_vancouver() -> None:
    p = parse_author("Smith JA")
    assert p.last == "smith"
    assert p.first_initials == ("j", "a")

    p2 = parse_author("Öztürk Ş")
    assert p2.last == "ozturk"
    assert p2.first_initials == ("s",)


def test_parse_dotted_initials_after_surname() -> None:
    # "Poehlman J.M." — surname followed by dot-separated initials (no comma)
    p = parse_author("Poehlman J.M.")
    assert p.last == "poehlman", f"got {p.last!r}"
    assert p.first_initials == ("j", "m")

    p2 = parse_author("Sleper D.A.")
    assert p2.last == "sleper"
    assert p2.first_initials == ("d", "a")


def test_authors_match_poehlman_variants() -> None:
    # User-reported case: "Poehlman, J.M., Sleper, D.A" -> "Poehlman, John Milton"
    src = ["Poehlman, J.M.", "Sleper, D.A"]
    assert authors_match(src, ["Poehlman, John Milton", "Sleper, David A."])
    assert authors_match(src, ["John Milton Poehlman", "David A. Sleper"])
    assert authors_match(src, ["J. M. Poehlman", "D. A. Sleper"])
    # Truncated candidate (book catalog returns only the first author)
    assert authors_match(src, ["Poehlman, John Milton"])
    assert authors_match(src, ["John Milton Poehlman"])


def test_truncated_candidate_accepted_when_listed_author_matches() -> None:
    # A 2-author source vs 1-author truncated candidate: accept if the
    # one listed candidate author is present in source.
    assert authors_match(["Smith, John", "Jones, Kate"], ["Smith, John"])
    # But reject when the one listed candidate author is NOT in source.
    assert not authors_match(["Smith, John", "Jones, Kate"], ["Wang, Li"])


def test_parse_google_scholar_initials_prefix() -> None:
    # Google Scholar: "JM Keller" / "PR Pintrich" / "KJ Räihä"
    p = parse_author("JM Keller")
    assert p.last == "keller", f"got {p.last!r}"
    assert p.first_initials == ("j", "m")

    p2 = parse_author("PR Pintrich")
    assert p2.last == "pintrich"
    assert p2.first_initials == ("p", "r")

    p3 = parse_author("KJ Räihä")
    assert p3.last == "raiha"
    assert p3.first_initials == ("k", "j")


def test_parse_google_scholar_strips_metadata() -> None:
    # GS leaks publication info after "\xa0-" or " - "
    p = parse_author("JS Shamma\xa0- …\xa0Transactions on Control\xa0…")
    assert p.last == "shamma"
    assert p.first_initials == ("j", "s")

    p2 = parse_author("PB Marschik…\xa0- …\xa0für Psychologie")
    assert p2.last == "marschik"
    assert p2.first_initials == ("p", "b")


def test_authors_match_gs_vs_crossref() -> None:
    # Source: standard form; Candidate: Google Scholar shape
    assert authors_match(
        ["Keller, J. M."],
        ["JM Keller\xa0- …\xa0learning and performance"],
    )
    assert authors_match(
        ["Majaranta, P.", "Räihä, K.-J."],
        ["P Majaranta", "KJ Räihä\xa0- …\xa0symposium on Eye tracking"],
    )


def test_authors_match_compound_surname_split_by_api() -> None:
    # Some APIs return primary family + rest of compound in given name.
    # Source "M. Memarian" should match Crossref "Sorkhabi, Majid Memarian"
    # because "Memarian" appears as a whole word in the candidate's given.
    assert authors_match(
        ["M. Memarian", "M. Saadat"],
        ["Sorkhabi, Majid Memarian", "Saadat Khajeh, Maryam"],
    )


def test_relaxed_initial_disambiguation_when_multiple_surnames_match() -> None:
    # When >=2 surnames match, wrong initials are treated as extraction
    # noise (abbreviated citations often have wrong initials).
    # Source: "Goel, A., Agarwal, N." (wrong initials from truncated cite)
    # Candidate: "Mansi Goel, Ayush Agarwal, ..."
    assert authors_match(
        ["Goel, A.", "Agarwal, N."],
        ["Mansi Goel", "Ayush Agarwal", "Rishabh Gupta"],
    )


def test_extractor_vancouver_complete_no_overpairing() -> None:
    # Regression: "Savran A., Sankur B" shouldn't be paired into one entry
    from services.source_extractor import _parse_standard_authors
    out = _parse_standard_authors("Savran A., Sankur B")
    assert len(out) == 2, f"expected 2 authors, got {out}"
    assert authors_match(out, ["Savran, Arman", "Sankur, Bülent"])


def test_authors_match_compound_surname_wrongly_cited() -> None:
    # Source cited the name backwards ("G. Bo-Cai" instead of "B.-C. Gao").
    # Candidate is the correct "Gao, Bo-cai". Compound-surname fallback
    # via given_full, and initial disambiguation is skipped because the
    # source clearly mangled the family/given split.
    assert authors_match(["G. Bo-Cai"], ["Gao, Bo-cai"])


def test_authors_match_ocr_typo_with_matching_initials() -> None:
    # "Dewor" vs "Devor" — one-letter typo, both have A.W. initials
    assert authors_match(["Dewor, A.W"], ["Devor, A. W."])
    # "Akalin" vs "Akalm" — OCR on Turkish diacritic, prefix-based match
    assert authors_match(["F. Akalin"], ["Fatma Akalm"])
    # Safety: unrelated surnames with same initials should still fail
    assert not authors_match(["Smith, J"], ["Jones, J"])


def test_extractor_lowercase_particle_initials() -> None:
    # "A.-r." (Abdel-rahman), "A. u." (Aziz ul), "C. d. S." (Brazilian)
    from services.source_extractor import _parse_standard_authors
    out = _parse_standard_authors("Mohamed, A.-r., Jaitly, N., Senior, A.")
    assert out[0] == "Mohamed, A.-r."
    out2 = _parse_standard_authors("Rehman, A. u., Qureshi, S. A.")
    assert out2[0] == "Rehman, A. u."
    out3 = _parse_standard_authors("Pires, C. d. S., Marba, S. T. M.")
    assert out3[0] == "Pires, C. d. S."


def test_extractor_rule4_continues_on_surname_comma_initial() -> None:
    # Regression for 126E151_ref_37: "Hinton, G., Deng, L., ..." — the
    # first ". , " must NOT be treated as end of authors.
    from services.source_extractor import _extract_source_fields_regex
    raw = (
        '[37] Hinton, G., Deng, L., Yu, D., Dahl, G. E., Mohamed, A.-r., '
        'Jaitly, N., Senior, A., Vanhoucke, V., Nguyen, P., Sainath, T. N., '
        'et al., "Deep neural networks for acoustic modeling in speech '
        'recognition," Signal Processing Magazine 29(6), 82-97 (2012).'
    )
    src = _extract_source_fields_regex(raw)
    # Expect at least the 10 named authors (et al. is dropped).
    assert len(src.authors) >= 10, f"got {src.authors}"
    assert authors_match(
        src.authors,
        [
            "G. Hinton", "L. Deng", "Dong Yu", "George E. Dahl",
            "Abdel-rahman Mohamed", "N. Jaitly", "A. Senior",
            "Vincent Vanhoucke", "Patrick Nguyen", "Tara N. Sainath",
        ],
    )


def test_extractor_strips_parenthesized_trailing_year() -> None:
    # "F. AKALIN (2025)" — the parenthesized year must be stripped so
    # the author list resolves to just "F. AKALIN".
    from services.source_extractor import _extract_source_fields_regex
    raw = (
        'F. AKALIN (2025), "Detection and Classification of Heart Rhythms '
        'With Optimized MobileNetv2 Transfer Learning," Journal, 2025.'
    )
    src = _extract_source_fields_regex(raw)
    assert src.authors == ["F. AKALIN"], f"got {src.authors}"


def test_extractor_quoted_nickname_inside_authors() -> None:
    # 126E147_ref_141: "Um, E. \"Rachel\", Plass, J. L., ..." — the
    # embedded quoted nickname used to truncate authors or confuse
    # pairing. All 4 authors should be extracted and matched.
    from services.source_extractor import _extract_source_fields_regex
    raw = (
        'Um, E. "Rachel", Plass, J. L., Hayward, E. O., Homer, B. D. '
        '(2012). Emotional design in multimedia learning. Journal of '
        'Educational Psychology, 104(2), 485-498. doi:10.1037/a0026609'
    )
    src = _extract_source_fields_regex(raw)
    assert len(src.authors) == 4, f"expected 4 authors, got {src.authors}"
    cand = ['Um, Eunjoon "Rachel"', "Plass, Jan L.", "Hayward, Elizabeth O.", "Homer, Bruce D."]
    assert authors_match(src.authors, cand)


def test_parse_display_name_with_particles() -> None:
    p = parse_author("Johan Van Der Berg")
    assert p.last == "van der berg", f"got {p.last!r}"
    assert p.first_initials == ("j",)

    p2 = parse_author("Ludwig von Mises")
    assert p2.last == "von mises"
    assert p2.first_initials == ("l",)


def test_authors_match_ieee_vs_standard() -> None:
    assert authors_match(
        ["G. Liu", "K. Y. Lee"],
        ["Liu, Gang", "Lee, Kwang Yong"],
    )


def test_authors_match_vancouver_vs_full() -> None:
    assert authors_match(["Smith JA"], ["John Anthony Smith"])


def test_authors_match_accent_insensitive() -> None:
    assert authors_match(["Müller K."], ["Klaus Muller"])
    assert authors_match(["Şeyma Öztürk"], ["S. Ozturk"])


def test_authors_match_multipart_surname() -> None:
    assert authors_match(["Van Der Berg, J."], ["Johan van der Berg"])


def test_same_surname_different_initials_rejected() -> None:
    assert not authors_match(["Smith, J."], ["Smith, K."])


def test_same_surname_one_side_no_initials_accepted() -> None:
    assert authors_match(["Smith"], ["Smith, K."])


def test_strict_when_two_or_fewer_authors() -> None:
    assert not authors_match(
        ["Smith, J.", "Jones, K."],
        ["Smith, John", "Totally Different"],
    )


def test_relaxed_when_more_than_two() -> None:
    assert authors_match(
        ["Smith, J.", "Jones, K.", "Doe, X.", "Roe, Y."],
        ["John Smith", "Kate Jones", "Unrelated Person"],
    )


def test_empty_source_returns_true() -> None:
    assert authors_match([], ["anyone"])


def test_empty_candidate_returns_false() -> None:
    assert not authors_match(["Smith, J."], [])


def test_author_score_range() -> None:
    assert author_score([], []) == 0.0
    assert author_score(["Smith, J."], ["John Smith"]) == 1.0
    score = author_score(
        ["Smith, J.", "Jones, K."],
        ["John Smith", "Unrelated"],
    )
    assert score == 0.5, f"expected 0.5, got {score}"


def _run(tests: list) -> int:
    failed = 0
    for t in tests:
        name = t.__name__
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(e).__name__}: {e}")
        else:
            print(f"ok   {name}")
    return failed


if __name__ == "__main__":
    tests = [
        test_normalize_name_strips_diacritics,
        test_parse_standard_comma_form,
        test_parse_ieee,
        test_parse_vancouver,
        test_parse_dotted_initials_after_surname,
        test_authors_match_poehlman_variants,
        test_truncated_candidate_accepted_when_listed_author_matches,
        test_parse_google_scholar_initials_prefix,
        test_parse_google_scholar_strips_metadata,
        test_authors_match_gs_vs_crossref,
        test_authors_match_compound_surname_split_by_api,
        test_relaxed_initial_disambiguation_when_multiple_surnames_match,
        test_extractor_vancouver_complete_no_overpairing,
        test_authors_match_compound_surname_wrongly_cited,
        test_authors_match_ocr_typo_with_matching_initials,
        test_extractor_lowercase_particle_initials,
        test_extractor_rule4_continues_on_surname_comma_initial,
        test_extractor_strips_parenthesized_trailing_year,
        test_extractor_quoted_nickname_inside_authors,
        test_parse_display_name_with_particles,
        test_authors_match_ieee_vs_standard,
        test_authors_match_vancouver_vs_full,
        test_authors_match_accent_insensitive,
        test_authors_match_multipart_surname,
        test_same_surname_different_initials_rejected,
        test_same_surname_one_side_no_initials_accepted,
        test_strict_when_two_or_fewer_authors,
        test_relaxed_when_more_than_two,
        test_empty_source_returns_true,
        test_empty_candidate_returns_false,
        test_author_score_range,
    ]
    failures = _run(tests)
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
