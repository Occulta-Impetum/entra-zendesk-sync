from __future__ import annotations

import unittest

from lib.reconcile import add_user_field_actions, plan_reconciliation


FIELD_KEYS = {
    "employee_id": "employee_id",
    "job_title": "standard::job_title",
    "manager": "standard::manager",
}


def desired_user(
    *,
    entra_id: str = "entra-user",
    email: str = "person@example.com",
    manager_entra_id: str = "",
    manager_email: str = "",
    manager_name: str = "",
) -> dict:
    return {
        "entra_id": entra_id,
        "name": "Person Example",
        "email": email,
        "enabled": True,
        "employee_id": "1234",
        "job_title": "Example Role",
        "manager_entra_id": manager_entra_id,
        "manager_name": manager_name,
        "manager_email": manager_email,
        "group_id": "group-1",
        "group_name": "Example Group",
        "zendesk_org_id": 42,
        "zendesk_org_name": "Example Org",
    }


class OperationalFullReconcileIdentityTests(unittest.TestCase):
    def test_missing_external_id_with_staff_email_collision_is_protected(self) -> None:
        desired = {"entra-user": desired_user(email="tsalter@example.com")}
        zendesk = [
            {
                "id": 50,
                "name": "Taylor Salter",
                "email": "tsalter@example.com",
                "external_id": "",
                "organization_id": 42,
                "role": "agent",
                "suspended": False,
                "user_fields": {},
            }
        ]

        plan = plan_reconciliation(
            desired,
            zendesk,
            in_scope_entra_ids={"entra-user"},
            allow_email_bootstrap=False,
            protect_zendesk_staff_roles=True,
        )

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["action"], "PROTECTED")
        self.assertEqual(plan[0]["zendesk_id"], 50)
        self.assertEqual(plan[0]["matched_by"], "email_collision")
        self.assertNotIn("CREATE", plan[0]["action"])

    def test_missing_external_id_with_end_user_email_collision_is_conflict(self) -> None:
        desired = {"entra-user": desired_user()}
        zendesk = [
            {
                "id": 51,
                "name": "Existing Person",
                "email": "person@example.com",
                "external_id": "entra:someone-else",
                "organization_id": 42,
                "role": "end-user",
                "suspended": True,
                "user_fields": {},
            }
        ]

        plan = plan_reconciliation(
            desired,
            zendesk,
            in_scope_entra_ids={"entra-user"},
            allow_email_bootstrap=False,
        )

        self.assertEqual(plan[0]["action"], "CONFLICT")
        self.assertEqual(plan[0]["conflict_type"], "operational_email_collision")


class FullReconcileManagerTests(unittest.TestCase):
    def test_out_of_scope_manager_resolves_against_full_zendesk_snapshot(self) -> None:
        desired = {
            "employee-id": desired_user(
                entra_id="employee-id",
                email="employee@example.com",
                manager_entra_id="manager-id",
                manager_email="manager@example.com",
                manager_name="Manager Example",
            )
        }
        zendesk = [
            {
                "id": 100,
                "name": "Person Example",
                "email": "employee@example.com",
                "external_id": "entra:employee-id",
                "organization_id": 42,
                "role": "end-user",
                "suspended": False,
                "user_fields": {
                    "employee_id": "1234",
                    "standard::job_title": "Example Role",
                    "standard::manager": "200",
                },
            },
            {
                "id": 200,
                "name": "Manager Example",
                "email": "manager@example.com",
                "external_id": "entra:manager-id",
                "organization_id": None,
                "role": "agent",
                "suspended": False,
                "user_fields": {},
            },
        ]

        plan = plan_reconciliation(
            desired,
            zendesk,
            in_scope_entra_ids={"employee-id"},
            allow_email_bootstrap=False,
        )
        plan = add_user_field_actions(plan, desired, zendesk, field_keys=FIELD_KEYS)

        employee_row = next(row for row in plan if row.get("entra_id") == "employee-id")
        self.assertEqual(employee_row["action"], "NO CHANGE")
        self.assertEqual(employee_row["manager_zendesk_id"], 200)
        self.assertNotIn("UPDATE MANAGER", employee_row["action"])

    def test_manager_email_fallback_is_relationship_resolution_only(self) -> None:
        desired = {
            "employee-id": desired_user(
                entra_id="employee-id",
                email="employee@example.com",
                manager_entra_id="manager-id",
                manager_email="manager@example.com",
                manager_name="Manager Example",
            )
        }
        zendesk = [
            {
                "id": 100,
                "name": "Person Example",
                "email": "employee@example.com",
                "external_id": "entra:employee-id",
                "organization_id": 42,
                "role": "end-user",
                "suspended": False,
                "user_fields": {
                    "employee_id": "1234",
                    "standard::job_title": "Example Role",
                    "standard::manager": "200",
                },
            },
            {
                "id": 200,
                "name": "Manager Example",
                "email": "manager@example.com",
                "external_id": "",
                "organization_id": None,
                "role": "agent",
                "suspended": False,
                "user_fields": {},
            },
        ]

        plan = plan_reconciliation(
            desired,
            zendesk,
            in_scope_entra_ids={"employee-id"},
            allow_email_bootstrap=False,
        )
        plan = add_user_field_actions(plan, desired, zendesk, field_keys=FIELD_KEYS)

        employee_row = next(row for row in plan if row.get("entra_id") == "employee-id")
        self.assertEqual(employee_row["action"], "NO CHANGE")
        self.assertEqual(employee_row["manager_zendesk_id"], 200)
        self.assertIn("email fallback", employee_row.get("manager_resolution", ""))


if __name__ == "__main__":
    unittest.main()
