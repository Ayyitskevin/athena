"""Live embeds — pages that show real work.

Three layers, tested separately because they fail differently:

* the **grammar** (`core/embeds.py`), pure and fixture-free;
* the **resolver** (`aegis/embed_data.py`), where the viewer's visibility decides
  what a directive returns;
* the **renderer** (`web/render.py`), where an embed must compose with the
  Markdown sanitizer without either weakening it or being eaten by it.

The rule threaded through all three: an embed that cannot render says so, in
place, with a reason. A silent hole where an embed was is indistinguishable from
an embed that matched nothing, which is the invented answer the whole design
refuses to give.
"""

import pytest
from fastapi.testclient import TestClient

from athena.core import embeds
from athena.core.embeds import DirectiveError
from athena.main import create_app

H1 = {"X-Athena-Actor": "1"}


def _app(tmp_path, name="embeds.db"):
    return create_app(tmp_path / name), tmp_path / name


def _bootstrap(client):
    client.post("/users", json={"email": "a@e.com", "name": "Ann", "password": "pw"})


def _user(client, email):
    return client.post(
        "/users",
        json={"email": email, "name": email.split("@")[0], "password": "pw"},
        headers=H1,
    ).json()


def _login(client, email="a@e.com"):
    client.post("/login", data={"email": email, "password": "pw"})
    client.headers["X-CSRF-Token"] = client.cookies.get("athena_csrf", "")


def _space_and_page(client, body, *, title="Runbook"):
    space = client.post("/spaces", json={"key": "S", "name": "S"}, headers=H1).json()
    page = client.post(
        f"/spaces/{space['id']}/pages",
        json={"title": title, "body": body},
        headers=H1,
    ).json()
    return space, page


def _resolve(client, text, headers=H1):
    return client.post("/embeds/resolve", json={"text": text}, headers=headers)


def _fence(body):
    return f"```athena\n{body}\n```"


# --- the grammar (no database) ----------------------------------------------


def test_only_athena_fences_are_directives():
    assert embeds.find_directives("```python\nkind: issues\n```") == []
    assert embeds.find_directives("```\nplain\n```") == []
    found = embeds.find_directives(_fence("kind: count\nq: is:open"))
    assert len(found) == 1
    assert "kind: count" in found[0][1]


def test_the_fence_is_case_insensitive_and_tolerates_spacing():
    assert len(embeds.find_directives("``` ATHENA \nkind: count\n```")) == 1


def test_a_directive_parses_its_keys():
    directive = embeds.parse_directive("kind: issues\nq: is:open\nlimit: 5\ntitle: Now")
    assert directive.kind == "issues"
    assert directive.query == "is:open"
    assert directive.limit == 5
    assert directive.title == "Now"


def test_defaults_and_clamping():
    assert embeds.parse_directive("kind: issues").limit == embeds.DEFAULT_LIMIT
    # Clamped, not refused: the author's intent is clear and the surface says
    # what it actually showed.
    huge = embeds.parse_directive(f"kind: issues\nlimit: {embeds.MAX_LIMIT + 500}")
    assert huge.limit == embeds.MAX_LIMIT


@pytest.mark.parametrize(
    "body,fragment",
    [
        ("q: is:open", "needs a 'kind'"),
        ("kind: charts", "unknown embed kind"),
        ("kind: issues\nlimt: 5", "unknown embed key"),
        ("kind: issues\nlimit: soon", "must be a number"),
        ("kind: issues\nlimit: 0", "at least 1"),
        ("kind: issues\nkind: count", "more than once"),
        ("kind: issue", "needs an 'issue:'"),
        ("kind: issue\nissue: 1\nq: is:open", "not a 'q:'"),
        ("kind: issues\nissue: 1", "only applies to"),
        ("kind: issues\nnonsense line", "not 'key: value'"),
    ],
)
def test_every_malformed_directive_says_what_is_wrong(body, fragment):
    with pytest.raises(DirectiveError) as caught:
        embeds.parse_directive(body)
    assert fragment in str(caught.value)


