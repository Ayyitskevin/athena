"""Reading what a forge says — the pure half of the integration.

Athena **integrates with a forge; it never becomes one.** This module is the
parser for inbound events: it turns one signed webhook payload into a small list
of normalized facts, and finds the issue keys those facts name. It touches no
database, makes no network call, and decides nothing about trust — the caller
authenticates the delivery before any of this runs.

Three properties are the design:

  * **A closed vocabulary.** Exactly three event kinds are understood: a push, a
    pull-request transition, and a branch creation. Anything else is ignored
    rather than stored generically. A tracker that quietly absorbs every event
    shape a forge invents becomes a second, worse copy of the forge.
  * **Every fact is a CLAIM BY THE SOURCE.** Nothing here is verified against
    reality; Athena knows only what it was told, and the wording of every summary
    says so. That is why these land as imported history rather than as native
    events.
  * **Key extraction proposes; the database disposes.** A bare token like
    ``ATH-12`` is *shaped* like an issue key, and so are ``UTF-8``, ``SHA-256``,
    and ``ISO-8601``. This module returns candidates; only the caller, resolving
    against real projects, can turn one into an issue — which is what keeps a
    commit message mentioning a character encoding from landing on somebody's
    work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The event kinds Athena understands, normalized away from any one forge's naming.
KIND_PUSH = "push"
KIND_PULL_REQUEST = "pull_request"
KIND_BRANCH = "branch"
KINDS = (KIND_PUSH, KIND_PULL_REQUEST, KIND_BRANCH)

# The source dialects that can be registered. Closed, and checked at registration,
# so no request can arrive for a parser that does not exist.
SOURCE_GITHUB = "github"
SOURCE_KINDS = (SOURCE_GITHUB,)

# The signature header each dialect signs with, and the header naming the event.
GITHUB_SIGNATURE_HEADER = "x-hub-signature-256"
GITHUB_EVENT_HEADER = "x-github-event"

# A delivery larger than this is refused unread. A forge push event carries a
# commit list, which is unbounded in principle; the cap keeps an inbound endpoint
# from being a memory amplifier for anyone holding the secret.
MAX_BODY_BYTES = 512 * 1024

# How many commits from a single push are examined. A force-push or a merge of a
# long branch can carry hundreds; the first few are where humans put issue keys,
# and the cap bounds the work one delivery can cause.
MAX_COMMITS = 20

# Bounds on what is written into a trail row, so a forge can never author an
# unbounded detail string.
MAX_SUMMARY_CHARS = 200
MAX_REF_CHARS = 300

# How many distinct issues one delivery may land on. A commit message listing
# thirty keys is either a mass rename or an attempt to spray the trail.
MAX_ISSUES_PER_DELIVERY = 10

# A token SHAPED like an issue key: a letter-led alphanumeric prefix, a dash, and
# digits. Deliberately permissive — the prefix is validated against real project
# keys by the caller, which is the only check that can tell ATH-12 from UTF-8.
_KEY_RE = re.compile(r"(?<![0-9A-Za-z])([A-Za-z][A-Za-z0-9]*)-(\d+)(?![0-9A-Za-z])")


@dataclass(frozen=True)
class ForgeFact:
    """One thing a source claims happened, and the issue keys it names."""

    kind: str
    summary: str
    ref: str | None = None
    keys: tuple[tuple[str, int], ...] = field(default_factory=tuple)


class ForgeEventError(ValueError):
    """An inbound payload that cannot be read as the dialect it claims to be."""


def extract_keys(*texts: str | None) -> list[tuple[str, int]]:
    """Every ``(PREFIX, number)`` candidate in the given texts, first-seen order.

    Case is preserved for the caller to match case-insensitively (project keys are
    ``COLLATE NOCASE``). Deduped, because a branch name and its PR title usually
    repeat the same key and one delivery should land once.
    """
    found: list[tuple[str, int]] = []
    for text in texts:
        if not text:
            continue
        for prefix, number in _KEY_RE.findall(text):
            candidate = (prefix.upper(), int(number))
            if candidate not in found:
                found.append(candidate)
    return found


def _clip(text: str | None, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def _branch_of(ref: str | None) -> str | None:
    """The branch name in a git ref, or None if the ref names something else.

    Tags are deliberately not branches: a tag is a release marker, not the place
    someone writes an issue key while working.
    """
    if ref and ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    return None


def parse_github(event: str, payload: dict) -> list[ForgeFact]:
    """Normalize one GitHub webhook delivery into the facts Athena keeps.

    Returns an empty list for an event outside the closed vocabulary — a delivery
    Athena has no opinion about is authentic but uninteresting, which is not an
    error and must not look like one.

    Raises ``ForgeEventError`` only when the payload contradicts its own header
    (a ``push`` with no ``commits`` array, say), because that is a malformed
    delivery rather than an uninteresting one.
    """
    if not isinstance(payload, dict):
        raise ForgeEventError("payload must be a JSON object")

    if event == "push":
        commits = payload.get("commits")
        if commits is None:
            raise ForgeEventError("push payload has no 'commits'")
        if not isinstance(commits, list):
            raise ForgeEventError("push 'commits' must be a list")
        branch = _branch_of(payload.get("ref"))
        facts: list[ForgeFact] = []
        for commit in commits[:MAX_COMMITS]:
            if not isinstance(commit, dict):
                continue
            message = commit.get("message")
            # The branch name is part of the key search: teams that name branches
            # ATH-12-thing routinely write commit messages that do not repeat it.
            keys = extract_keys(message, branch)
            if not keys:
                continue
            facts.append(
                ForgeFact(
                    kind=KIND_PUSH,
                    summary=_clip(
                        f"commit on {branch or 'a branch'}: {message}",
                        MAX_SUMMARY_CHARS,
                    ),
                    ref=_clip(commit.get("url"), MAX_REF_CHARS) or None,
                    keys=tuple(keys),
                )
            )
        return facts

    if event == "pull_request":
        pull = payload.get("pull_request")
        if not isinstance(pull, dict):
            raise ForgeEventError("pull_request payload has no 'pull_request'")
        action = payload.get("action")
        # Merged is not an action of its own in GitHub's shape: a close carries a
        # `merged` flag, and the distinction is the whole point of the event.
        if action == "closed":
            verb = "merged" if pull.get("merged") else "closed"
        elif action == "opened":
            verb = "opened"
        elif action == "reopened":
            verb = "reopened"
        else:
            return []  # edited, labeled, assigned, review_requested… not our business
        title = pull.get("title")
        branch = (
            (pull.get("head") or {}).get("ref")
            if (isinstance(pull.get("head"), dict))
            else None
        )
        keys = extract_keys(title, branch)
        if not keys:
            return []
        number = pull.get("number")
        label = f"pull request #{number}" if number is not None else "a pull request"
        return [
            ForgeFact(
                kind=KIND_PULL_REQUEST,
                summary=_clip(f"{label} {verb}: {title}", MAX_SUMMARY_CHARS),
                ref=_clip(pull.get("html_url"), MAX_REF_CHARS) or None,
                keys=tuple(keys),
            )
        ]

    if event == "create":
        if payload.get("ref_type") != "branch":
            return []  # a tag is not a branch
        branch = payload.get("ref")
        keys = extract_keys(branch)
        if not keys:
            return []
        return [
            ForgeFact(
                kind=KIND_BRANCH,
                summary=_clip(f"branch created: {branch}", MAX_SUMMARY_CHARS),
                ref=None,
                keys=tuple(keys),
            )
        ]

    return []


#: Parser per registered source kind. Adding a dialect means adding a parser here
#: and a value to SOURCE_KINDS — the two are edited together on purpose.
PARSERS = {SOURCE_GITHUB: parse_github}


def describe() -> dict:
    """The inbound vocabulary as data — emitted by the parser itself, so docs and
    help output cannot drift from what is actually accepted."""
    return {
        "source_kinds": list(SOURCE_KINDS),
        "event_kinds": list(KINDS),
        "github_events": ["push", "pull_request", "create"],
        "limits": {
            "max_body_bytes": MAX_BODY_BYTES,
            "max_commits": MAX_COMMITS,
            "max_issues_per_delivery": MAX_ISSUES_PER_DELIVERY,
            "max_summary_chars": MAX_SUMMARY_CHARS,
        },
        "never": [
            "Athena does not call the forge — inbound only, no stored forge token.",
            "A forge event is a claim by the source, recorded as imported history.",
        ],
    }
