"""Undo by compensation — reversing an action without rewriting history.

`VISION.md`'s Trust/Learn step promises the operator can *undo* what an agent did,
and `ARCHITECTURE.md` promises the activity trail is append-only. Those are only
compatible one way: **undo never deletes or edits a row.** Undoing event N runs
the inverse as a NEW, fully audited forward command, and the event that command
records carries ``reverses_event_id = N`` (migration 0064). History gains two rows
— the action and its reversal — which is the truthful record of what happened.

Three consequences follow, and they are the whole design:

* **Authorization is re-evaluated, never inherited.** The compensator runs as the
  undoing actor through the ordinary command owner, so the same role, scope, and
  visibility gates apply as if they had made the change themselves. Undo is not a
  back door into someone else's write.
* **Replay stays exact.** A reversal is an ordinary event of the run that
  performed it, so `run_replay` and lineage show the action *and* its later
  reversal rather than a rewritten past.
* **An undo that changes nothing is a refusal.** Every compensator is idempotent
  at the domain layer (re-archiving an archived issue records no event), so
  "nothing was recorded" is exactly the signal that the effect being undone is no
  longer in force — someone already reversed it by hand, or the world moved on.
  Reporting success there would be a lie, so :func:`undo` refuses.

**The registry is populated by the domain layers, not here.** ``core`` may not
import ``aegis``/``mentor`` (the import contract runs web → aegis|mentor → core),
and the inverse of a domain write is domain knowledge. So this module owns the
mechanism and the taxonomy, `aegis/issue_undo.py` and `mentor/page_undo.py` own
the inverses, and `main.py` — the composition root — wires them together.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import sqlite3

from athena.core import activity, db

# Reversibility classes, borrowed from the operator's own mental model. Only
# TWO_WAY carries a compensator; the other two exist so a surface can say WHY it
# will not offer undo instead of leaving the verb unexplained.
TWO_WAY = "two_way"
#: A real side effect that cannot be silently retracted (a comment people read, a
#: delivered webhook, a published blob). Reversing it is a new forward decision the
#: operator makes explicitly — deleting the comment — not something undo may infer.
ONE_WAY = "one_way"
#: No undo exists at any price: a revealed secret, a destroyed row, a completed
#: external effect. Surfaces must refuse to offer one.
TRAPDOOR = "trapdoor"
#: A verb nobody has classified yet. Distinct from TRAPDOOR: "we have not taught
#: undo about this" is an honest answer, and claiming it is irreversible would not be.
UNCLASSIFIED = "unclassified"

# Stable machine-readable refusal codes. Branch on these, not on prose.
NOT_FOUND_CODE = "undo_event_not_found"
NOT_REVERSIBLE_CODE = "undo_not_reversible"
ALREADY_REVERSED_CODE = "undo_already_reversed"
IMPORTED_CODE = "undo_imported_event"
NO_EFFECT_CODE = "undo_no_effect"
#: The inverse COMMAND refused — a role, scope, visibility, budget, or policy
#: answer, carrying that command's own status code and message. Distinct from the
#: codes above, which are the engine's own: this one means "undo is possible, but
#: not for you, or not right now."
COMMAND_REFUSED_CODE = "undo_refused_by_command"

#: A compensator applies the inverse of ``event`` as ``actor``, inside the caller's
#: transaction, using the ordinary command owner for that write. It raises whatever
#: that command raises when the actor may not perform it; it returns nothing,
#: because the *evidence* of success is the reversal event the command recorded.
Compensator = Callable[[sqlite3.Connection, dict | None, dict], None]
#: A precheck answers, WITHOUT writing, whether the compensator would accept
#: ``event`` from ``actor`` now: it raises :class:`UndoRefused` with the refusal
#: the undo would get (the inverse command's write gate, an effect no longer in
#: force) and returns nothing otherwise. Registered by the layer that owns the
#: compensator, which uses the same checks, so an offer and the act agree.
Precheck = Callable[[sqlite3.Connection, dict | None, dict], None]

#: What :func:`undo` says when the compensating command recorded nothing.
NO_EFFECT_DETAIL = "nothing to undo: this action's effect is no longer in force"


@dataclass(frozen=True)
class Reversal:
    """How one verb may (or may not) be undone."""

    verb: str
    action_class: str
    compensator: Compensator | None
    #: One line the operator reads when undo is refused — why this verb is one-way
    #: or a trapdoor. Empty for two-way verbs, which need no excuse.
    reason: str = ""
    #: How a surface asks, read-only, whether this undo would be accepted now.
    #: None: nobody taught the engine, so no surface may promise it (see offer).
    precheck: Precheck | None = None


_REGISTRY: dict[str, Reversal] = {}

#: A target kind's current revision tag (its ETag), or None when the target is
#: gone. Registered by the layer that owns the kind, so the engine can answer the
#: reversed target's new tag without importing that layer (C9: every write that
#: changes an issue's ETag returns the new one).
Validator = Callable[[sqlite3.Connection, int], str | None]
_VALIDATORS: dict[str, Validator] = {}


class UndoRefused(Exception):
    """Undo was refused. ``code`` is the stable contract; ``status_code`` lets each
    adapter answer in its own idiom without re-deriving the mapping."""

    def __init__(self, message: str, *, code: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def register(
    verb: str,
    *,
    action_class: str,
    compensator: Compensator | None = None,
    reason: str = "",
    precheck: Precheck | None = None,
) -> None:
    """Teach the engine how one verb is reversed.

    Idempotent so the composition root may wire the same layer into many app
    instances in one process (every test builds its own app). Re-registering the
    SAME entry is a no-op; registering a DIFFERENT one for a verb already claimed
    raises, because a verb with two inverses is a bug, not a configuration."""
    if (action_class == TWO_WAY) != (compensator is not None):
        raise ValueError("exactly the two-way verbs carry a compensator")
    if precheck is not None and compensator is None:
        raise ValueError("only a verb with a compensator carries a precheck")
    entry = Reversal(
        verb=verb,
        action_class=action_class,
        compensator=compensator,
        reason=reason,
        precheck=precheck,
    )
    existing = _REGISTRY.get(verb)
    if existing is not None and existing != entry:
        raise ValueError(
            f"verb '{verb}' is already registered with a different inverse"
        )
    _REGISTRY[verb] = entry


def register_validator(target_kind: str, validator: Validator) -> None:
    """Teach the engine how a target kind names its current revision. Idempotent
    for the same function; a second, different one for a kind raises, as a verb
    with two inverses does."""
    existing = _VALIDATORS.get(target_kind)
    if existing is not None and existing is not validator:
        raise ValueError(f"target kind '{target_kind}' already has a validator")
    _VALIDATORS[target_kind] = validator


def target_validator(conn: sqlite3.Connection, reversal: dict) -> str | None:
    """The reversed target's revision tag after the reversal, or None when its
    kind registered none (a page, today) or the target is gone. Read inside the
    request's connection, after the reversal committed."""
    validator = _VALIDATORS.get(reversal["target_kind"])
    return None if validator is None else validator(conn, reversal["target_id"])


