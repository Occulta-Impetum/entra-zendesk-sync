"""Safety tests for the one-time bootstrap apply workflow."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from lib.bootstrap_apply import BootstrapApplyError, _validate_apply_plan, _write_row


class BootstrapApplyValidationTests(unittest.TestCase):
    def test_refuses_unresolved_conflicts(self) -> None:
        with self.assertRaises(BootstrapApplyError):
            _validate_apply_plan([], [{"action": "CONFLICT"}], [])

    def test_refuses_unresolved_name_reviews(self) -> None:
        with self.assertRaises(BootstrapApplyError):
            _validate_apply_plan([], [], [{"review_type": "adopt_name_mismatch"}])

    def test_refuses_update_email(self) -> None:
        plan = [{"action": "ADOPT + UPDATE EMAIL", "name": "Example", "email": "a@example.com"}]
        with self.assertRaises(BootstrapApplyError):
            _validate_apply_plan(plan, [], [])

    def test_allows_current_bootstrap_action_types(self) -> None:
        plan = [
            {"action": "CREATE", "name": "New User", "email": "new@example.com"},
            {"action": "ADOPT + UPDATE NAME + UPDATE ORGANIZATION", "name": "Existing", "email": "e@example.com"},
            {"action": "RELINK", "name": "Legacy", "email": "l@example.com"},
            {"action": "PROTECTED", "name": "Admin", "email": "admin@example.com"},
        ]
        _validate_apply_plan(plan, [], [])


class BootstrapApplyWriteTests(unittest.TestCase):
    @patch("lib.bootstrap_apply.create_user")
    def test_create_sets_entra_external_id(self, create_user_mock) -> None:
        row = {
            "action": "CREATE",
            "entra_id": "abc-123",
            "name": "New User",
            "email": "new@example.com",
            "zendesk_org_id": 42,
        }
        result = _write_row(row, access_token="token", subdomain="example")
        self.assertEqual(result, "CREATED")
        create_user_mock.assert_called_once_with(
            "token",
            "example",
            name="New User",
            email="new@example.com",
            external_id="entra:abc-123",
            organization_id=42,
        )

    @patch("lib.bootstrap_apply.update_user")
    def test_adopt_combines_external_name_and_org_update(self, update_user_mock) -> None:
        row = {
            "action": "ADOPT + UPDATE NAME + UPDATE ORGANIZATION",
            "entra_id": "abc-123",
            "name": "HR Name",
            "email": "person@example.com",
            "zendesk_id": 99,
            "zendesk_org_id": 42,
        }
        result = _write_row(row, access_token="token", subdomain="example")
        self.assertEqual(result, "UPDATED")
        update_user_mock.assert_called_once_with(
            "token",
            "example",
            99,
            fields={
                "external_id": "entra:abc-123",
                "name": "HR Name",
                "organization_id": 42,
            },
        )

    @patch("lib.bootstrap_apply.update_user")
    def test_protected_row_never_writes(self, update_user_mock) -> None:
        result = _write_row(
            {"action": "PROTECTED", "entra_id": "abc-123"},
            access_token="token",
            subdomain="example",
        )
        self.assertEqual(result, "SKIPPED")
        update_user_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
