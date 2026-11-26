"""Read and search tools.

Registered by mcp.server.build_server."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from athena.aegis import fleet_attention, fleet_metrics, fleet_work, issue_narrative
from athena.core import tokens
from athena.core.notification_commands import MAX_ACKNOWLEDGE_IDS
from athena.mcp import results
from athena.mcp.results import ListResult, listing, paged, window
from athena.mcp.server import (
    ADDS,
    ANY_WRITE_SCOPE,
    CONDUCTOR,
    ERASES,
    FULL_ONLY,
    KNOWLEDGE,
    READS,
    READ_ONLY,
    SEAT,
    SETS,
    AttentionLimit,
    AttentionWindowHours,
    AutomationScheduleAt,
    AutomationScheduleInterval,
    AutomationTriggerType,
    BodyOffset,
    CommentChars,
    CommentListLimit,
    DelegationLimit,
    DelegationOffset,
    DigestWindowMinutes,
    EventLimit,
    FleetActorLimit,
    FleetAgentId,
    FleetMetricDate,
    FleetMetricId,
    FleetWorkLimit,
    IdempotencyKey,
    IssueFilterId,
    IssueHistoryLimit,
    IssueListLimit,
    ListCursor,
    ListOffset,
    MuteUntil,
    NotificationLimit,
    NotificationPriority,
    ProjectId,
    RunId,
    WaitSeconds,
    WatchKind,
    WatchPreferencePriority,
    WatchTargetId,
    WorkspaceSearchLimit,
    WriteHint,
)

# The page size the search tool asks /search for, and the inbox's REST cap.
SEARCH_LIMIT = 20
NOTIFICATION_LIST_CAP = 200
# /activity/runs reconstructs from at most this many recent events.
ACTIVITY_RUNS_EVENT_CAP = 500


def _issue_ref(ref: str | None, issue_id: int | None) -> str:
    """One issue, named either way: `issue_id` (the number every write tool
    takes) or `ref` ('12' or 'ATH-12'). Exactly one of the two, so a call that
    names two issues cannot silently read one of them."""
    if (ref is None) == (issue_id is None):
        raise ValueError("pass exactly one of ref or issue_id")
    return ref if ref is not None else str(issue_id)


def _comment_row(row: dict, include_body: bool, max_chars: int) -> dict:
    """A comment as a list row: an excerpt by default, or its body up to
    ``max_chars``, marked when cut and naming the call that reads the rest of
    it (get_issue_comment, from where the cut ends)."""
    body = row.get("body") or ""
    if not include_body:
        shaped = results.summary(row)
        shaped["excerpt"] = body[: results.EXCERPT_CHARS]
        shaped["body_chars"] = len(body)
        return shaped
    if len(body) <= max_chars:
        return row
    more = {
        "tool": "get_issue_comment",
        "arguments": {
            "issue_id": row["issue_id"],
            "comment_id": row["id"],
            "body_offset": max_chars,
        },
    }
    return {
        **row,
        "body": body[:max_chars],
        results.TRUNCATED_KEY: {"body": {"total_chars": len(body), "next": more}},
    }


def register_read_tools(tool, mutation_tool, client) -> None:
    # --- search & read ------------------------------------------------------

    @tool(scope=READ_ONLY, profiles=KNOWLEDGE)
    def search(
        query: str, kind: str | None = None, offset: ListOffset = 0
    ) -> ListResult:
        """Full-text search of issues and pages (kind='issue' or 'page' narrows):
        ranked hits with title and snippet."""
        hits = client.search(query, kind=kind, limit=SEARCH_LIMIT, offset=offset)
        return paged(hits, limit=SEARCH_LIMIT, offset=offset)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def search_workspace(
        query: str, limit_per_kind: WorkspaceSearchLimit | None = None
    ) -> dict:
        """One ask across issues, pages, AND comments — use this when you do not
        already know which module holds the answer.

        The work query grammar works here: `is:open label:infra project:ATH`
        filters issues structurally, and any bare words in the same query are
        full-text searched across pages and comments. Plain words alone
        full-text search all three. An unknown atom (`labl:infra`) is an ERROR
        naming the atom, never an empty result — so a typo cannot read as "no
        such work".

        Results are GROUPED BY KIND, not globally ranked: two engines with two
        orders cannot be interleaved into one honest relevance score, so compare
        within a group, not across them. Each group reports `clipped` when its
        bound cut the list. A pure-grammar query leaves the page and comment
        groups empty — `query.text` shows exactly what was text-searched."""
        return client.search_workspace(query, limit_per_kind=limit_per_kind)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def list_issues(
        status: str | None = None,
        project: str | None = None,
        sprint: IssueFilterId | None = None,
        label: str | None = None,
        search: str | None = None,
        include_archived: bool = False,
        limit: IssueListLimit = 50,
        cursor: ListCursor | None = None,
    ) -> ListResult:
        """List Aegis issues, optionally filtered by a project-configured status,
        project (id or 'none' for the backlog), sprint id, label name, or a text
        substring. Omit sprint to include every sprint (there is no unsprinted-only
        value). Each result's assignee_is_agent is true for an agent, false for a
        human, and null when unassigned. Archived issues are hidden by default; pass
        include_archived=true to see them. The backlog defaults to
        open/in_progress/done; other projects may define different status names.
        Rows are in id order. While truncated, keep the filters fixed and add the
        cursor in `next`; an issue created meanwhile never repeats a row."""
        page = client.list_issues(
            status=status,
            project=project,
            sprint=sprint,
            label=label,
            search=search,
            include_archived=include_archived,
            limit=limit,
            cursor=cursor,
        )
        return listing(
            [results.summary(row) for row in page["issues"]],
            truncated=page["has_more"],
            next={"cursor": page["next_cursor"]} if page["has_more"] else None,
        )

    @tool(scope=READ_ONLY, profiles=SEAT)
    def search_work(
        q: str, limit: IssueListLimit = 50, cursor: ListCursor | None = None
    ) -> ListResult:
        """Find issues with a work query: `field:value` atoms joined by AND, `-`
        negates one, e.g. `is:open assignee:@me sort:priority-desc`. Fields:
        is:(open|closed|archived|unassigned), has:(blockers|parent|children|
        labels), status:, priority:, label:, project:(id|KEY|none),
        sprint:(id|none), assignee:(id|@me|none), sort:(created|id|priority|
        status)-(asc|desc). Bare words and "quoted phrases" match title and
        body. Archived issues need is:archived. An unknown field is an error
        naming it, never an empty result."""

        def fetch(rows: int) -> results.CursorPage:
            page = client.search_work(q, limit=rows, cursor=cursor)
            summaries = [results.summary(row) for row in page["issues"]]
            return summaries, page["next_cursor"], page["has_more"]

        return results.cursor_page(fetch, limit=limit)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def count_work(q: str) -> dict:
        """How many issues a work query matches, ignoring paging — so a bounded
        page can be reported as "50 of 340" rather than as the whole answer."""
        return client.count_work(q)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def read_page_embeds(page_id: int) -> ListResult:
        """Resolve a Mentor page's live embeds to DATA, as you.

        A page can carry ```athena blocks that show real work — an issue list, a
        count, a single issue — rendered fresh whenever anyone looks. This returns
        what those blocks resolve to for YOUR visibility, as structured rows
        rather than the HTML a browser gets.

        Use it on an issue's runbook page to see the live work the runbook points
        at, instead of re-deriving it from the prose around it. Each result has a
        `kind` and either its data or an `error` saying why that block did not
        render. Nothing here is stored on the page: the page holds the directive,
        the data is resolved per reader."""
        return listing(client.page_embeds(page_id))

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def resolve_embeds(text: str) -> ListResult:
        """Resolve embed directives in arbitrary text, as you.

        The same resolver read_page_embeds uses. Useful before saving a page: see
        what your ```athena blocks will actually show — including which ones will
        render an error — without writing them first."""
        return listing(client.resolve_embeds(text))

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def embed_help() -> dict:
        """The embed vocabulary as data: every kind, its keys, and the limits.
        Emitted by the parser itself, so it cannot drift from what actually
        renders."""
        return client.embed_help()

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def link_graph(
        kind: str, id: int, depth: int | None = None, max_nodes: int | None = None
    ) -> dict:
        """The link neighbourhood around an issue or page, as you can see it.

        `kind` is "issue" or "page". Returns positioned nodes and edges — the same
        graph the browser draws, as data rather than a picture. Adjacency is
        undirected: what points here and what this points at are both neighbours.

        Bounded on purpose: depth 2 and 40 nodes by default. When the ceiling
        bites, `truncated` is true and `total` says how many visible nodes were
        within range — so a partial neighbourhood is never mistaken for the whole
        one. Nodes you cannot see are absent, and do not conduct a path.

        Use it to orient before editing: what already references this runbook, and
        what does it reach."""
        return client.link_graph(kind, id, depth, max_nodes)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def related_items(kind: str, id: int, limit: int | None = None) -> dict:
        """What cites what this cites, but is NOT linked to it yet.

        `kind` is "issue" or "page". Co-citation over the same links the graph
        walks, derived at read time: each item shares at least one
        link-neighbour with the focus, ranked by how many (`shared`), and
        everything already linked directly is deliberately absent — backlinks
        and outgoing links answer that. This answers the question they cannot:
        what belongs to the same cluster with no edge saying so yet.

        Bounded (10 by default); `truncated`/`total` disclose what the bound
        cut. Nodes you cannot see are absent AND do not raise anyone's score.

        Use it before starting work an issue describes: the runbook nobody
        linked, the sibling issue solving the same subsystem."""
        return client.related_items(kind, id, limit)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def project_timeline(
        project_id: ProjectId,
        max_per_lane: int | None = None,
        max_items: int | None = None,
    ) -> dict:
        """A project's roadmap: sprint lanes, the issues in them, and the declared
        dependencies between those issues — as positioned data, the same picture
        the browser draws.

        Lanes run in date order (sprints with dates first, then undated ones, then
        the backlog), but lane WIDTH is not a duration — sprint dates are optional,
        so the order is the only time claim made. Each card carries its issue's
        status and category, so you can see what is done without another lookup.

        Bounded on purpose: each lane draws its first few issues and the whole
        picture has a ceiling. `truncated` and the per-lane `shown`/`total` say
        what was left out, and `edges_outside` counts dependencies whose other end
        is not on this picture — nothing is silently dropped. Issues you cannot
        see, and archived ones, are absent.

        This is a read. To move an issue between sprints use the issue's own
        sprint assignment; nothing here schedules or reorders work."""
        return client.project_timeline(
            project_id, max_per_lane=max_per_lane, max_items=max_items
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def unlinked_mentions(kind: str, id: int, limit: int | None = None) -> dict:
        """Documents whose text NAMES this issue/page without linking to it.

        `kind` is "issue" or "page". A page is named by its title; an issue by its
        key (ATH-12), never its title. Each result carries the source and an
        excerpt showing the mention in context.

        This is a READ. It proposes edges and creates none — use `link_mention` to
        take one. Occurrences inside code fences, inline code, existing [[refs]],
        and link targets are deliberately not mentions.

        Use it after writing a page: find the docs that already talk about it and
        connect them, instead of hoping someone links it later."""
        return client.unlinked_mentions(kind, id, limit)

    # Writes EITHER module depending on source_kind; the route still enforces
    # the precise scope, this only decides presentation. Not idempotent: it
    # rewrites the next unlinked mention. Not destructive: a page keeps the
    # version it replaced and an issue the body it replaced (F10).
    @mutation_tool(
        scope=ANY_WRITE_SCOPE,
        profiles=FULL_ONLY,
        hints=WriteHint(destructive=False, idempotent=False),
    )
    def link_mention(
        source_kind: str,
        source_id: int,
        target_kind: str,
        target_id: int,
        if_match: str | None = None,
    ) -> dict:
        """Rewrite the SOURCE document so its first unlinked mention of the target
        becomes a real reference.

        This edits `source_id` — not the target — through the ordinary page/issue
        command, so it keeps the text it replaced, records an edit event, and is
        attributed to you like any other write. It rewrites ONE occurrence; call
        again for the next. For an issue source, if_match (the issue's `_etag`)
        is required, as for any issue body edit: without it the call is refused
        428.

        Refused with 409 if the mention is no longer there (the body changed under
        you). That is deliberate: editing anyway would rewrite text you never
        read."""
        return client.link_mention(
            source_kind, source_id, target_kind, target_id, if_match=if_match
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def query_help() -> dict:
        """The work-query vocabulary as data: every field, its accepted values,
        and the limits. Emitted by the parser itself, so it cannot drift from
        what search_work actually accepts."""
        return client.query_help()

    @tool(scope=READ_ONLY, profiles=SEAT)
    def list_my_delegated_work(
        include_closed: bool = False,
        limit: DelegationLimit = 50,
        offset: DelegationOffset = 0,
    ) -> dict:
        """Issues delegated to you, without archived or done work unless
        include_closed: each with its assignee, who delegated it and its visible
        open blockers. Page with has_more and next_offset. Claims nothing."""
        inbox = client.list_my_delegated_work(
            include_closed=include_closed,
            limit=limit,
            offset=offset,
        )
        for item in inbox.get("items", []):
            item["issue"] = results.summary(item["issue"])
        return inbox

    # An admin read: the route carries the admin dependency
    # (aegis/fleet_work_api.py).
    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def get_fleet_active_work(
        agent_id: FleetAgentId | None = None,
        limit: FleetWorkLimit = fleet_work.DEFAULT_LIMIT,
        attention_state: Literal["needs_attention", "observed"] | None = None,
    ) -> dict:
        """Admin-only view of agent-held issue claims. Joins each lease to its
        exact tagged claim run, cooperative check-in, visible blockers, and replay
        readiness. Reporting is an observation, never proof that a process is alive
        or executing work; use attention_reasons to steer by exception.

        Rows needing attention are returned FIRST, and attention_state='needs_attention'
        returns only those. `examined_count` says how many rows the attention
        decision saw: on a clipped fleet, a summary of "0 need attention" means
        "none of these did", not "none exist"."""
        return client.get_fleet_active_work(
            agent_id=agent_id, limit=limit, attention_state=attention_state
        )

    @tool(scope=READ_ONLY, profiles=SEAT)
    def get_issue(
        ref: str | None = None,
        issue_id: int | None = None,
        body_offset: BodyOffset = 0,
    ) -> dict:
        """Get one issue by issue_id or by ref ('12' or 'ATH-12'); pass one. Its
        `_etag` is the if_match a guarded update takes. A long body is cut;
        body_offset reads on."""
        issue = client.get_issue(_issue_ref(ref, issue_id))
        return results.from_offset(issue, "body", body_offset)

    # The one packet read before work: the issue, its twenty newest comments and
    # recent activity in one answer. On the budget test's issue (sixty comments),
    # 16,000 leaves the description 938 characters beside twenty comment excerpts
    # and three events; 32,000 leaves it 16,936. The rest is one get_issue away.
    @tool(scope=READ_ONLY, profiles=SEAT, budget=32_000)
    def get_issue_work_context(
        ref: str | None = None, issue_id: int | None = None, brief: bool = False
    ) -> dict:
        """A bounded, current packet: one visible issue and its
        visible supporting docs, by issue_id or ref; pass one. brief=true keeps
        issue, issue_etag, dependencies, warnings and work_order, for a
        re-read. work_order says which of the six body parts are present
        and missing; check success_criteria before you claim.
        claim_handoffs.open is the handoff awaiting resume. Not a claim or
        lease; it does not guarantee readiness, unblocked status, liveness or
        replayability."""
        reference = _issue_ref(ref, issue_id)
        if brief:
            return client.get_issue_work_context(reference, brief=True)
        return client.get_issue_work_context(reference)

    @tool(scope=READ_ONLY, profiles=CONDUCTOR)
    def get_issue_history(
        issue_id: int,
        limit: IssueHistoryLimit = issue_narrative.DEFAULT_LIMIT,
    ) -> dict:
        """One issue's bounded run narrative; each item cites its source and a
        clipped lane says so."""
        return client.get_issue_history(issue_id, limit=limit)

    @tool(scope=READ_ONLY, profiles=CONDUCTOR)
    def get_issue_prior_text(
        issue_id: int, event_id: int, body_offset: BodyOffset = 0
    ) -> dict:
        """The title and body one edit replaced, as they were. event_id is the
        issue_edited event an `edit` item of get_issue_history names in
        prior_text.next; a field the edit left alone is null. body_offset reads
        on a cut prior_body. The operator's undo restores it."""
        answer = client.get_issue_prior_text(issue_id, event_id)
        return results.from_offset(answer, "prior_body", body_offset)

    @tool(scope=READ_ONLY, profiles=CONDUCTOR)
    def get_attention_ranking(
        signals: list[fleet_attention.AttentionSignal] | None = None,
        window_hours: AttentionWindowHours = fleet_attention.DEFAULT_WINDOW_HOURS,
        limit: AttentionLimit = 20,
    ) -> dict:
        """The bounded "Now" queue you may see: your claim and control rows,
        claims and blockers on issues you answer for (assignee, or delegator of
        the holder), and a HOLD or BLOCKED report there as `held_report` until
        someone else acts. Each row cites source, reason and freshness, and
        labels a next action it never takes. A lapsed lease you answer for is
        recorded once as a `lease_lapsed` event."""
        return client.get_attention_ranking(
            signals=signals,
            window_hours=window_hours,
            limit=limit,
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def get_fleet_metrics(
        start: FleetMetricDate | None = None,
        end: FleetMetricDate | None = None,
        project_id: FleetMetricId | None = None,
        actor_id: FleetMetricId | None = None,
        actor_limit: FleetActorLimit = fleet_metrics.DEFAULT_ACTOR_LIMIT,
    ) -> dict:
        """Read bounded issue throughput for this token's visible scope. Dates are
        UTC YYYY-MM-DD bounds in [start,end); provide both or neither. Created and
        completed are typed event flow, and completion attribution belongs to the
        event performer. Cycle timing requires a full-visibility admin token;
        partial-visibility responses mark it unavailable."""
        return client.get_fleet_metrics(
            start=start,
            end=end,
            project_id=project_id,
            actor_id=actor_id,
            actor_limit=actor_limit,
        )

    @tool(scope=READ_ONLY, profiles=SEAT)
    def list_issue_comments(
        issue_id: int,
        limit: CommentListLimit = 40,
        cursor: ListCursor | None = None,
        include_body: bool = False,
        max_chars: CommentChars = 4_000,
    ) -> ListResult:
        """One issue's comments, oldest first, each an excerpt;
        include_body=true gives bodies up to max_chars, get_issue_comment one
        whole."""

        def fetch(rows: int) -> results.CursorPage:
            page = client.list_issue_comments(issue_id, limit=rows, cursor=cursor)
            shaped = [
                _comment_row(row, include_body, max_chars) for row in page["comments"]
            ]
            return shaped, page["next_cursor"], page["has_more"]

        return results.cursor_page(fetch, limit=limit)

    @tool(scope=READ_ONLY, profiles=SEAT)
    def get_issue_comment(
        issue_id: int, comment_id: int, body_offset: BodyOffset = 0
    ) -> dict:
        """One comment whole; a long body is cut and body_offset reads on."""
        return results.from_offset(
            client.get_issue_comment(issue_id, comment_id), "body", body_offset
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def get_issue_state(issue_id: int, as_of_event_id: int | None = None) -> dict:
        """Reconstruct an issue's lifecycle state from the activity log. Pass
        as_of_event_id to time-travel to the state at that activity checkpoint; omit
        it for the current lifecycle state. Content fields are intentionally absent."""
        return client.get_issue_state(issue_id, as_of_event_id=as_of_event_id)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def recent_events(
        after: int | None = None,
        kind: str | None = None,
        target: int | None = None,
        actor_id: int | None = None,
        actor_type: Literal["agent", "human"] | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        verb: str | None = None,
        project_id: ProjectId | None = None,
        mine: bool = False,
        limit: EventLimit = 50,
    ) -> dict:
        """Read the audit/event feed in ascending event-id order, with REST filters.

        Pass `after` for newer events. Filters may be combined: kind, target
        (requires kind), actor_id, actor_type (agent/human), run_id,
        parent_run_id (events in child runs), verb, project_id (that project's
        events; one you cannot see reads as empty) and mine=true (issues you
        are assigned, delegated or created, or delegated to another account).
        limit defaults to 50,
        from 1 to 200. Returns {events, next_after, has_more}; an empty page
        retains the input after. Each event's stored imported_at is null for a
        native event; any non-null value marks an import, regardless of verb.
        A lease event carries `generation`, a yield or handoff resume
        `handoff_token`, a completion its reported `verdict`; absent otherwise.

        A filtered read can omit intervening events: its next_after cannot
        justify advancing the desk cursor. Advance only after processing the
        unfiltered contiguous visible prefix from your last processed boundary.
        Reading never advances the desk cursor or acknowledges notifications.
        """
        return client.recent_events(
            after=after,
            kind=kind,
            target=target,
            actor_id=actor_id,
            actor_type=actor_type,
            run_id=run_id,
            parent_run_id=parent_run_id,
            verb=verb,
            project_id=project_id,
            mine=mine,
            limit=limit,
        )

    @tool(scope=READ_ONLY, profiles=SEAT)
    def wait_for_events(
        after: int | None = None,
        wait: WaitSeconds = 25,
        kind: str | None = None,
        target: int | None = None,
        actor_id: int | None = None,
        actor_type: Literal["agent", "human"] | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        verb: str | None = None,
        project_id: ProjectId | None = None,
        mine: bool = False,
        limit: EventLimit = 50,
    ) -> dict:
        """Events you may read after `after`, ascending, in place of a sleep
        loop: it holds up to `wait` seconds (0 answers at once) until one
        lands. project_id or mine=true (issues you are assigned, delegated or
        created) narrows what answers. Returns {events, next_after, has_more,
        wait: {outcome, ...}}; outcome is event, timeout (next_after is your
        cursor) or capacity (back off, then wait again). It first records a
        lapsed lease you answer for as one `lease_lapsed` event. A filtered
        read can omit events, so its next_after cannot justify advancing the
        desk cursor."""
        return client.wait_for_events(
            after=after,
            wait=wait,
            kind=kind,
            target=target,
            actor_id=actor_id,
            actor_type=actor_type,
            run_id=run_id,
            parent_run_id=parent_run_id,
            verb=verb,
            project_id=project_id,
            mine=mine,
            limit=limit,
        )

    @tool(scope=READ_ONLY, profiles=SEAT)
    def get_idempotency_key(key: str) -> dict:
        """After a keyed write timed out or was refused idempotency_in_progress
        or idempotency_indeterminate: what a retry under your key would meet
        (`state`, the request, and `retry` in words)."""
        return client.get_idempotency_key(key)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def whoami() -> dict:
        """Who am I? Your identity (id, email, role, agent flag), the acting
        token's effective scopes (null means the auth is not scope-limited), your
        durable action budget (null when unlimited), the action kinds that need
        operator approval before you may take them (`approval_required`), and the
        run identity currently stamped on your writes. `my_desk()` carries the
        same identity lane plus your asks, work and signals, so start there; use
        whoami when you only need the scopes or the run id."""
        return {**client.whoami(), "run": client.current_run()}

    @tool(scope=READ_ONLY, profiles=SEAT)
    def list_notifications(
        unread: bool = False, limit: int = 50, offset: ListOffset = 0
    ) -> ListResult:
        """Your notifications: mentions, watched changes, delegations, lapsed
        leases you answer for; unread=true for the unseen. A row's target_state
        "completed_by_you" means you completed a claim on it since."""
        return window(
            lambda rows: client.list_notifications(unread=unread, limit=rows),
            limit=limit,
            offset=offset,
            cap=NOTIFICATION_LIST_CAP,
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def list_priority_notifications(
        unread: bool = False,
        min_priority: NotificationPriority | None = None,
        include_muted: bool = False,
        digest: bool = False,
        limit: NotificationLimit = 50,
    ) -> dict:
        """Read YOUR inbox as a priority/mute/digest projection. Priority is an
        explicit watch override, then the Aegis issue priority, then `normal`.
        Muted rows are hidden unless include_muted=true. digest=true adds the
        stable digest bucket without claiming that an external delivery occurred."""
        return client.list_priority_notifications(
            unread=unread,
            min_priority=min_priority,
            include_muted=include_muted,
            digest=digest,
            limit=limit,
        )

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def notification_priority_summary(unread: bool = False) -> dict:
        """Count YOUR visible notifications by resolved priority and mute state."""
        return client.notification_priority_summary(unread=unread)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def get_watch_preference(target_kind: WatchKind, target_id: WatchTargetId) -> dict:
        """Read YOUR priority, mute, and digest preference for one active watch."""
        return client.get_watch_preference(target_kind, target_id)

    # An acknowledgement moves a marker forward; what it acknowledges stays
    # readable.
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=SEAT, hints=SETS)
    def mark_notification_read(
        notification_id: WatchTargetId | None = None,
        ids: Annotated[
            list[WatchTargetId] | None,
            Field(min_length=1, max_length=MAX_ACKNOWLEDGE_IDS),
        ] = None,
        through_id: WatchTargetId | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict | None:
        """Mark your notifications read, after handling them. Pass exactly one:
        notification_id (one read or not yours: 404), ids (answers {count,
        not_found}) or through_id (every unread one up to that id; answers
        {count}). It acknowledges reading, not completion."""
        given = [
            name
            for name, value in (
                ("notification_id", notification_id),
                ("ids", ids),
                ("through_id", through_id),
            )
            if value is not None
        ]
        if len(given) != 1:
            raise ValueError("pass exactly one of notification_id, ids or through_id")
        if notification_id is not None:
            return client.mark_notification_read(
                notification_id, idempotency_key=idempotency_key
            )
        return client.acknowledge_notifications(
            ids=ids, through_id=through_id, idempotency_key=idempotency_key
        )

    # Not idempotent: a repeat also marks newer arrivals.
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=FULL_ONLY, hints=ADDS)
    def mark_notifications_read() -> dict:
        """Mark ALL unread notifications in YOUR inbox as read, including rows not shown
        in a bounded read and arrivals since that read (returns the cleared count).
        Use mark_notification_read for one handled item. Bulk acknowledgment is
        deliberate, never implied by reading the desk or completing work."""
        return client.mark_all_notifications_read()

    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def watch(target_kind: WatchKind, target_id: WatchTargetId) -> None:
        """Subscribe YOUR inbox to a target so you learn it changed without
        re-reading it.

        'space' is the one to reach for when a space is your fleet's shared
        memory: it delivers the space's own lifecycle events AND every event on
        every page inside it — created, edited, archived, commented, deleted.
        That is a firehose by design. Per-watch priority, mute, and digest
        preferences shape the read-time projection without creating delivery or
        a second event store; `unwatch` alone stops future fan-out. 'page' follows
        one document, 'issue' one work item.

        Watching twice is a no-op. Watching something you cannot see subscribes
        you to nothing you can read: the inbox filters by visibility at read
        time, so the notifications simply never surface."""
        return client.watch(target_kind, target_id)

    # Destructive: deletes the watch's preference with it.
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=FULL_ONLY, hints=ERASES)
    def unwatch(target_kind: WatchKind, target_id: WatchTargetId) -> None:
        """Unsubscribe YOUR inbox from a target you are watching. Errors 404 if
        you were not watching it — the refusal distinguishes "stopped" from
        "was never subscribed", which a silent success would blur."""
        return client.unwatch(target_kind, target_id)

    # Destructive: replaces the preference; no history is kept.
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=FULL_ONLY, hints=ERASES)
    def set_watch_preference(
        target_kind: WatchKind,
        target_id: WatchTargetId,
        priority: WatchPreferencePriority | None = None,
        mute_until: MuteUntil | None = None,
        digest_window_minutes: DigestWindowMinutes | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Replace YOUR preference for an active watch. Omit or pass null for a
        field to restore its default. This is owner-scoped personal state and
        records no activity event; it cannot create a subscription by itself."""
        return client.set_watch_preference(
            target_kind,
            target_id,
            priority=priority,
            mute_until=mute_until,
            digest_window_minutes=digest_window_minutes,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=FULL_ONLY, hints=ERASES)
    def clear_watch_preference(
        target_kind: WatchKind,
        target_id: WatchTargetId,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict | None:
        """Delete YOUR preference for a watch, restoring target/default priority."""
        return client.clear_watch_preference(
            target_kind, target_id, idempotency_key=idempotency_key
        )

    # Liveness: every call refreshes last-seen (and usage counts again).
    @mutation_tool(scope=ANY_WRITE_SCOPE, profiles=SEAT, hints=ADDS)
    def heartbeat_agent_run(
        run_id: RunId | None = None, usage: dict | None = None
    ) -> dict:
        """Say you are still working on this session's run (omit run_id); call
        it repeatedly. Optional usage is the work since your last report (keys:
        docs/CLOSEOUT_RECIPE.md). Takes no idempotency key: report each usage
        interval only once."""
        return client.heartbeat_agent_run(run_id, usage)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def begin_run(
        run_id: RunId,
        parent_run_id: RunId | None = None,
        fork_from_event_id: int | None = None,
    ) -> dict:
        """Switch this session's run identity: every write you make afterwards is
        attributed to `run_id` in the activity trail (replayable, lineage-linked).
        A session already starts with an auto-minted run id, so call this when you
        begin a NEW unit of work, continue a run you were assigned, or apply the
        `headers` from get_run_fork_contract (pass its run/parent/fork values here
        to work on the fork). Setting a new run clears the previous parent/fork
        context. Returns the now-active identity."""
        return client.set_run(
            run_id,
            parent_run_id=parent_run_id,
            fork_from_event_id=fork_from_event_id,
        )

    @tool(scope=READ_ONLY, profiles=SEAT)
    def current_run() -> dict:
        """The run identity this session stamps on writes."""
        return client.current_run()

    # An admin read: the route carries the admin dependency
    # (core/activity_api.py).
    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def get_agent_run_health(agent_id: int | None = None) -> dict:
        """Read the admin-only fleet cockpit rollup: each agent's bounded recent
        runs, cooperative check-ins, replay posture, lineage counts, and totals.
        Check-ins are self-reports; they do not prove an OS process is alive.
        Each agent also has last_refusal (the newest 4xx it was answered: code,
        status_code, request, at) and last_nothing_ready_at (when a pickup last
        answered nothing_ready); both null when none is kept. Diagnosis only."""
        return client.get_agent_run_health(agent_id=agent_id)

    # An admin read: the route carries the admin dependency
    # (aegis/automation_api.py).
    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def list_automation_rules() -> ListResult:
        """List every admin-only automation rule with its event or schedule
        configuration, progress, failure health, and enabled state."""
        return listing(client.list_automation_rules())

    # An admin read: the route carries the admin dependency
    # (aegis/automation_api.py).
    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def get_automation_rule(rule_id: int) -> dict:
        """Get one admin-only automation rule by id, including schedule progress,
        configuration errors, failure health, and enabled state."""
        return client.get_automation_rule(rule_id)

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=ADDS)
    def create_automation_rule(
        name: str,
        trigger_verb: str,
        action_type: str,
        conditions: dict | None = None,
        action_params: dict | None = None,
        target_kind: str = "issue",
        trigger_type: AutomationTriggerType = "event",
        schedule_at: AutomationScheduleAt | None = None,
        schedule_every_seconds: AutomationScheduleInterval | None = None,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Create an admin-only automation rule. Event rules use trigger_type='event'
        and an activity trigger_verb. Schedule rules use trigger_type='schedule',
        trigger_verb='scheduled', canonical UTC schedule_at (YYYY-MM-DDTHH:MM:SSZ),
        and optional schedule_every_seconds; omit the interval for a one-shot rule.
        conditions select issues and action_params configure the requested action."""
        return client.create_automation_rule(
            name=name,
            trigger_verb=trigger_verb,
            action_type=action_type,
            conditions=conditions,
            action_params=action_params,
            target_kind=target_kind,
            trigger_type=trigger_type,
            schedule_at=schedule_at,
            schedule_every_seconds=schedule_every_seconds,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=SETS)
    def set_automation_rule_enabled(
        rule_id: int,
        enabled: bool,
        idempotency_key: IdempotencyKey | None = None,
    ) -> dict:
        """Arm or disarm an admin-only automation rule without deleting its
        configuration or history."""
        return client.set_automation_rule_enabled(
            rule_id,
            enabled,
            idempotency_key=idempotency_key,
        )

    @mutation_tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=ERASES)
    def delete_automation_rule(
        rule_id: int, idempotency_key: IdempotencyKey | None = None
    ) -> dict | None:
        """Permanently delete an admin-only automation rule. Disable it instead when
        the operator may need to resume the same rule later."""
        return client.delete_automation_rule(
            rule_id,
            idempotency_key=idempotency_key,
        )

    # An admin read: the route carries the admin dependency
    # (aegis/automation_api.py).
    @tool(scope=tokens.ADMIN_SCOPE, profiles=FULL_ONLY, hints=READS)
    def list_automation_failures() -> ListResult:
        """Read the admin-only exception list of automation rules whose actions have
        failed. Failure counts are cumulative; inspect the rule before intervening."""
        return listing(client.list_automation_failures())

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def list_activity_runs(
        actor_id: int, gap_seconds: int = 1800, limit: int = 200
    ) -> ListResult:
        """Reconstruct one actor's recent activity into runs. Explicit X-Athena-Run
        ids are authoritative; untagged work falls back to a time-gap heuristic."""
        runs = client.list_activity_runs(
            actor_id=actor_id, gap_seconds=gap_seconds, limit=limit
        )
        # Only the oldest run can be clipped by the event window; widening the
        # window is how this route continues.
        if not any(run.get("partial") for run in runs):
            return listing(runs)
        wider = min(limit * 2, ACTIVITY_RUNS_EVENT_CAP)
        if wider == limit:
            return {"items": runs, "count": len(runs), "truncated": True, "next": None}
        return listing(runs, truncated=True, next={"limit": wider})

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def list_run_events(
        run_id: str, before_id: int | None = None, limit: int = 100
    ) -> ListResult:
        """Replay one run: exactly the activity events tagged with this run id,
        newest first (page older history with before_id). Use it to review what
        a run — yours or another agent's — actually did."""
        events = client.list_run_events(run_id, before_id=before_id, limit=limit)
        if len(events) < limit or not events:
            return listing(events)
        return listing(events, truncated=True, next={"before_id": events[-1]["id"]})

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def get_run_lineage(run_id: str) -> dict:
        """Read a tagged run's causal tree: ancestors, the focal run's replayable
        events, and descendant runs spawned from it."""
        return client.get_run_lineage(run_id)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def get_run_replay(run_id: str) -> dict:
        """Export one run as its portable replay ARTIFACT: the events in replay
        order plus lineage placement and a determinism contract, frozen from one
        consistent snapshot. Use list_run_events for a quick look; use this when
        handing a run to another agent or preserving it for audit. Hidden or
        unknown runs are a clean not-found."""
        return client.get_run_replay(run_id)

    @tool(scope=READ_ONLY, profiles=FULL_ONLY)
    def get_run_fork_contract(
        run_id: str, fork_from_event_id: int, fork_run_id: str
    ) -> dict:
        """Validate a fork point inside a parent run and return the child-run
        headers to use on subsequent writes, plus the visible shared-prefix events.
        This creates no state; pass the returned run/parent/fork values to begin_run
        and the child run starts with your next write."""
        return client.get_run_fork_contract(
            run_id,
            fork_from_event_id=fork_from_event_id,
            fork_run_id=fork_run_id,
        )