def reversal_for(verb: str) -> Reversal:
    """How this verb is classified — UNCLASSIFIED when nobody has taught undo yet."""
    return _REGISTRY.get(verb) or Reversal(
        verb=verb,
        action_class=UNCLASSIFIED,
        compensator=None,
        reason="this action has no registered inverse",
    )


def is_undoable(event: dict) -> bool:
    """Whether a surface should offer undo for this event *shape*.

    Shape only: the verb is two-way and the row is natively recorded. Whether it
    has already been reversed is a database fact the caller supplies separately
    (`activity.reversed_event_ids`), and whether THIS actor may perform the inverse
    is a question only :func:`undo` can answer — a caller cannot know another
    actor's authorization. This exists so a surface does not render a button that
    is guaranteed to fail, not to pre-empt the authority.

    An event that is itself a reversal stays undoable: undoing an undo is a redo,
    an ordinary forward command like any other."""
    return (
        reversal_for(event["verb"]).action_class == TWO_WAY
        and event.get("imported_at") is None
    )


def offer(
    conn: sqlite3.Connection, actor: dict | None, events: Sequence[dict]
) -> dict | None:
    """The newest undoable event of ``events`` (newest first, as
    ``activity.list_activity`` returns them, already gated by the actor's
    visibility), when :func:`undo` would accept it from ``actor`` right now;
    otherwise None.

    Stronger than :func:`is_undoable`, for a surface that offers ONE undo where
    the operator looks (the issue page, U9). The candidate is the newest event
    whose shape is two-way and native and which is not already reversed. It is
    offered when its verb's registered precheck (the same write gate and
    still-in-force checks its compensator applies) passes. When that precheck
    refuses, or the verb has none, nothing is offered: the offer never moves
    down to an older event, so one Undo always means the last change that can
    be reversed. An older event is still undone from the activity page.

    One refusal is not about now: ``undo_not_reversible`` says the event never
    had an inverse (an edit recorded before prior texts were kept, F10). Such
    an event is no candidate, as a one-way verb is none, and the next one is
    asked; otherwise one old edit would end the offer on its issue for good.
    Read-only; :func:`undo` still decides under its own transaction, so a race
    between render and click is refused there, not here."""
    if actor is None:
        return None
    shaped = [event for event in events if is_undoable(event)]
    if not shaped:
        return None
    already = activity.reversed_event_ids(conn, [event["id"] for event in shaped])
    for candidate in shaped:
        if candidate["id"] in already:
            continue
        precheck = reversal_for(candidate["verb"]).precheck
        if precheck is None:
            return None
        try:
            precheck(conn, actor, candidate)
        except UndoRefused as refused:
            if refused.code == NOT_REVERSIBLE_CODE:
                continue
            return None
        return candidate
    return None


