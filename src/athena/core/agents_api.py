"""Admin REST API for agent accounts (agent admin V2).

JSON surface over the same read models and write helpers the browser admin UI
uses. Agents remain ordinary users; this only exposes supervision + policy
actions (revoke tokens, membership grants, role/agent flags, scope presets).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from athena.core import access, agents, tokens, users
from athena.core.deps import get_conn
from athena.core.identity import admin_actor

router = APIRouter(prefix="/api/admin/agents", tags=["admin-agents"])


class RoleBody(BaseModel):
    role: str


class AgentFlagBody(BaseModel):
    is_agent: bool


class MembershipBody(BaseModel):
    project_id: int | None = None
    space_id: int | None = None


class BulkMembershipBody(BaseModel):
    project_ids: list[int] = Field(default_factory=list)
    space_ids: list[int] = Field(default_factory=list)


class MintTokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    # Either an explicit scope list or a named preset. Default matches the
    # historical "aegis worker" mint (read + issue:write).
    scopes: list[str] | None = None
    preset: str | None = None


@router.get("")
def list_agents(
    conn: sqlite3.Connection = Depends(get_conn),
    _admin: dict = Depends(admin_actor),
) -> dict:
    summaries = agents.agent_admin_summaries(conn)
    return {
        "agents": summaries,
        "count": len(summaries),
        "run_health": agents.agent_run_health(conn)["totals"],
        "scope_presets": tokens.list_scope_presets(),
    }


@router.get("/scope-presets")
def list_scope_presets(
    _admin: dict = Depends(admin_actor),
) -> dict:
    """Named token-scope presets for agent minting (static catalog)."""
    presets = tokens.list_scope_presets()
    return {"presets": presets, "count": len(presets)}


@router.get("/{agent_id}")
def get_agent(
    agent_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    _admin: dict = Depends(admin_actor),
) -> dict:
    summary = _agent_summary_or_404(conn, agent_id)
    health = agents.agent_run_health(conn, agent_id=agent_id)
    return {
        **summary,
        "run_health": health["agents"][0] if health["agents"] else None,
        "scope_presets": tokens.list_scope_presets(),
    }


@router.post("/{agent_id}/tokens/revoke-all")
def revoke_all_agent_tokens(
    agent_id: int,
    conn: sqlite3.Connection = Depends(get_conn),
    admin: dict = Depends(admin_actor),
) -> dict:
    _agent_or_404(conn, agent_id)
    revoked = tokens.revoke_all_tokens(conn, user_id=agent_id)
    return {
        "agent_id": agent_id,
        "revoked": revoked,
        "actor_id": admin["id"],
    }


@router.post("/{agent_id}/tokens")
def mint_agent_token(
    agent_id: int,
    body: MintTokenBody,
    conn: sqlite3.Connection = Depends(get_conn),
    admin: dict = Depends(admin_actor),
) -> dict:
    _agent_or_404(conn, agent_id)
    try:
        scopes = _resolve_mint_scopes(body)
        created = tokens.create_token(
            conn,
            user_id=agent_id,
            name=body.name.strip(),
            scopes=scopes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # raw token is only available at create time
    return {
        "agent_id": agent_id,
        "token": created,
        "preset": body.preset.strip().lower() if body.preset else None,
        "actor_id": admin["id"],
        "note": "Store the raw token now; it is not retrievable later.",
    }


@router.patch("/{agent_id}/role")
def set_agent_role(
    agent_id: int,
    body: RoleBody,
    conn: sqlite3.Connection = Depends(get_conn),
    admin: dict = Depends(admin_actor),
) -> dict:
    agent = _agent_or_404(conn, agent_id)
    try:
        if (
            agent["role"] == users.ADMIN_ROLE
            and body.role != users.ADMIN_ROLE
            and users.count_admins(conn) <= 1
        ):
            raise HTTPException(
                status_code=422,
                detail="cannot demote the last admin",
            )
        updated = users.set_role(conn, agent_id, body.role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return {"agent": updated, "actor_id": admin["id"]}


@router.patch("/{agent_id}/agent-flag")
def set_agent_flag(
    agent_id: int,
    body: AgentFlagBody,
    conn: sqlite3.Connection = Depends(get_conn),
    admin: dict = Depends(admin_actor),
) -> dict:
    user = users.get_user(conn, agent_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    updated = users.set_agent(conn, agent_id, body.is_agent)
    return {"user": updated, "actor_id": admin["id"]}


@router.post("/{agent_id}/memberships")
def grant_membership(
    agent_id: int,
    body: MembershipBody,
    conn: sqlite3.Connection = Depends(get_conn),
    admin: dict = Depends(admin_actor),
) -> dict:
    _agent_or_404(conn, agent_id)
    if (body.project_id is None) == (body.space_id is None):
        raise HTTPException(
            status_code=422,
            detail="provide exactly one of project_id or space_id",
        )
    if body.project_id is not None:
        try:
            changed = access.add_project_member(
                conn, body.project_id, agent_id, admin["id"]
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=404, detail="project or user not found"
            ) from exc
        return {
            "agent_id": agent_id,
            "project_id": body.project_id,
            "granted": True,
            "changed": bool(changed),
        }
    try:
        changed = access.add_space_member(
            conn, body.space_id, agent_id, admin["id"]
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=404, detail="space or user not found"
        ) from exc
    return {
        "agent_id": agent_id,
        "space_id": body.space_id,
        "granted": True,
        "changed": bool(changed),
    }


@router.post("/{agent_id}/memberships/bulk")
def grant_memberships_bulk(
    agent_id: int,
    body: BulkMembershipBody,
    conn: sqlite3.Connection = Depends(get_conn),
    admin: dict = Depends(admin_actor),
) -> dict:
    """Grant many project/space memberships in one call (idempotent per id)."""
    _agent_or_404(conn, agent_id)
    project_ids = _unique_positive_ids(body.project_ids)
    space_ids = _unique_positive_ids(body.space_ids)
    if not project_ids and not space_ids:
        raise HTTPException(
            status_code=422,
            detail="provide at least one project_id or space_id",
        )
    projects = access.add_project_members(
        conn, agent_id, project_ids, admin["id"]
    )
    spaces = access.add_space_members(conn, agent_id, space_ids, admin["id"])
    return {
        "agent_id": agent_id,
        "projects": projects,
        "spaces": spaces,
        "actor_id": admin["id"],
    }


def _resolve_mint_scopes(body: MintTokenBody) -> list[str]:
    """Resolve mint scopes from an explicit list or a named preset."""
    has_preset = body.preset is not None and body.preset.strip() != ""
    has_scopes = body.scopes is not None
    if has_preset and has_scopes:
        raise ValueError("provide either preset or scopes, not both")
    if has_preset:
        return list(tokens.resolve_scope_preset(body.preset or ""))
    if has_scopes:
        if not body.scopes:
            raise ValueError("at least one token scope is required")
        return list(body.scopes)
    return [tokens.READ_SCOPE, tokens.ISSUE_WRITE_SCOPE]


def _unique_positive_ids(raw: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for value in raw:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item < 1 or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _agent_or_404(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Return the agent user row or 404."""
    user = users.get_user(conn, agent_id)
    if user is None or not user.get("is_agent"):
        raise HTTPException(status_code=404, detail="agent not found")
    return user


def _agent_summary_or_404(conn: sqlite3.Connection, agent_id: int) -> dict:
    """Return the full admin summary for one agent or 404."""
    _agent_or_404(conn, agent_id)
    for summary in agents.agent_admin_summaries(conn):
        if summary["user"]["id"] == agent_id:
            return summary
    raise HTTPException(status_code=404, detail="agent not found")
