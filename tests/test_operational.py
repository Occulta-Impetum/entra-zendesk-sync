"""Tests for incremental operational change detection and email-reuse safety."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from lib.cache import diff_entra_users
from lib.operational import _collision_email, build_incremental_plan


class EntraDiffTests(unittest.TestCase):
    def test_detects_new_changed_and_removed(self) -> None:
        previous = {
            "current": {
                "old": {"name": "Old", "email": "old@example.com"},
                "same": {"name": "Same", "email": "same@example.com"},
                "changed": {"name": "Before", "email": "c@example.com"},
            },
            "history": {},
        }
        current = {
            "same": {"name": "Same", "email": "same@example.com"},
            "changed": {"name": "After", "email": "c@example.com"},
            "new": {"name": "New", "email": "new@example.com"},
        }
        new_ids, changed_ids, removed_ids = diff_entra_users(current, previous)
        self.assertEqual(new_ids, {"new"})
        self.assertEqual(changed_ids, {"changed"})
        self.assertEqual(removed_ids, {"old"})


class CollisionEmailTests(unittest.TestCase):
    def test_appends_employee_id_before_domain(self) -> None:
        self.assertEqual(_collision_email("jsmith@company.com", "123456"), "jsmith123456@company.com")

    def test_sanitizes_employee_id_for_email_local_part(self) -> None:
        self.assertEqual(_collision_email("jsmith@company.com", "12 34/56"), "jsmith123456@company.com")


class IncrementalEmailReusePlanTests(unittest.TestCase):
    @patch("lib.operational.load_entra_users_cache")
    @patch("lib.operational.get_user")
    @patch("lib.operational.find_users_by_email")
    @patch("lib.operational.find_users_by_external_id")
    @patch("lib.operational.get_access_token")
    @patch("lib.operational.load_zendesk_config")
    @patch("lib.operational.collect_current_entra_state")
    def test_reused_email_requires_retired_managed_identity_and_history(
        self,
        collect_mock,
        zendesk_cfg_mock,
        token_mock,
        external_mock,
        email_mock,
        get_user_mock,
        cache_mock,
    ) -> None:
        config = {
            "zendesk": {
                "user_fields": {
                    "employee_id": "employee_id",
                    "job_title": "standard::job_title",
                    "manager": "standard::manager",
                }
            }
        }
        current = {
            "new-id": {
                "entra_id": "new-id",
                "name": "New Person",
                "email": "jsmith@company.com",
                "enabled": True,
                "employee_id": "999999",
                "job_title": "Tech",
                "manager_entra_id": "",
                "manager_name": "",
                "manager_email": "",
                "zendesk_org_id": 42,
                "zendesk_org_name": "Example",
            }
        }
        previous = {
            "version": 1,
            "current": {},
            "history": {
                "old-id": {
                    "entra_id": "old-id",
                    "name": "Old Person",
                    "email": "jsmith@company.com",
                    "employee_id": "123456",
                }
            },
        }
        collect_mock.return_value = (config, current, [])
        cache_mock.return_value = previous
        zendesk_cfg_mock.return_value = {"subdomain": "example"}
        token_mock.return_value = ("token", {"scope": "users:read"})
        external_mock.return_value = []
        email_mock.side_effect = [
            [{"id": 50, "email": "jsmith@company.com", "external_id": "entra:old-id"}],
            [],
        ]

        plan, _, _ = build_incremental_plan()
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["action"], "EMAIL REUSE + CREATE")
        self.assertEqual(plan[0]["rename_old_email_to"], "jsmith123456@company.com")
        self.assertEqual(plan[0]["old_zendesk_id"], 50)
        get_user_mock.assert_not_called()

    @patch("lib.operational.load_entra_users_cache")
    @patch("lib.operational.find_users_by_email")
    @patch("lib.operational.find_users_by_external_id")
    @patch("lib.operational.get_access_token")
    @patch("lib.operational.load_zendesk_config")
    @patch("lib.operational.collect_current_entra_state")
    def test_email_owner_without_retired_history_is_conflict(
        self,
        collect_mock,
        zendesk_cfg_mock,
        token_mock,
        external_mock,
        email_mock,
        cache_mock,
    ) -> None:
        config = {"zendesk": {"user_fields": {"employee_id": "employee_id", "job_title": "standard::job_title", "manager": "standard::manager"}}}
        current = {
            "new-id": {
                "entra_id": "new-id", "name": "New Person", "email": "jsmith@company.com",
                "enabled": True, "employee_id": "999999", "job_title": "Tech",
                "manager_entra_id": "", "manager_name": "", "manager_email": "",
                "zendesk_org_id": 42, "zendesk_org_name": "Example",
            }
        }
        collect_mock.return_value = (config, current, [])
        cache_mock.return_value = {"version": 1, "current": {}, "history": {}}
        zendesk_cfg_mock.return_value = {"subdomain": "example"}
        token_mock.return_value = ("token", {"scope": "users:read"})
        external_mock.return_value = []
        email_mock.return_value = [{"id": 50, "email": "jsmith@company.com", "external_id": "entra:old-id"}]

        plan, _, _ = build_incremental_plan()
        self.assertEqual(plan[0]["action"], "CONFLICT")


if __name__ == "__main__":
    unittest.main()