def undo(conn: sqlite3.Connection, actor: dict | None, *, event_id: int) -> dict:
    """Reverse one event as ``actor``, returning the reversal event.

    Refuses with :class:`UndoRefused` when the event is invisible or missing, was
    imported, was already reversed, has no registered inverse, or turns out to have
    nothing left to reverse. Anything the compensating command itself refuses
    (role, scope, visibility, a budget, an approval gate) propagates unchanged —
    undo is an ordinary write and is bound by every rule ordinary writes obey."""
    event = activity.get_visible_activity(conn, event_id, actor)
    if event is None:
        # Missing and hidden collapse to one answer on purpose (see
        # get_visible_activity): undo must not become a probe for private work.
        raise UndoRefused(
            "no such activity event", code=NOT_FOUND_CODE, status_code=404
        )
    if event["imported_at"] is not None:
        # An imported row describes something that happened in ANOTHER system, and
        # 0041 exists precisely so foreign history is never mistaken for our own.
        # Compensating it here would invent a local write nobody made.
        raise UndoRefused(
            "an imported event describes history from another system and cannot be undone",
            code=IMPORTED_CODE,
            status_code=422,
        )
    reversal = reversal_for(event["verb"])
    if reversal.action_class != TWO_WAY or reversal.compensator is None:
        raise UndoRefused(
            f"'{event['verb']}' is {reversal.action_class}: {reversal.reason}",
            code=NOT_REVERSIBLE_CODE,
            status_code=422,
        )
    with db.transaction(conn, immediate=True):
        # Re-read under the writer reservation: the cheap pre-check above races, and
        # this one cannot. The unique index in 0064 is the final backstop.
        existing = activity.reversal_of(conn, event_id)
        if existing is not None:
            raise UndoRefused(
                f"already reversed by event #{existing['id']}",
                code=ALREADY_REVERSED_CODE,
                status_code=409,
            )
        with activity.pending_reversal(event_id):
            reversal.compensator(conn, actor, event)
        recorded = activity.reversal_of(conn, event_id)
        if recorded is None:
            # The compensating command recorded nothing, which its idempotency
            # rules mean exactly one thing: the effect is not in force any more.
            # Rolls back — a "successful" undo that changed nothing is a lie.
            raise UndoRefused(
                NO_EFFECT_DETAIL,
                code=NO_EFFECT_CODE,
                status_code=409,
            )
        return recorded