def test_describe_emits_the_real_vocabulary():
    described = embeds.describe()
    assert set(described["kinds"]) == set(embeds.KINDS)
    assert described["limits"]["max_limit"] == embeds.MAX_LIMIT


# --- resolution, per viewer -------------------------------------------------


def test_an_issues_embed_resolves_to_real_rows(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = c.post(
            "/projects", json={"name": "Athena", "key": "ATH"}, headers=H1
        ).json()
        for n in range(3):
            c.post(
                "/issues",
                json={"title": f"work {n}", "project_id": project["id"]},
                headers=H1,
            )

        resolved = _resolve(c, _fence("kind: issues\nq: is:open project:ATH")).json()
        assert len(resolved) == 1
        result = resolved[0]
        assert result["error"] is None
        assert result["kind"] == "issues"
        assert {item["title"] for item in result["items"]} == {
            "work 0",
            "work 1",
            "work 2",
        }
        assert result["matched"] == 3
        assert result["truncated"] is False


def test_a_bounded_embed_reports_what_it_left_out(tmp_path):
    """A window presented as the whole answer is how an operator concludes there
    are two open issues when there are seven."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        for n in range(7):
            c.post("/issues", json={"title": f"work {n}"}, headers=H1)

        result = _resolve(c, _fence("kind: issues\nq: is:open\nlimit: 2")).json()[0]
        assert result["shown"] == 2
        assert result["matched"] == 7
        assert result["truncated"] is True


def test_count_and_single_issue_kinds(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        project = c.post(
            "/projects", json={"name": "Athena", "key": "ATH"}, headers=H1
        ).json()
        issue = c.post(
            "/issues",
            json={"title": "the one", "project_id": project["id"]},
            headers=H1,
        ).json()

        counted = _resolve(c, _fence("kind: count\nq: is:open")).json()[0]
        assert counted["kind"] == "count" and counted["matched"] == 1

        by_id = _resolve(c, _fence(f"kind: issue\nissue: {issue['id']}")).json()[0]
        assert by_id["item"]["title"] == "the one"
        # And by project key, through the SAME resolver [[ATH-1]] uses.
        by_key = _resolve(c, _fence("kind: issue\nissue: ATH-1")).json()[0]
        assert by_key["item"]["id"] == issue["id"]


def test_an_embed_renders_the_viewers_visibility_not_the_authors(tmp_path):
    """The property most likely to be broken by a future cache. An admin's embed
    shows a member only what that member could already see."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        outsider = _user(c, "out@e.com")
        h_out = {"X-Athena-Actor": str(outsider["id"])}
        private = c.post(
            "/projects", json={"name": "Private", "key": "PRV"}, headers=H1
        ).json()
        c.put(
            f"/projects/{private['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        c.post(
            "/issues",
            json={"title": "secret work", "project_id": private["id"]},
            headers=H1,
        )
        c.post("/issues", json={"title": "public work"}, headers=H1)

        text = _fence("kind: issues\nq: is:open")
        admin_titles = {i["title"] for i in _resolve(c, text).json()[0]["items"]}
        member_titles = {
            i["title"] for i in _resolve(c, text, headers=h_out).json()[0]["items"]
        }
        assert admin_titles == {"secret work", "public work"}
        assert member_titles == {"public work"}
        # The count follows visibility too — otherwise the number leaks what the
        # rows withheld.
        assert (
            _resolve(c, _fence("kind: count\nq: is:open"), headers=h_out).json()[0][
                "matched"
            ]
            == 1
        )


def test_a_hidden_single_issue_is_reported_exactly_like_a_missing_one(tmp_path):
    """Distinguishing them would make an embed an existence oracle for private
    work — the same rule the [[ATH-12]] renderer already applies."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        outsider = _user(c, "out@e.com")
        h_out = {"X-Athena-Actor": str(outsider["id"])}
        private = c.post(
            "/projects", json={"name": "Private", "key": "PRV"}, headers=H1
        ).json()
        c.put(
            f"/projects/{private['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        hidden = c.post(
            "/issues",
            json={"title": "secret", "project_id": private["id"]},
            headers=H1,
        ).json()

        hidden_result = _resolve(
            c, _fence(f"kind: issue\nissue: {hidden['id']}"), headers=h_out
        ).json()[0]
        missing_result = _resolve(
            c, _fence("kind: issue\nissue: 999999"), headers=h_out
        ).json()[0]
        assert hidden_result["error"] is not None
        # Same shape, and neither mentions the title.
        assert "secret" not in str(hidden_result)
        assert hidden_result["error"].split()[0] == missing_result["error"].split()[0]


def test_a_bad_query_inside_an_embed_is_an_error_not_an_empty_list(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        result = _resolve(c, _fence("kind: issues\nq: asignee:@me")).json()[0]
        assert result["error"] is not None
        assert "asignee" in result["error"]
        assert "items" not in result


def test_one_broken_directive_does_not_break_the_others(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        c.post("/issues", json={"title": "fine"}, headers=H1)
        text = f"{_fence('kind: nonsense')}\n\n{_fence('kind: count\nq: is:open')}"
        results = _resolve(c, text).json()
        assert len(results) == 2
        assert results[0]["error"] is not None
        assert results[1]["matched"] == 1


def test_too_many_directives_refuse_visibly_rather_than_vanishing(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        text = "\n\n".join(
            _fence("kind: count\nq: is:open")
            for _ in range(embeds.MAX_DIRECTIVES_PER_PAGE + 3)
        )
        results = _resolve(c, text).json()
        assert len(results) == embeds.MAX_DIRECTIVES_PER_PAGE + 3
        assert all(
            r["error"] is None for r in results[: embeds.MAX_DIRECTIVES_PER_PAGE]
        )
        overflow = results[embeds.MAX_DIRECTIVES_PER_PAGE :]
        assert all("not rendered" in r["error"] for r in overflow)


# --- rendering, and composing with the sanitizer ----------------------------


def test_a_page_renders_its_embed_with_live_data(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _login(c)
        c.post("/issues", json={"title": "live issue title"}, headers=H1)
        _, page = _space_and_page(
            c, f"Some prose.\n\n{_fence('kind: issues\nq: is:open')}\n\nMore prose."
        )
        rendered = c.get(f"/mentor/pages/{page['id']}")
        assert rendered.status_code == 200
        assert "live issue title" in rendered.text
        assert 'class="embed embed-issues"' in rendered.text
        # The prose around it still renders as prose.
        assert "Some prose." in rendered.text
        # And the directive source is gone — replaced, not left beside the result.
        assert "kind: issues" not in rendered.text


def test_the_rendered_page_shows_the_viewers_rows_not_the_authors(tmp_path):
    """The resolver-level visibility test above passes even if the PAGE renders
    with the wrong actor — the two paths fail independently. This one renders the
    real HTML as a non-member and asserts the private title never reaches it,
    which is the assertion a future per-page embed cache would break.
    """
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        member = _user(c, "member@e.com")
        private = c.post(
            "/projects", json={"name": "Private", "key": "PRV"}, headers=H1
        ).json()
        c.put(
            f"/projects/{private['id']}/visibility",
            json={"visibility": "private"},
            headers=H1,
        )
        c.post(
            "/issues",
            json={"title": "classified rocketry", "project_id": private["id"]},
            headers=H1,
        )
        c.post("/issues", json={"title": "ordinary chores"}, headers=H1)
        # Authored by the admin, who can see both.
        _login(c)
        _, page = _space_and_page(c, _fence("kind: issues\nq: is:open"))
        authored = c.get(f"/mentor/pages/{page['id']}")
        assert "classified rocketry" in authored.text

        # Read by a member who cannot see the private project.
        with TestClient(app) as other:
            other.post("/login", data={"email": "member@e.com", "password": "pw"})
            other.headers["X-CSRF-Token"] = other.cookies.get("athena_csrf", "")
            seen = other.get(f"/mentor/pages/{page['id']}")
            assert seen.status_code == 200
            assert "ordinary chores" in seen.text
            assert "classified rocketry" not in seen.text
        assert member["id"] != 1


def test_a_broken_embed_renders_a_visible_box_not_a_hole(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _login(c)
        _, page = _space_and_page(c, _fence("kind: charts"))
        rendered = c.get(f"/mentor/pages/{page['id']}")
        assert 'class="embed embed-error"' in rendered.text
        assert "unknown embed kind" in rendered.text


def test_a_non_athena_code_fence_is_left_completely_alone(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _login(c)
        _, page = _space_and_page(c, "```python\nkind: issues\nq: is:open\n```")
        rendered = c.get(f"/mentor/pages/{page['id']}")
        assert "<code>" in rendered.text
        assert "kind: issues" in rendered.text  # still literal code
        assert "embed-issues" not in rendered.text


def test_an_embed_cannot_be_used_to_inject_markup(tmp_path):
    """The embed HTML is built by Athena from escaped values. An author-supplied
    title carrying markup must arrive as text, exactly as it would anywhere else
    in a page body."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _login(c)
        _, page = _space_and_page(
            c, _fence("kind: count\nq: is:open\ntitle: <script>alert(1)</script>")
        )
        rendered = c.get(f"/mentor/pages/{page['id']}")
        assert "<script>alert(1)</script>" not in rendered.text
        assert "&lt;script&gt;" in rendered.text


def test_an_issue_title_containing_markup_is_escaped_in_an_embed(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _login(c)
        c.post("/issues", json={"title": "<img src=x onerror=alert(1)>"}, headers=H1)
        _, page = _space_and_page(c, _fence("kind: issues\nq: is:open"))
        rendered = c.get(f"/mentor/pages/{page['id']}")
        assert "onerror=alert(1)>" not in rendered.text
        assert "&lt;img" in rendered.text


def test_an_author_cannot_forge_the_placeholder_token(tmp_path):
    """The token carries a per-render nonce. An author writing a literal
    'athenaembed...' string gets their text, not somebody's embed."""
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _login(c)
        c.post("/issues", json={"title": "real work"}, headers=H1)
        _, page = _space_and_page(
            c, f"athenaembed0000000000000000x0\n\n{_fence('kind: count\nq: is:open')}"
        )
        rendered = c.get(f"/mentor/pages/{page['id']}")
        # The forged token survives as literal text...
        assert "athenaembed0000000000000000x0" in rendered.text
        # ...and the real embed still rendered exactly once.
        assert rendered.text.count('class="embed embed-count"') == 1


def test_the_page_still_renders_when_every_embed_fails(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        _login(c)
        _, page = _space_and_page(
            c, f"# Heading\n\n{_fence('kind: issues\nq: bogus:atom')}"
        )
        rendered = c.get(f"/mentor/pages/{page['id']}")
        assert rendered.status_code == 200
        assert "Heading" in rendered.text
        assert "embed-error" in rendered.text


# --- MCP parity -------------------------------------------------------------


def test_mcp_reads_a_pages_embeds_as_data_not_markup(tmp_path):
    from athena.mcp.client import AthenaClient

    app, _ = _app(tmp_path)
    with TestClient(app) as c:
        _bootstrap(c)
        agent = c.post(
            "/users/onboard_agent",
            json={"email": "s@e.com", "name": "Sol", "scopes": ["read", "docs:write"]},
            headers=H1,
        ).json()
        c.post("/issues", json={"title": "agent-visible work"}, headers=H1)
        _, page = _space_and_page(c, _fence("kind: issues\nq: is:open"))

        c.headers.update({"Authorization": f"Bearer {agent['token']['token']}"})
        client = AthenaClient(client=c)
        results = client.page_embeds(page["id"])
        assert results[0]["items"][0]["title"] == "agent-visible work"
        # Data, not markup — an agent reads rows.
        assert "<div" not in str(results)
        assert client.embed_help()["limits"]["max_limit"] == embeds.MAX_LIMIT
