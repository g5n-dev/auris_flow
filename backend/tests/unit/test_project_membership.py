from __future__ import annotations

from app.core.project_membership import (
    conflicting_project_member_identities,
    duplicate_project_member_user_ids,
    project_member_role_binding,
)
from app.models import Project


def _project(members: object) -> Project:
    return Project(
        project_id="project_membership_test",
        tenant_id="aurora_auto",
        name="Membership Test",
        status="active",
        data={"members": members},
    )


def test_role_binding_uses_one_canonical_member_identity() -> None:
    project = _project([{"user_id": "u_1", "roles": ["annotator", "annotator"]}])

    binding = project_member_role_binding(project, "u_1")

    assert binding.configured is True
    assert binding.duplicate is False
    assert binding.roles == ("annotator",)


def test_user_id_and_id_aliases_are_duplicate_member_bindings() -> None:
    members = [
        {"user_id": "u_1", "roles": ["project_admin"]},
        {"id": "u_1", "roles": ["annotator"]},
    ]
    project = _project(members)

    binding = project_member_role_binding(project, "u_1")

    assert binding.duplicate is True
    assert duplicate_project_member_user_ids(members) == ("u_1",)


def test_conflicting_user_id_and_id_in_one_member_are_ambiguous() -> None:
    members = [{"user_id": "u_1", "id": "u_2", "roles": ["project_admin"]}]
    project = _project(members)

    first_binding = project_member_role_binding(project, "u_1")
    second_binding = project_member_role_binding(project, "u_2")

    assert first_binding.configured is False
    assert second_binding.configured is False
    assert conflicting_project_member_identities(members) == (("u_1", "u_2"),)
