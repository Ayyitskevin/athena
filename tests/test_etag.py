"""HTTP validator parsing is strict, bounded, and strong-comparison safe."""

import math

import pytest

from athena.core.etag import (
    MAX_IF_MATCH_BYTES,
    MAX_IF_MATCH_TAGS,
    IfMatchTooLarge,
    InvalidIfMatch,
    parse_if_match,
    strong_etag,
)


def test_strong_etag_is_quoted_deterministic_and_canonical():
    first = strong_etag("issue-v1", {"title": "Café", "id": 7})
    reordered = strong_etag("issue-v1", {"id": 7, "title": "Café"})

    assert first == reordered
    assert first.startswith('"sha256-') and first.endswith('"')
    assert len(first.removeprefix('"sha256-').removesuffix('"')) == 64
    assert not first.startswith('W/"')


def test_strong_etag_frames_namespace_and_representation():
    baseline = strong_etag("issue-v1", {"id": 7, "status": "open"})

    assert strong_etag("issue-v2", {"id": 7, "status": "open"}) != baseline
    assert strong_etag("issue-v1", {"id": 7, "status": "done"}) != baseline
    assert strong_etag("ab", "c") != strong_etag("a", "bc")


def test_strong_etag_rejects_non_json_and_non_finite_values():
    with pytest.raises(TypeError):
        strong_etag("issue-v1", object())
    with pytest.raises(ValueError):
        strong_etag("issue-v1", {"score": math.nan})
    with pytest.raises(TypeError):
        strong_etag(7, {"id": 7})  # type: ignore[arg-type]


def test_wildcard_accepts_ows_and_matches_any_existing_representation():
    condition = parse_if_match(["\t *  "])

    assert condition.wildcard is True
    assert condition.matches('"anything"')


def test_strong_list_matches_any_exact_opaque_tag():
    condition = parse_if_match([' "other" , "current" , "third" '])

    assert condition.wildcard is False
    assert condition.matches('"current"')
    assert not condition.matches('"CURRENT"')
    assert not condition.matches('"missing"')


def test_repeated_fields_and_ows_form_one_list():
    condition = parse_if_match(['\t"first" ', ' W/"ignored"\t', '"last"'])

    assert condition.matches('"first"')
    assert condition.matches('"last"')
    assert not condition.matches('"ignored"')


def test_weak_tags_are_valid_but_never_strong_match():
    weak_only = parse_if_match(['W/"same"'])
    mixed = parse_if_match(['W/"same", "same"'])

    assert not weak_only.matches('"same"')
    assert not weak_only.matches('W/"same"')
    assert mixed.matches('"same"')


def test_commas_and_backslashes_inside_opaque_tags_are_not_list_separators():
    condition = parse_if_match(['"one,two\\three", "other"'])

    assert condition.matches('"one,two\\three"')
    assert condition.matches('"other"')


def test_empty_opaque_tag_and_obs_text_are_valid():
    condition = parse_if_match(['"", "caf\u00e9"'])

    assert condition.matches('""')
    assert condition.matches('"caf\u00e9"')


def test_empty_list_members_are_ignored_within_the_existing_bounds():
    condition = parse_if_match(["", ' , "tag", , ', ""])

    assert condition.matches('"tag"')
    assert not parse_if_match(["", "  "]).matches('"anything"')

    with pytest.raises(IfMatchTooLarge, match="list members"):
        parse_if_match(["," * (MAX_IF_MATCH_TAGS + 1)])


@pytest.mark.parametrize(
    "values",
    [
        [],
        ["bare"],
        ['"unterminated'],
        ['"tag" trailing'],
        ['*, "tag"'],
        ['"tag", *'],
        ['w/"tag"'],
        ['"has space"'],
        ['"has\ttab"'],
        ['"has\nnewline"'],
        ['"has"quote"'],
        ['"snowman \u2603"'],
    ],
)
def test_malformed_or_empty_conditions_are_rejected(values):
    with pytest.raises(InvalidIfMatch):
        parse_if_match(values)


def test_total_received_value_bytes_are_bounded_before_syntax_work():
    oversized = "x" * (MAX_IF_MATCH_BYTES + 1)

    with pytest.raises(IfMatchTooLarge, match="bytes"):
        parse_if_match([oversized])


def test_combining_repeated_fields_counts_inserted_separator_bytes():
    values = ["x" * (MAX_IF_MATCH_BYTES // 2)] * 2

    with pytest.raises(IfMatchTooLarge, match="bytes"):
        parse_if_match(values)


def test_exact_byte_limit_is_not_rejected_as_too_large():
    value = '"' + ("x" * (MAX_IF_MATCH_BYTES - 2)) + '"'

    assert parse_if_match([value]).matches(value)


def test_entity_tag_count_is_bounded_and_duplicates_still_count():
    at_limit = ",".join(f'"{index}"' for index in range(MAX_IF_MATCH_TAGS))
    too_many = ",".join('"same"' for _ in range(MAX_IF_MATCH_TAGS + 1))

    assert parse_if_match([at_limit]).matches('"63"')
    with pytest.raises(IfMatchTooLarge, match="entity tags"):
        parse_if_match([too_many])


def test_non_string_values_are_rejected_without_an_incidental_type_error():
    with pytest.raises(InvalidIfMatch, match="strings"):
        parse_if_match(['"ok"', 7])  # type: ignore[list-item]
