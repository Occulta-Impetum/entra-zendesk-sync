from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.cache import CacheError, load_entra_users_cache, save_entra_users_cache


class AtomicEntraCacheTests(unittest.TestCase):
    def test_successful_save_replaces_target_with_valid_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "entra_users.json"
            current = {"id-1": {"entra_id": "id-1", "name": "Example"}}

            save_entra_users_cache(current, previous=None, path=path)
            loaded = load_entra_users_cache(path)

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["current"], current)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_failed_replace_leaves_existing_target_untouched_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "entra_users.json"
            original = '{"sentinel": true}\n'
            path.write_text(original, encoding="utf-8")
            current = {"id-1": {"entra_id": "id-1", "name": "Example"}}

            with patch("lib.cache.os.replace", side_effect=OSError("simulated replace failure")):
                with self.assertRaises(CacheError):
                    save_entra_users_cache(current, previous=None, path=path)

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
