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

from collections.abc import Callable
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


@dataclass(frozen=True)
class Reversal:
    """How one verb may (or may not) be undone."""

    verb: str
    action_class: str
    compensator: Compensator | None
    #: One line the operator reads when undo is refused — why this verb is one-way
    #: or a trapdoor. Empty for two-way verbs, which need no excuse.
    reason: str = ""


_REGISTRY: dict[str, Reversal] = {}


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
) -> None:
    """Teach the engine how one verb is reversed.

    Idempotent so the composition root may wire the same layer into many app
    instances in one process (every test builds its own app). Re-registering the
    SAME entry is a no-op; registering a DIFFERENT one for a verb already claimed
    raises, because a verb with two inverses is a bug, not a configuration."""
    if (action_class == TWO_WAY) != (compensator is not None):
        raise ValueError("exactly the two-way verbs carry a compensator")
    entry = Reversal(
        verb=verb, action_class=action_class, compensator=compensator, reason=reason
    )
    existing = _REGISTRY.get(verb)
    if existing is not None and existing != entry:
        raise ValueError(
            f"verb '{verb}' is already registered with a different inverse"
        )
    _REGISTRY[verb] = entry


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
                "nothing to undo: this action's effect is no longer in force",
                code=NO_EFFECT_CODE,
                status_code=409,
            )
        return recorded
