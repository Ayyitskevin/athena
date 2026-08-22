"""Markdown rendering for issue/page bodies (web/render.render_body).

These encode the contract that matters for the body render path:

  * Markdown formatting (headings, lists, emphasis, inline + block code, links)
    becomes real HTML — Mentor pages and issue descriptions are no longer
    plain text;
  * the body is NEVER trusted raw: author HTML is inert (escaped or stripped),
    and dangerous link schemes don't become live links;
  * a single newline still renders as a line break (breaks=True), so bodies
    written before Markdown landed read the same;
  * the [[issue:N]]/[[page:N]]/[[KEY-N]] cross-links still resolve, even when a
    token sits inside Markdown markup.
"""

from athena.aegis import issues
from athena.core import db
from athena.mentor import spaces
from athena.web.render import render_body


def _conn(db_file):
    conn = db.connect(db_file)
    db.migrate(conn)
    conn.execute("INSERT INTO users (email, name) VALUES ('a@e.com', 'A')")
    conn.commit()
    return conn


# --- formatting -------------------------------------------------------------


def test_markdown_headings_lists_and_emphasis_render(tmp_path):
    conn = _conn(tmp_path / "fmt.db")
    html = str(render_body(conn, "## Title\n\n- one\n- two\n\nsome **bold** text"))
    assert "<h2>Title</h2>" in html
    assert "<ul>" in html and "<li>one</li>" in html
    assert "<strong>bold</strong>" in html


def test_markdown_inline_and_block_code_render(tmp_path):
    conn = _conn(tmp_path / "code.db")
    html = str(render_body(conn, "use `x = 1` here\n\n```\nblock\n```"))
    assert "<code>x = 1</code>" in html
    assert "<pre>" in html and "block" in html


def test_markdown_link_renders_as_anchor(tmp_path):
    conn = _conn(tmp_path / "link.db")
    html = str(render_body(conn, "see [docs](https://example.com)"))
    assert 'href="https://example.com"' in html
    assert ">docs</a>" in html


def test_single_newline_is_a_line_break(tmp_path):
    # WHY: breaks=True keeps authored line breaks, so a plain-text body written
    # before Markdown landed still reads with its lines intact.
    conn = _conn(tmp_path / "br.db")
    html = str(render_body(conn, "line one\nline two"))
    assert "<br" in html


def test_blank_line_separates_paragraphs(tmp_path):
    conn = _conn(tmp_path / "para.db")
    html = str(render_body(conn, "para one\n\npara two"))
    assert html.count("<p>") == 2


def test_empty_body_renders_empty(tmp_path):
    conn = _conn(tmp_path / "empty.db")
    assert str(render_body(conn, "")) == ""
    assert str(render_body(conn, None)) == ""


# --- safety (the body is untrusted, possibly agent-authored) ----------------


