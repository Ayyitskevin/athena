"""Lean answers, and the one glossary that explains what they leave out (AD2, D29).

A full desk carried the same explanations on every read: each lane's
``meaning``, the work protocol twice (under ``work`` and under ``office``), the
office's ``semantics``. A full work-context packet carried its ``semantics``
(with ``readiness``), ``protocol`` and the fixed half of ``how_to_claim``, and
a run of keys whose value was null. An agent paid for all of it on every call.

So each answer has a lean shape: the same answer without those texts and
without any null-valued key, under its own schema name. The texts move here,
listed by schema and path, and are served once: as the MCP resource
``athena://answers/glossary`` and at ``GET /agent/glossary``.

**One place strips.** :func:`lean` is a function over the built answer, never
a second builder: the lean shape is the full one minus what the glossary
lists, so the two cannot disagree on anything else. MCP serves it by default
(``explain=true`` restores the full answer); REST serves it on ``?lean=true``.

**The glossary cannot drift.** Each entry is the constant the builder puts in
the answer, imported here, not a copy of its wording.
``tests/test_answer_glossary.py`` collects every text a full answer carries at
a listed path, checks it is the glossary's, and checks a lean answer carries no
explanation key at all, so a new one fails until it is listed.
"""

from __future__ import annotations

import copy
import json
from typing import Any, NamedTuple

from athena.aegis import (
    desk,
    office,
    ready_queue,
    run_state,
    work_context,
    work_progress,
)
from athena.core import agent_notebook

SCHEMA = "athena.answer_glossary.v1"
URI = "athena://answers/glossary"
ROUTE = "/agent/glossary"
#: What ``?lean=true`` does, on ``GET /desk`` and the work-context route.
LEAN_QUERY = (
    f"true answers the lean shape: without the fixed explanations GET {ROUTE} "
    "lists and without null-valued keys, under the lean schema name. The "
    "default is the full answer."
)

#: The path step that stands for every item of a list.
EACH = "[]"

#: The keys that hold fixed explanation. A lean answer carries none of them,
#: anywhere (``tests/test_answer_glossary.py`` holds it).
EXPLANATION_KEYS = frozenset({"meaning", "semantics", "protocol", "readiness"})


class Explained(NamedTuple):
    """One fixed text: where a full answer carries it, and what it is."""

    path: tuple[str, ...]
    text: Any


class Shape(NamedTuple):
    """A full answer's schema, its lean schema, and the texts lean leaves out."""

    schema: str
    lean_schema: str
    served_by: str
    note: str
    explained: tuple[Explained, ...]


DESK = Shape(
    schema=desk.SCHEMA,
    lean_schema="athena.agent_desk.v3",
    served_by="`my_desk` (MCP, by default) and `GET /desk?lean=true`",
    note=(
        "Nested blocks keep their own schema names (`work.ready` is "
        "athena.ready_queue.v1, `office` is athena.office.v1); their texts are "
        "listed here by their path in the desk."
    ),
    explained=(
        Explained(("identity", "notebook", "meaning"), agent_notebook.NOTEBOOK_MEANING),
        Explained(("asks", "run_controls", "meaning"), desk.RUN_CONTROLS_MEANING),
        Explained(
            ("asks", "worker_kill_requests", "meaning"), desk.WORKER_KILL_MEANING
        ),
        Explained(("asks", "claim_handoffs", "meaning"), desk.CLAIM_HANDOFFS_MEANING),
        Explained(("work", "protocol"), desk.WORK_PROTOCOL),
        Explained(("work", "ready", "meaning"), ready_queue.READY_MEANING),
        Explained(
            ("work", "waiting_on_operator", "meaning"),
            run_state.WAITING_ON_OPERATOR_MEANING,
        ),
        Explained(
            ("work", "waiting_on_operator", "not_offered", "meaning"),
            ready_queue.NOT_OFFERED_MEANING,
        ),
        Explained(("work", "waiting_on", "meaning"), desk.WAITING_ON_MEANING),
        Explained(("work", "leases", "meaning"), desk.LEASES_MEANING),
        Explained(
            ("work", "leases", "items", EACH, "progress", "meaning"),
            work_progress.PROGRESS_MEANING,
        ),
        Explained(("office", "protocol"), office.PROTOCOL),
        Explained(("office", "semantics"), office.SEMANTICS),
    ),
)

