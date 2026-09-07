from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.services.manga.telegram_worker import cleanup_temp_dir


class TempCleanupTests(unittest.TestCase):
    def test_cleanup_removes_nested_files_and_empty_directories(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            temp_dir = Path(raw_dir)
            nested = temp_dir / "chat" / "album"
            nested.mkdir(parents=True)
            first = nested / "page-1.jpg"
            second = temp_dir / "page-2.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            removed_files, removed_bytes = cleanup_temp_dir(temp_dir)

            self.assertEqual((removed_files, removed_bytes), (2, 11))
            self.assertEqual(list(temp_dir.rglob("*")), [])

    def test_cleanup_is_safe_for_missing_directory(self):
        with tempfile.TemporaryDirectory() as raw_dir:
            temp_dir = Path(raw_dir) / "missing"
            self.assertEqual(cleanup_temp_dir(temp_dir), (0, 0))


if __name__ == "__main__":
    unittest.main()
