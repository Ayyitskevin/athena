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
