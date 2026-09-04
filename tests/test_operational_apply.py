"""Tests for guarded operational write helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from lib.operational_apply import OperationalApplyError, repair_reused_email_and_create


class ReusedEmailApplyTests(unittest.TestCase):
    @patch("lib.operational_apply.create_user")
    @patch("lib.operational_apply.find_users_by_email")
    @patch("lib.operational_apply.rename_primary_email_identity")
    def test_renames_old_primary_then_creates_new_user(self, rename_mock, search_mock, create_mock) -> None:
        search_mock.return_value = []
        create_mock.return_value = {"id": 200}
        row = {
            "entra_id": "new-id",
            "name": "New Person",
            "email": "jsmith@company.com",
            "employee_id": "999999",
            "job_title": "Tech",
            "zendesk_org_id": 42,
            "old_zendesk_id": 50,
            "rename_old_email_to": "jsmith123456@company.com",
        }
        result = repair_reused_email_and_create(
            row,
            user_token="user-token",
            identity_token="identity-token",
            subdomain="example",
            field_keys={"employee_id": "employee_id", "job_title": "standard::job_title", "manager": "standard::manager"},
        )
        self.assertEqual(result["id"], 200)
        rename_mock.assert_called_once_with("identity-token", "example", 50, "jsmith123456@company.com")
        search_mock.assert_called_once_with("user-token", "example", "jsmith@company.com")
        create_mock.assert_called_once()
        self.assertEqual(create_mock.call_args.kwargs["external_id"], "entra:new-id")

    @patch("lib.operational_apply.create_user")
    @patch("lib.operational_apply.find_users_by_email")
    @patch("lib.operational_apply.rename_primary_email_identity")
    def test_does_not_create_if_original_email_is_still_owned(self, rename_mock, search_mock, create_mock) -> None:
        search_mock.return_value = [{"id": 99, "email": "jsmith@company.com"}]
        row = {
            "entra_id": "new-id", "name": "New Person", "email": "jsmith@company.com",
            "employee_id": "999999", "job_title": "Tech", "zendesk_org_id": 42,
            "old_zendesk_id": 50, "rename_old_email_to": "jsmith123456@company.com",
        }
        with self.assertRaises(OperationalApplyError):
            repair_reused_email_and_create(
                row,
                user_token="user-token",
                identity_token="identity-token",
                subdomain="example",
                field_keys={"employee_id": "employee_id", "job_title": "standard::job_title", "manager": "standard::manager"},
            )
        rename_mock.assert_called_once()
        create_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
