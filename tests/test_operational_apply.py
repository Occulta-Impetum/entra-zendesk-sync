"""Tests for guarded operational write helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from lib.operational_apply import (
    OperationalApplyError,
    execute_incremental_plan,
    repair_reused_email_and_create,
)

FIELD_KEYS = {
    "employee_id": "employee_id",
    "job_title": "standard::job_title",
    "manager": "standard::manager",
}


class ReusedEmailApplyTests(unittest.TestCase):
    def _row(self) -> dict:
        return {
            "entra_id": "new-id",
            "name": "New Person",
            "email": "jsmith@company.com",
            "employee_id": "999999",
            "job_title": "Tech",
            "zendesk_org_id": 42,
            "old_zendesk_id": 50,
            "old_entra_id": "old-id",
            "old_employee_id": "123456",
            "rename_old_email_to": "jsmith123456@company.com",
        }

    def _old_user(self, *, suspended: bool = True) -> dict:
        return {
            "id": 50,
            "email": "jsmith@company.com",
            "external_id": "entra:old-id",
            "role": "end-user",
            "suspended": suspended,
        }

    @patch("lib.operational_apply.create_user")
    @patch("lib.operational_apply.find_users_by_email")
    @patch("lib.operational_apply.get_user")
    @patch("lib.operational_apply.rename_primary_email_identity")
    def test_renames_old_primary_then_creates_new_user(
        self,
        rename_mock,
        get_user_mock,
        search_mock,
        create_mock,
    ) -> None:
        get_user_mock.return_value = self._old_user()
        search_mock.side_effect = [
            [{"id": 50, "email": "jsmith@company.com"}],
            [],
            [],
        ]
        create_mock.return_value = {"id": 200}

        result = repair_reused_email_and_create(
            self._row(),
            user_token="user-token",
            identity_token="identity-token",
            subdomain="example",
            field_keys=FIELD_KEYS,
        )

        self.assertEqual(result["id"], 200)
        get_user_mock.assert_called_once_with("user-token", "example", 50)
        rename_mock.assert_called_once_with("identity-token", "example", 50, "jsmith123456@company.com")
        self.assertEqual(search_mock.call_count, 3)
        create_mock.assert_called_once()
        self.assertEqual(create_mock.call_args.kwargs["external_id"], "entra:new-id")

    @patch("lib.operational_apply.create_user")
    @patch("lib.operational_apply.find_users_by_email")
    @patch("lib.operational_apply.get_user")
    @patch("lib.operational_apply.rename_primary_email_identity")
    def test_does_not_create_if_original_email_is_still_owned_after_rename(
        self,
        rename_mock,
        get_user_mock,
        search_mock,
        create_mock,
    ) -> None:
        get_user_mock.return_value = self._old_user()
        search_mock.side_effect = [
            [{"id": 50, "email": "jsmith@company.com"}],
            [],
            [{"id": 99, "email": "jsmith@company.com"}],
        ]

        with self.assertRaises(OperationalApplyError):
            repair_reused_email_and_create(
                self._row(),
                user_token="user-token",
                identity_token="identity-token",
                subdomain="example",
                field_keys=FIELD_KEYS,
            )
        rename_mock.assert_called_once()
        create_mock.assert_not_called()

    @patch("lib.operational_apply.create_user")
    @patch("lib.operational_apply.find_users_by_email")
    @patch("lib.operational_apply.get_user")
    @patch("lib.operational_apply.rename_primary_email_identity")
    def test_rechecks_suspended_state_immediately_before_reuse(
        self,
        rename_mock,
        get_user_mock,
        search_mock,
        create_mock,
    ) -> None:
        get_user_mock.return_value = self._old_user(suspended=False)

        with self.assertRaises(OperationalApplyError):
            repair_reused_email_and_create(
                self._row(),
                user_token="user-token",
                identity_token="identity-token",
                subdomain="example",
                field_keys=FIELD_KEYS,
            )
        search_mock.assert_not_called()
        rename_mock.assert_not_called()
        create_mock.assert_not_called()


class ManagerSecondPassTests(unittest.TestCase):
    @patch("lib.operational_apply.save_entra_users_cache")
    @patch("lib.operational_apply.update_user")
    @patch("lib.operational_apply.find_users_by_external_id")
    @patch("lib.operational_apply.create_user")
    @patch("lib.operational_apply.get_access_token")
    @patch("lib.operational_apply.load_zendesk_config")
    def test_new_employee_can_reference_manager_created_same_run(
        self,
        config_mock,
        token_mock,
        create_mock,
        external_mock,
        update_mock,
        cache_mock,
    ) -> None:
        config_mock.return_value = {"subdomain": "example"}
        token_mock.return_value = ("write-token", {"scope": "users:read users:write"})
        create_mock.side_effect = [{"id": 201}, {"id": 202}]
        cache_mock.return_value = "cache/entra_users.json"

        def find_external(_token: str, _subdomain: str, external_id: str) -> list[dict]:
            if external_id == "entra:employee-id":
                return [{"id": 202, "external_id": external_id}]
            if external_id == "entra:manager-id":
                return [{"id": 201, "external_id": external_id}]
            return []

        external_mock.side_effect = find_external
        plan = [
            {
                "entra_id": "manager-id",
                "name": "Manager",
                "email": "manager@example.com",
                "employee_id": "1",
                "job_title": "Manager",
                "zendesk_org_id": 42,
                "action": "CREATE",
                "fields_to_write": {},
            },
            {
                "entra_id": "employee-id",
                "name": "Employee",
                "email": "employee@example.com",
                "employee_id": "2",
                "job_title": "Tech",
                "manager_entra_id": "manager-id",
                "manager_email": "manager@example.com",
                "zendesk_org_id": 42,
                "action": "CREATE + UPDATE MANAGER",
                "fields_to_write": {},
                "manager_deferred": True,
            },
        ]
        current = {row["entra_id"]: row for row in plan}

        written, _skipped = execute_incremental_plan(
            plan,
            current,
            {"version": 1, "current": {}, "history": {}},
            field_keys=FIELD_KEYS,
        )

        self.assertEqual(create_mock.call_count, 2)
        update_mock.assert_called_once_with(
            "write-token",
            "example",
            202,
            fields={"user_fields": {"standard::manager": 201}},
        )
        self.assertEqual(written, 3)
        cache_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
