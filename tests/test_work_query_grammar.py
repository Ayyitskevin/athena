"""The work query grammar, parsed in isolation.

`core/work_query.py` is pure — no database, no domain imports — so these tests
need no fixtures and run in milliseconds. That is the point of the split: the
grammar's edge cases (quoting, negation, closed vocabularies, limits) are pinned
here, and the semantic tests in test_work_query.py can concentrate on what the
atoms MEAN against real rows.

The load-bearing rule is the last section: an unknown atom is an error, never an
empty result. A query box that answers "no results" to a typo has invented an
answer, which is the one thing the steering rules forbid outright.
"""

import pytest

from athena.core import work_query
from athena.core.work_query import QueryError, Term


def test_an_empty_query_means_everything():
    for raw in (None, "", "   "):
        parsed = work_query.parse(raw)
        assert parsed.terms == ()
        assert parsed.text == ""
        assert parsed.sort == work_query.DEFAULT_SORT


def test_atoms_parse_into_terms_and_bare_words_into_text():
    parsed = work_query.parse("is:open label:infra payment retry")
    assert parsed.terms == (
        Term("is", "open"),
        Term("label", "infra"),
    )
    assert parsed.text == "payment retry"


def test_quoted_phrases_survive_as_one_text_blob():
    parsed = work_query.parse('label:infra "payment retry" timeout')
    assert parsed.of("label") == ("infra",)
    assert parsed.text == "payment retry timeout"


def test_an_unterminated_quote_runs_to_the_end_instead_of_raising():
    # Every search box on the internet behaves this way; shlex would raise.
    assert work_query.parse('"payment retry').text == "payment retry"


def test_negation_marks_the_term_without_changing_its_field():
    parsed = work_query.parse("-label:noise label:infra")
    assert parsed.of("label") == ("infra",)
    assert parsed.of("label", negated=True) == ("noise",)


def test_a_bare_hyphen_word_is_text_not_a_broken_negation():
    # Refusing this would fail any query containing a hyphenated word.
    parsed = work_query.parse("well-known -")
    assert parsed.terms == ()
    assert "well-known" in parsed.text


def test_fields_are_case_insensitive_but_open_values_are_not():
    parsed = work_query.parse("IS:OPEN Label:Infra")
    assert parsed.of("is") == ("open",)
    # A label name is data, not vocabulary: case is preserved for the domain to
    # resolve (labels match COLLATE NOCASE there, but that is the domain's rule).
    assert parsed.of("label") == ("Infra",)


def test_repeated_fields_all_survive_for_the_compiler_to_and_together():
    parsed = work_query.parse("label:a label:b")
    assert parsed.of("label") == ("a", "b")


def test_sort_is_extracted_and_is_not_a_term():
    parsed = work_query.parse("is:open sort:priority-desc")
    assert parsed.sort == "priority-desc"
    assert parsed.terms == (Term("is", "open"),)


def test_the_grammar_refuses_to_guess_between_two_sorts():
    with pytest.raises(QueryError) as caught:
        work_query.parse("sort:id-asc sort:created-desc")
    assert "only one sort" in str(caught.value)
    # Repeating the SAME sort is harmless and accepted.
    assert work_query.parse("sort:id-asc sort:id-asc").sort == "id-asc"


def test_sort_cannot_be_negated():
    with pytest.raises(QueryError):
        work_query.parse("-sort:id-asc")


# --- the rule that matters: refuse, never silently match nothing ------------


def test_an_unknown_field_names_itself_in_the_error():
    with pytest.raises(QueryError) as caught:
        work_query.parse("asignee:@me")
    assert caught.value.atom == "asignee"
    assert "asignee" in str(caught.value)


def test_a_bad_closed_value_lists_what_would_have_worked():
    with pytest.raises(QueryError) as caught:
        work_query.parse("is:opne")
    assert caught.value.atom == "is:opne"
    message = str(caught.value)
    assert all(f"is:{v}" in message for v in work_query.IS_VALUES)


def test_a_field_with_no_value_is_refused():
    with pytest.raises(QueryError) as caught:
        work_query.parse("label:")
    assert caught.value.atom == "label"


@pytest.mark.parametrize("field", work_query.OPEN_FIELDS)
def test_every_open_field_accepts_an_arbitrary_value(field):
    # An unknown label/status/project is a real filter that matches nothing — the
    # long-standing contract. Only the CLOSED vocabularies refuse a bad value.
    assert work_query.parse(f"{field}:whatever").of(field) == ("whatever",)


@pytest.mark.parametrize("value", work_query.IS_VALUES)
def test_every_documented_is_value_parses(value):
    assert work_query.parse(f"is:{value}").of("is") == (value,)


@pytest.mark.parametrize("value", work_query.HAS_VALUES)
def test_every_documented_has_value_parses(value):
    assert work_query.parse(f"has:{value}").of("has") == (value,)


@pytest.mark.parametrize("value", work_query.SORT_VALUES)
def test_every_documented_sort_value_parses(value):
    assert work_query.parse(f"sort:{value}").sort == value


def test_sort_deliberately_has_no_updated_key():
    """The issues table has no updated_at column. A sort key that silently fell
    back to something else would be a lie about ordering, so the gap is recorded
    rather than faked."""
    assert not any(v.startswith("updated") for v in work_query.SORT_VALUES)


# --- bounds -----------------------------------------------------------------


def test_an_oversized_query_is_refused():
    with pytest.raises(QueryError):
        work_query.parse("x" * (work_query.MAX_QUERY_CHARS + 1))


def test_too_many_terms_is_refused():
    raw = " ".join(f"label:l{n}" for n in range(work_query.MAX_TERMS + 5))
    with pytest.raises(QueryError):
        work_query.parse(raw)


@pytest.mark.parametrize(
    "garbage",
    [
        "::::",
        "-",
        "--",
        '"',
        '""',
        "label:a:b:c",
        "  is:open   ",
        "\t\nlabel:x\r",
        "-:x",
        ":value",
        "😀 label:emoji",
        "a" * 400,
    ],
)
def test_garbage_either_parses_or_raises_query_error_but_never_explodes(garbage):
    """Fuzz floor: the parser's only failure mode is QueryError. Anything else
    reaching a transport would be a 500 on user input."""
    try:
        work_query.parse(garbage)
    except QueryError:
        pass


def test_a_colon_inside_a_value_is_kept_whole():
    # partition() on the FIRST colon, so a URL-ish value survives.
    assert work_query.parse("label:a:b:c").of("label") == ("a:b:c",)


def test_an_empty_field_name_is_an_unknown_field_not_a_crash():
    with pytest.raises(QueryError) as caught:
        work_query.parse(":value")
    assert caught.value.atom == ""


# --- the vocabulary is emitted, not restated -------------------------------


def test_describe_emits_the_real_vocabulary():
    described = work_query.describe()
    fields = described["fields"]
    assert fields["is"] == list(work_query.IS_VALUES)
    assert fields["has"] == list(work_query.HAS_VALUES)
    assert fields["sort"] == list(work_query.SORT_VALUES)
    # Every open field is documented too, so /query/help is complete rather than
    # partial — an agent reading it must not have to guess at the rest.
    for field in work_query.OPEN_FIELDS:
        assert field in fields
    assert described["default_sort"] == work_query.DEFAULT_SORT
