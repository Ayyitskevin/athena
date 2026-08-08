"""The template contracts hold, and their parsers read markup correctly.

WHY these exist: the 2026-08-08 design migration shipped a stylesheet that
dropped rules for 116 classes templates still used, and CI stayed green —
tests assert markup, never "does this class render as anything". These tests
run the two contract scripts the way CI does, so a template pointing at a
dead endpoint or an unstyled class fails the suite, not the first user.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_styles = _load("check_template_styles")
check_routes = _load("check_template_routes")


def test_every_rendered_class_is_styled_or_allowlisted():
    assert check_styles.main() == 0


def test_every_template_endpoint_reaches_a_route():
    assert check_routes.main() == 0


# --- Parser behavior the contracts depend on --------------------------------


def test_class_tokenizer_drops_dynamic_fragments_not_literals():
    tokens = check_styles._class_tokens(
        '<span class="chip rollup-{{ segment.bucket }} label">'
        '<i class="{% if x %}on{% endif %} solo">'
    )
    # Dynamic fragments must vanish entirely; standalone literals survive.
    assert tokens == {"chip", "label", "solo"}


def test_class_tokenizer_ignores_jinja_comments():
    assert check_styles._class_tokens('{# class="ghost" #}<b class="real">') == {"real"}


def test_route_normalizer_expands_both_branch_arms():
    raw = (
        "{% if item.kind == 'issue' %}/aegis/issues/{{ item.id }}/link-mention"
        "{% else %}/mentor/pages/{{ item.id }}/link-mention{% endif %}"
    )
    variants = check_routes._normalize(raw)
    assert len(variants) == 2
    assert any(v.startswith("/aegis/issues/") for v in variants)
    assert any(v.startswith("/mentor/pages/") for v in variants)


def test_route_matcher_is_method_aware():
    table = [("/aegis/issues/{issue_id}/status", frozenset({"POST"}))]
    assert check_routes._matches("/aegis/issues/\0/status", "ANY", table)
    assert check_routes._matches("/aegis/issues/\0/status", "POST", table)
    assert not check_routes._matches("/aegis/issues/\0/status", "GET", table)
    assert not check_routes._matches("/aegis/issues/\0/nope", "POST", table)