def test_raw_script_is_inert(tmp_path):
    conn = _conn(tmp_path / "xss1.db")
    html = str(render_body(conn, "<script>alert(1)</script>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_event_handler_html_is_neutralized(tmp_path):
    # WHY: author HTML never becomes a live element — the <img> is escaped to
    # inert text, so there is no real tag for an onerror handler to ride on.
    conn = _conn(tmp_path / "xss2.db")
    html = str(render_body(conn, "<img src=x onerror=alert(1)>"))
    assert "<img" not in html  # no live tag
    assert "&lt;img" in html  # shown as escaped text instead


def test_javascript_link_scheme_is_not_live(tmp_path):
    # WHY: the literal text "javascript:..." may appear inertly, but it must never
    # become a live <a href="javascript:..."> link.
    conn = _conn(tmp_path / "xss3.db")
    html = str(render_body(conn, "[click](javascript:alert(1))"))
    assert 'href="javascript' not in html
    assert "<a " not in html  # the dangerous link was not rendered as an anchor


# --- cross-links survive Markdown rendering ---------------------------------


def test_crosslink_inside_markdown_emphasis_resolves(tmp_path):
    conn = _conn(tmp_path / "x1.db")
    target = issues.create_issue(conn, title="Target", body="", created_by=1)
    # The token sits inside bold markup — it must still become a live xref.
    html = str(render_body(conn, f"**see [[issue:{target['id']}]]**"))
    assert f'href="/aegis/issues/{target["id"]}"' in html
    assert 'class="xref"' in html
    assert "Target" in html
    assert "<strong>" in html


def test_broken_crosslink_in_list_item_renders_broken(tmp_path):
    conn = _conn(tmp_path / "x2.db")
    html = str(render_body(conn, "- dangling [[page:404]]"))
    assert "xref broken" in html
    assert "[[page:404]]" in html
    assert "<li>" in html


def test_page_body_renders_markdown(tmp_path):
    # WHY: render_body is the shared path for issue AND page bodies — exercise it
    # against a page body too so Mentor is covered, not just Aegis.
    conn = _conn(tmp_path / "pg.db")
    sp = spaces.create_space(conn, key="ENG", name="Eng", created_by=1)
    from athena.mentor import pages

    pg = pages.create_page(
        conn, space_id=sp["id"], title="Doc", body="# Heading\n\ntext", created_by=1
    )
    html = str(render_body(conn, pg["body"]))
    assert "<h1>Heading</h1>" in html


# --- buzz:// permalinks ------------------------------------------------------


def test_bare_buzz_permalink_renders_as_link(tmp_path):
    # A pasted relay permalink becomes a clickable chip labelled by entity kind,
    # href intact (the & in the query survives the escape/unescape round trip),
    # full URI in the title so hover shows where it goes.
    conn = _conn(tmp_path / "b.db")
    uri = "buzz://message?channel=e29bb951-d272-4822-a8e5-ffac2f9462f2&id=2ccaf8cb"
    html = str(render_body(conn, f"review evidence: {uri} (thread)"))
    assert f'href="{uri.replace("&", "&amp;")}"' in html
    assert 'class="xref buzz-link"' in html
    assert ">buzz:message</a>" in html
    assert "(thread)" in html


def test_buzz_permalink_entities_and_non_entities(tmp_path):
    conn = _conn(tmp_path / "b2.db")
    for entity in ("issue", "pr", "repo", "project"):
        html = str(render_body(conn, f"buzz://{entity}?owner=ab&d=x"))
        assert f">buzz:{entity}</a>" in html
    # An unknown entity stays literal text — this pass renders the relay's
    # entity-link grammar, it does not bless arbitrary buzz:// strings.
    html = str(render_body(conn, "buzz://huddle?x=1"))
    assert "<a" not in html and "buzz://huddle?x=1" in html


def test_buzz_permalink_never_lands_in_the_links_index(tmp_path):
    # Mention-style by design: rendered clickable, never indexed — there is no
    # local table a Buzz entity resolves against, so backlinks stay local. The
    # body carries a REAL cross-ref alongside the permalink so the assertion can
    # fail: the indexer demonstrably runs and records the local ref, and still
    # records nothing for the buzz URI.
    conn = _conn(tmp_path / "b3.db")
    target = issues.create_issue(conn, title="target", body="", created_by=1)
    issue = issues.create_issue(
        conn,
        title="radio receipt",
        body=f"see buzz://message?channel=ab&id=cd and [[issue:{target['id']}]]",
        created_by=1,
    )
    rows = conn.execute(
        "SELECT target_kind, target_id FROM links WHERE source_kind='issue' "
        "AND source_id=?",
        (issue["id"],),
    ).fetchall()
    assert [(r["target_kind"], r["target_id"]) for r in rows] == [
        ("issue", target["id"])
    ]
    assert not any(r["target_kind"].startswith("buzz") for r in rows)


def test_buzz_pass_cannot_inject_markup_into_an_attribute(tmp_path):
    # The pass emits markup AFTER nh3 sanitized, so it must only ever touch text
    # nodes. Markdown puts author text into alt= and title= attributes, which is
    # where a character-class guard failed: substituting there injected a raw `"`
    # and a `>` that closed the tag early.
    conn = _conn(tmp_path / "attr.db")
    html = str(
        render_body(conn, "![see buzz://message?channel=ab&id=cd](https://x/p.png)")
    )
    assert 'alt="see buzz://message?channel=ab&amp;id=cd"' in html
    assert "<a" not in html  # no anchor smuggled inside the img tag
    assert html.count("<img") == 1 and html.count(">") == html.count("<")

    # The link-title variant: nh3's own hardening must stay INSIDE the anchor.
    html = str(
        render_body(conn, '[click](https://good.example "go buzz://message?id=1")')
    )
    assert 'rel="noopener noreferrer"' in html.split(">")[0] + ">" or "rel=" in html
    assert "buzz-link" not in html  # nothing substituted inside the title attribute
    assert ">click</a>" in html


def test_buzz_permalink_inside_a_link_stays_literal(tmp_path):
    # A permalink already inside an anchor must not nest a second one — browsers
    # resolve nested anchors by closing the outer link, truncating it.
    conn = _conn(tmp_path / "nested.db")
    html = str(
        render_body(conn, "[buzz://message?channel=ab&id=cd](https://example.com/x)")
    )
    assert html.count("<a ") == 1
    assert "buzz-link" not in html
    assert 'href="https://example.com/x"' in html


def test_buzz_pass_is_linear_on_adversarial_input(tmp_path):
    # This repo has a py/polynomial-redos history in the link grammar. The
    # pattern has one unambiguous repeated class, so a long non-matching run
    # cannot backtrack quadratically; assert it stays fast rather than trusting
    # the shape.
    import time

    conn = _conn(tmp_path / "redos.db")
    hostile = "buzz://message?" + ("a=" * 40000)
    start = time.monotonic()
    render_body(conn, hostile)
    assert time.monotonic() - start < 2.0
