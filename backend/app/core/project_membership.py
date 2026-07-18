"""Canonical parsing for project membership and role bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models import Project


@dataclass(frozen=True)
class ProjectMemberRoleBinding:
    configured: bool
    roles: tuple[str, ...]
    duplicate: bool = False


def project_member_user_id(member: object) -> str | None:
    if not isinstance(member, dict):
        return None
    user_id = member.get("user_id")
    legacy_id = member.get("id")
    canonical_user_id = user_id if isinstance(user_id, str) and user_id else None
    canonical_legacy_id = legacy_id if isinstance(legacy_id, str) and legacy_id else None
    if (
        canonical_user_id is not None
        and canonical_legacy_id is not None
        and canonical_user_id != canonical_legacy_id
    ):
        return None
    return canonical_user_id or canonical_legacy_id


def conflicting_project_member_identities(members: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(members, list):
        return ()
    conflicts: set[tuple[str, str]] = set()
    for member in members:
        if not isinstance(member, dict):
            continue
        user_id = member.get("user_id")
        legacy_id = member.get("id")
        if (
            isinstance(user_id, str)
            and user_id
            and isinstance(legacy_id, str)
            and legacy_id
            and user_id != legacy_id
        ):
            conflicts.add((user_id, legacy_id))
    return tuple(sorted(conflicts))


def duplicate_project_member_user_ids(members: object) -> tuple[str, ...]:
    if not isinstance(members, list):
        return ()
    seen: set[str] = set()
    duplicates: set[str] = set()
    for member in members:
        user_id = project_member_user_id(member)
        if user_id is None:
            continue
        if user_id in seen:
            duplicates.add(user_id)
        seen.add(user_id)
    return tuple(sorted(duplicates))


def project_member_user_ids(project: Project) -> tuple[str, ...]:
    data: dict[str, Any] = project.data or {}
    member_ids: set[str] = set()
    for key in ("member_user_ids", "user_ids"):
        values = data.get(key)
        if isinstance(values, str) and values:
            member_ids.add(values)
        elif isinstance(values, list):
            member_ids.update(value for value in values if isinstance(value, str) and value)
    members = data.get("members")
    if isinstance(members, list):
        member_ids.update(
            user_id for member in members if (user_id := project_member_user_id(member)) is not None
        )
    return tuple(sorted(member_ids))


def user_has_project_membership(project: Project, user_id: str) -> bool:
    return user_id in project_member_user_ids(project)


def project_member_role_binding(project: Project, user_id: str) -> ProjectMemberRoleBinding:
    members = (project.data or {}).get("members")
    if not isinstance(members, list):
        return ProjectMemberRoleBinding(configured=False, roles=())
    matching = [member for member in members if project_member_user_id(member) == user_id]
    if not matching:
        return ProjectMemberRoleBinding(configured=False, roles=())
    configured = any(isinstance(member, dict) and "roles" in member for member in matching)
    roles = {
        role
        for member in matching
        if isinstance(member, dict) and isinstance(member.get("roles"), list)
        for role in member["roles"]
        if isinstance(role, str) and role
    }
    return ProjectMemberRoleBinding(
        configured=configured,
        roles=tuple(sorted(roles)),
        duplicate=len(matching) > 1,
    )


def project_member_roles(project: Project, user_id: str) -> tuple[str, ...]:
    binding = project_member_role_binding(project, user_id)
    return () if binding.duplicate else binding.roles
