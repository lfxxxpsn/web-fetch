from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import archive_capture as backup


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        html = "<html>已保存的網頁</html>".encode()
        self.report = {
            "success": True,
            "status": 200,
            "bytes": len(html),
            "sha256": hashlib.sha256(html).hexdigest(),
            "table_count": 1,
        }
        (self.directory / "page.html").write_bytes(html)
        (self.directory / "tables.json").write_text('[{"rows": []}]', encoding="utf-8")
        self.write_report()

    def write_report(self):
        (self.directory / "report.json").write_text(json.dumps(self.report), encoding="utf-8")

    def assert_no_archive(self):
        self.assertFalse((self.directory / "snapshot.zip").exists())
        self.assertFalse((self.directory / "snapshot.zip.sha256").exists())

    def test_restore_preserves_bytes_and_both_levels_of_checksums(self):
        (self.directory / ".env").write_text("UNRELATED_SECRET=do-not-archive", encoding="utf-8")
        archive, checksum = backup.create_archive(self.directory)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(checksum.read_bytes(), f"{digest}  snapshot.zip\n".encode())
        with ZipFile(archive) as bundle:
            self.assertIsNone(bundle.testzip())
            self.assertEqual(
                set(bundle.namelist()), {"page.html", "tables.json", "report.json", "manifest.json"}
            )
            restored = self.directory / "restored"
            bundle.extractall(restored)
        manifest = json.loads((restored / "manifest.json").read_bytes())
        self.assertEqual(manifest["format_version"], 1)
        for name in ("page.html", "tables.json", "report.json"):
            original = (self.directory / name).read_bytes()
            self.assertEqual((restored / name).read_bytes(), original)
            self.assertEqual(manifest["files"][name]["bytes"], len(original))
            self.assertEqual(
                manifest["files"][name]["sha256"], hashlib.sha256(original).hexdigest()
            )

    def test_failed_capture_cannot_be_archived_even_with_leftover_files(self):
        self.report["success"] = False
        self.write_report()
        with self.assertRaisesRegex(ValueError, "Only successful"):
            backup.create_archive(self.directory)
        self.assert_no_archive()

    def test_html_tampering_is_detected(self):
        (self.directory / "page.html").write_bytes(b"replaced content")
        with self.assertRaisesRegex(ValueError, "HTML does not match"):
            backup.create_archive(self.directory)
        self.assert_no_archive()

    def test_inconsistent_report_is_rejected(self):
        for field, value in (("status", 403), ("bytes", 0), ("sha256", "wrong")):
            with self.subTest(field=field):
                original = self.report[field]
                self.report[field] = value
                self.write_report()
                with self.assertRaises(ValueError):
                    backup.create_archive(self.directory)
                self.assert_no_archive()
                self.report[field] = original

    def test_missing_source_file_cannot_produce_partial_backup(self):
        (self.directory / "tables.json").unlink()
        with self.assertRaises(OSError):
            backup.create_archive(self.directory)
        self.assert_no_archive()

    def test_invalid_json_and_table_counts_are_rejected(self):
        for content in ("not json", "{}", "[]", "[{}, {}]"):
            with self.subTest(content=content):
                (self.directory / "tables.json").write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    backup.create_archive(self.directory)
                self.assert_no_archive()

    def test_existing_backup_is_never_overwritten(self):
        archive, checksum = backup.create_archive(self.directory)
        previous = (archive.read_bytes(), checksum.read_bytes())
        with self.assertRaises(FileExistsError):
            backup.create_archive(self.directory)
        self.assertEqual((archive.read_bytes(), checksum.read_bytes()), previous)

    def test_existing_checksum_is_preserved(self):
        checksum = self.directory / "snapshot.zip.sha256"
        checksum.write_bytes(b"previous checksum")
        with self.assertRaises(FileExistsError):
            backup.create_archive(self.directory)
        self.assertEqual(checksum.read_bytes(), b"previous checksum")
        self.assertFalse((self.directory / "snapshot.zip").exists())

    def test_size_limit_includes_the_exact_boundary_and_cleans_partial_backup(self):
        archive, checksum = backup.create_archive(self.directory)
        archive_size = archive.stat().st_size
        archive.unlink()
        checksum.unlink()
        for limit in (1, archive_size):
            with self.subTest(limit=limit), patch.object(backup, "MAX_ASSET_BYTES", limit):
                with self.assertRaisesRegex(ValueError, "smaller than 2 GiB"):
                    backup.create_archive(self.directory)
                self.assert_no_archive()

    def test_cli_failure_has_nonzero_exit_without_source_content(self):
        (self.directory / "report.json").write_text("private-query-secret", encoding="utf-8")
        with (
            patch("sys.argv", ["archive_capture.py", str(self.directory)]),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
            self.assertRaises(SystemExit) as error,
        ):
            backup.main()
        self.assertEqual(error.exception.code, 1)
        self.assertNotIn("private-query-secret", stderr.getvalue())
        self.assert_no_archive()


if __name__ == "__main__":
    unittest.main()
