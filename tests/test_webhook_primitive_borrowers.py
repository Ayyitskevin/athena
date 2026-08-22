"""Pin non-webhook subsystems that borrow webhook transport primitives."""

from __future__ import annotations

import ast
from pathlib import Path

from athena.core import webhooks


WEBHOOK_PRIMITIVE_BORROWERS = {
    "athena.aegis.icarus_commands": {
        "Poster",
        "is_safe_url",
        "sign",
        "urllib_poster",
    },
    "athena.aegis.dispatch_api": {"verify"},
    "athena.core.event_sources": {"sign"},
}

_WEBHOOK_FEATURE_MODULES = {
    "athena.core.webhooks",
    "athena.core.webhooks_api",
    "athena.web.admin",
}
_SHARED_PRIMITIVES = frozenset().union(*WEBHOOK_PRIMITIVE_BORROWERS.values())


def test_borrowed_webhook_primitives_keep_their_security_contract() -> None:
    for module, primitives in WEBHOOK_PRIMITIVE_BORROWERS.items():
        for name in primitives:
            assert callable(getattr(webhooks, name)), f"{module} borrows {name}"

    safe, _ = webhooks.is_safe_url("http://127.0.0.1/dispatch")
    assert safe is False

    signature = webhooks.sign("secret", b"body")
    assert signature
    assert webhooks.verify("secret", b"body", signature) is True


def test_every_webhook_primitive_borrower_is_recorded() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "athena"
    found: dict[str, set[str]] = {}

    for path in sorted(source_root.rglob("*.py")):
        dotted = "athena." + str(path.relative_to(source_root).with_suffix("")).replace(
            "/", "."
        ).removesuffix(".__init__")
        if dotted in _WEBHOOK_FEATURE_MODULES:
            continue

        tree = ast.parse(path.read_text())
        uses = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "webhooks"
            and node.attr in _SHARED_PRIMITIVES
        }
        if uses:
            found[dotted] = uses

    assert found == WEBHOOK_PRIMITIVE_BORROWERS
