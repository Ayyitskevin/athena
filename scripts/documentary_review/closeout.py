"""Presentation of an explicitly supplied desk snapshot. Never performs actions."""

from datetime import datetime, timezone


def closeout_view(desk: dict, *, as_of: str) -> dict:
    if desk.get("schema") != "athena.agent_desk.v2":
        raise ValueError("unsupported desk snapshot")
    now = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if now.tzinfo is None:
        raise ValueError("as_of must carry an explicit timezone")

    def age(stamp):
        try:
            then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            # Athena's SQLite UTC timestamps have no suffix.
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            seconds = int((now - then).total_seconds())
            return seconds if seconds >= 0 else None
        except (ValueError, AttributeError, TypeError):
            return None

    leases = desk["work"]["leases"]
    rows = []
    for kind, collection, clock, action in (
        (
            "active_lease",
            "items",
            "claimed_at",
            "inspect get_issue_lease; finish or deliberately yield current work under its exact generation",
        ),
        (
            "lapsed_lease",
            "lapsed",
            "expires_at",
            "inspect get_issue_lease; deliberately complete_claim with the retained generation if clearing this residual row; this does not close the issue",
        ),
    ):
        for row in leases[collection]:
            rows.append(
                {
                    "kind": kind,
                    "pointer": f"/issues/{row['issue_id']}/lease",
                    "generation": row["generation"],
                    "age_basis": clock,
                    "age_seconds": age(row.get(clock)),
                    "snapshot_active": row["active"],
                    "deliberate_action": action,
                }
            )
    notices = desk["signals"]["notifications"]
    for row in notices:
        if row["read_at"] is not None:
            raise ValueError("expected an unread desk subset; do not relabel read rows")
        rows.append(
            {
                "kind": "unread_notification",
                "pointer": f"/notifications/{row['id']}/read",
                "target": f"{row['target_kind']}:{row['target_id']}",
                "age_basis": "created_at",
                "age_seconds": age(row.get("created_at")),
                "deliberate_action": "read the item; deliberately acknowledge only that item through existing REST (or targeted MCP when adopted); not work completion",
            }
        )
    return {
        "as_of": as_of,
        "rows": rows,
        "unshown_active": max(0, leases["total"] - len(leases["items"])),
        "unshown_lapsed": max(0, leases["lapsed_total"] - len(leases["lapsed"])),
        "unshown_unread": max(
            0, desk["signals"]["unread_notifications"] - len(notices)
        ),
        "cursor": dict(desk["cursor"]),
        "automatic_actions": [],
        "limits": "snapshot presentation only; age is not urgency, worker death or authority; cursor/run-control/issue actions are separate",
    }