PACKET = Shape(
    schema=work_context.SCHEMA,
    lean_schema="athena.issue_work_context.v2",
    served_by=(
        "`get_issue_work_context` (MCP, by default, full and brief) and "
        "`GET /issues/{ref}/work-context?lean=true`"
    ),
    note=(
        "`how_to_claim.claimant_prerequisite` keeps `satisfied_at_read` and "
        "`next_operation`: they are this reader's, not fixed. The brief packet "
        "carries none of these texts; lean, it only drops its null keys."
    ),
    explained=(
        Explained(("semantics",), work_context.SEMANTICS),
        Explained(("protocol",), work_context.PROTOCOL),
        Explained(
            ("how_to_claim", "claimant_prerequisite", "policy"),
            work_context.CLAIMANT_POLICY,
        ),
        Explained(
            ("how_to_claim", "claimant_prerequisite", "meaning"),
            work_context.CLAIMANT_MEANING,
        ),
        *(
            Explained(("how_to_claim", key), text)
            for key, text in work_context.HOW_TO_CLAIM.items()
        ),
    ),
)

SHAPES: dict[str, Shape] = {shape.schema: shape for shape in (DESK, PACKET)}


def dotted(path: tuple[str, ...]) -> str:
    """``("work", "leases", "items", "[]", "progress")`` as
    ``work.leases.items[].progress``."""
    return ".".join(path).replace(f".{EACH}", EACH)


def _found(value: Any, path: tuple[str, ...]):
    """Every (container, key) where ``path`` lands in ``value``."""
    head, *rest = path
    if head == EACH:
        if isinstance(value, list):
            for item in value:
                yield from _found(item, tuple(rest))
        return
    if not isinstance(value, dict) or head not in value:
        return
    if rest:
        yield from _found(value[head], tuple(rest))
    else:
        yield value, head


def explanations(answer: dict) -> list[tuple[Explained, Any]]:
    """Each glossary entry a full answer carries, with the value it carries
    there (one pair per place: a list path yields one per item)."""
    shape = SHAPES[answer["schema"]]
    return [
        (entry, container[key])
        for entry in shape.explained
        for container, key in _found(answer, entry.path)
    ]


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_nulls(item) for key, item in value.items() if item is not None
        }
    if isinstance(value, list):
        return [_without_nulls(item) for item in value]
    return value


def lean(answer: dict) -> dict:
    """The lean shape of a full desk or work-context answer: without the texts
    its schema's glossary entry lists, without null-valued keys, and under its
    lean schema name. ``answer`` is not changed."""
    shape = SHAPES[answer["schema"]]
    shaped = copy.deepcopy(answer)
    for entry in shape.explained:
        for container, key in list(_found(shaped, entry.path)):
            del container[key]
    shaped = _without_nulls(shaped)
    shaped["schema"] = shape.lean_schema
    return shaped


def _leaves(path: str, value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(f"{path}.{key}", item)
    else:
        yield path, value


def _shown(value: Any) -> str:
    return value if isinstance(value, str) else f"`{json.dumps(value)}`"


def render() -> str:
    """The glossary as markdown: docs/ANSWER_GLOSSARY.md and its packaged copy
    are this text, and ``GET /agent/glossary`` answers it."""
    lines = [
        "# Answer glossary",
        "",
        "<!-- Generated from src/athena/aegis/answer_glossary.py by "
        "scripts/generate_agent_api.py. Do not edit by hand. -->",
        "",
        "The desk and the work-context packet each have a lean shape: the full "
        "answer without the fixed explanations listed here and without any key "
        "whose value is null. This document holds those explanations once, by "
        "the lean schema that leaves them out and the path where the full answer "
        "carries them. It is served as the MCP resource "
        f"`{URI}` and at `GET {ROUTE}`.",
        "",
        "- On MCP, `my_desk` and `get_issue_work_context` answer lean by default; "
        "`explain=true` returns the full answer.",
        "- On REST, `GET /desk` and `GET /issues/{ref}/work-context` answer in "
        "full by default; `?lean=true` returns the lean shape.",
        "- A lean answer's `schema` names its shape. A key absent from a lean "
        "answer is null. Nothing else differs from the full answer.",
        f"- Paths are dotted from the answer's root; `{EACH}` stands for each "
        "item of a list.",
    ]
    for shape in SHAPES.values():
        lines += [
            "",
            f"## {shape.lean_schema}",
            "",
            f"The lean `{shape.schema}`: {shape.served_by}. {shape.note}",
            "",
        ]
        for entry in shape.explained:
            for path, value in _leaves(dotted(entry.path), entry.text):
                lines.append(f"- `{path}`: {_shown(value)}")
    return "\n".join(lines) + "\n"


def document() -> dict:
    """The glossary as ``GET /agent/glossary`` answers it: each lean schema's
    entries by path, and the same text as markdown."""
    return {
        "schema": SCHEMA,
        "uri": URI,
        "answers": [
            {
                "schema": shape.lean_schema,
                "lean_of": shape.schema,
                "explained": {
                    dotted(entry.path): entry.text for entry in shape.explained
                },
            }
            for shape in SHAPES.values()
        ],
        "markdown": render(),
    }
