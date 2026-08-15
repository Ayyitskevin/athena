"""Generative tests over the hand-rolled parsers.

The rest of the suite is curated-case testing: it proves the parsers handle the
inputs someone thought of. These probe the ones nobody thought of, over the four
grammars that read untrusted text — the `[[ref]]` link grammar, the mention
grammar, the work-query grammar, and the forge key linkifier. All four run on
input an author types or a webhook delivers, and three of them feed a write path.

This file exists because that distinction is not academic. Writing it turned up a
crash reachable from an ordinary issue body: Python raises `ValueError` from
`int(str)` past 4300 digits, and every one of these grammars captured `\\d+`
unbounded, so `[[issue:<4301 digits>]]` took down `sync_links` — which runs on
every issue and page write — with a 500. Four grammars, one bug, none of it
visible from any hand-written example, because nobody writes that example. The
grammars are digit-bounded now (`links.ID_DIGITS`), and the properties below are
what keeps them that way.

The properties are deliberately about invariants rather than outputs: a parser
returns SOMETHING for all input, what it returns is well-formed, and re-parsing
its own output is stable. An assertion about a specific parse belongs in the
curated tests, where a reader can see the case.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from athena.core import forge_events, links, notifications, work_query

#: Arbitrary text, including the characters these grammars are built from. Plain
#: `st.text()` almost never produces a bracket, so it would exercise the "no match"
#: path and little else; this mixes structural characters into the alphabet so the
#: generator actually reaches the grammars.
BODY = st.lists(
    st.sampled_from(
        list("[]:-\"' \t\n0123456789abcXYZ") + ["issue", "page", "user", "ATH", "😀"]
    ),
    max_size=40,
).map("".join)

#: The digit run that used to be the bug: long enough to cross Python's int(str)
#: ceiling, generated rather than hardcoded so the boundary itself is probed.
DIGIT_RUN = st.integers(min_value=1, max_value=5000).map(lambda n: "9" * n)


# --- the link and mention grammars ------------------------------------------


@given(BODY)
def test_extract_refs_never_raises_and_returns_well_formed_refs(text):
    refs = links.extract_refs(text)
    assert len(refs) == len(set(refs)), "extract_refs promises deduplication"
    for kind, ident in refs:
        assert kind in ("issue", "page")
        assert isinstance(ident, int) and ident >= 0
        # Every ref it claims to have found is really in the text it was given.
        assert f"[[{kind}:{ident}]]" in text or f"[[{kind}:0{ident}]]" in text


@given(DIGIT_RUN)
def test_a_long_digit_run_is_text_not_a_crash(digits):
    """The bug this file was written to find. `int(str)` raises past 4300 digits, so
    an unbounded `\\d+` turned a body any author could type into a 500 on the write
    path. An over-long run must simply not be a reference."""
    for token in (
        f"[[issue:{digits}]]",
        f"[[page:{digits}]]",
        f"[[ATH-{digits}]]",
        f"[[user:{digits}]]",
    ):
        assert links.extract_refs(token) == [] or len(digits) <= 19
        assert notifications.parse_mentions(token) == [] or len(digits) <= 19
        assert links.KEY_REF_RE.findall(token) == [] or len(digits) <= 19
        assert forge_events.extract_keys(token) == [] or len(digits) <= 19


@given(st.lists(st.tuples(st.sampled_from(["issue", "page"]), st.integers(0, 10**18))))
def test_refs_round_trip_through_a_body(pairs):
    """Render references into text, read them back: same refs, first-seen order,
    deduplicated. The one true round-trip this grammar has."""
    body = " ".join(f"[[{kind}:{ident}]]" for kind, ident in pairs)
    expected: list[tuple[str, int]] = []
    for ref in pairs:
        if ref not in expected:
            expected.append(ref)
    assert links.extract_refs(body) == expected


@given(BODY)
def test_parse_mentions_never_raises_and_dedupes(text):
    ids = notifications.parse_mentions(text)
    assert ids == list(dict.fromkeys(ids))
    assert all(isinstance(i, int) and i >= 0 for i in ids)


@given(BODY)
def test_a_reserved_token_is_never_also_a_title(text):
    """The title grammar is deliberately broad and excludes the typed forms in code.
    Whatever `[[...]]` token appears, at most one resolver may claim it — otherwise a
    `[[issue:1]]` would also index as a page titled "issue:1"."""
    for inner in links.TITLE_REF_RE.findall(text):
        if links._is_reserved_ref(inner):
            assert (
                links.REF_RE.findall(f"[[{inner}]]")
                or links.KEY_REF_RE.findall(f"[[{inner}]]")
                or notifications.MENTION_RE.findall(f"[[{inner}]]")
            )


# --- the forge linkifier ----------------------------------------------------


@given(st.lists(BODY, max_size=4))
def test_extract_keys_never_raises_and_is_bounded(texts):
    keys = forge_events.extract_keys(*texts)
    assert keys == list(dict.fromkeys(keys))
    for prefix, number in keys:
        assert prefix and prefix[0].isalpha()
        assert isinstance(number, int) and number >= 0


# --- the work-query grammar -------------------------------------------------


@given(st.text(max_size=200))
def test_parse_raises_only_query_errors(raw):
    """A query box takes arbitrary text. Anything other than QueryError reaching a
    transport is a 500 where a 422 was promised."""
    try:
        query = work_query.parse(raw)
    except work_query.QueryError:
        return
    # A successful parse is well-formed: closed vocabularies stay closed.
    assert query.sort in work_query.SORT_VALUES
    for term in query.terms:
        assert term.field in work_query.FIELDS
        allowed = work_query._CLOSED.get(term.field)
        assert allowed is None or term.value in allowed


@given(st.text(max_size=200))
def test_reparsing_a_querys_own_raw_is_stable(raw):
    """`raw` is what a saved filter stores and what a chip UI renders back. If
    re-parsing it could produce a different query — or fail — a filter would not mean
    the same thing the second time it was opened."""
    try:
        first = work_query.parse(raw)
    except work_query.QueryError:
        return
    second = work_query.parse(first.raw)
    assert (second.terms, second.text, second.sort) == (
        first.terms,
        first.text,
        first.sort,
    )


@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12).filter(
        lambda w: w not in work_query.FIELDS
    ),
    st.text(alphabet="abcXYZ123", min_size=1, max_size=8),
)
def test_an_unknown_field_error_always_names_the_field(field, value):
    """QUERY.md's promise: "unknown search field 'asignee'" is actionable where
    "invalid query" is not, so the atom must survive to the transport."""
    try:
        work_query.parse(f"{field}:{value}")
    except work_query.QueryError as exc:
        assert exc.atom == field
        assert field in str(exc)
    else:
        raise AssertionError(f"{field!r} parsed as a known field")


@given(
    st.sampled_from(sorted(work_query._CLOSED)),
    st.text(alphabet="abcXYZ123", min_size=1, max_size=8),
)
def test_a_bad_closed_value_error_names_the_whole_atom(field, value):
    """For a field with a closed vocabulary the offending piece is `field:value`,
    not the field — the field was fine."""
    allowed = work_query._CLOSED[field]
    if value.lower() in allowed:
        return
    try:
        work_query.parse(f"{field}:{value}")
    except work_query.QueryError as exc:
        assert exc.atom == f"{field}:{value.lower()}"
        # ...and it lists what would have worked.
        assert all(option in str(exc) for option in allowed)
    else:
        raise AssertionError(f"{field}:{value} parsed as valid")


# --- the crash, at the boundary where it actually landed ---------------------


def test_an_over_long_reference_does_not_500_an_issue_write(tmp_path):
    """The property tests above pin the grammars; this pins the consequence. Before
    the bound, this exact request raised ValueError out of links.py and became a 500
    — an authenticated author could take down their own write by typing a long
    number between brackets."""
    from fastapi.testclient import TestClient

    from athena.main import create_app

    app = create_app(tmp_path / "refs.db")
    with TestClient(app) as client:
        client.post("/users", json={"email": "a@e.com", "name": "A", "password": "pw"})
        response = client.post(
            "/issues",
            json={"title": "long ref", "body": "[[issue:" + "9" * 5000 + "]]"},
            headers={"X-Athena-Actor": "1"},
        )
        assert response.status_code == 201, response.text

    # The body is stored verbatim; it simply projects no cross-link row.
    from athena.core import db

    conn = db.connect(tmp_path / "refs.db")
    try:
        issue_id = response.json()["id"]
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM links "
                "WHERE source_kind = 'issue' AND source_id = ?",
                (issue_id,),
            ).fetchone()["n"]
            == 0
        )
        body = conn.execute(
            "SELECT body FROM issues WHERE id = ?", (issue_id,)
        ).fetchone()["body"]
        assert body.count("9") == 5000, "the author's text was kept as written"
    finally:
        conn.close()
