from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

from batch_capture import archive_name, sha256
from site_catalog import SCHEMA, package_catalog, write_checksum
from site_restore import audit_catalog


class SiteAuditTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.releases = self.root / "releases"
        self.releases.mkdir()
        self.records = [
            ("/race_result/page1", "pilot-1-1", "batch.zip", b"<html>Race</html>"),
            ("/race_kekka/page1", "pilot-1-1", "batch.zip", b"<html>Result</html>"),
            ("/articles/page.html", "site-batch-2-1", "pages.zip", b"<html>Article</html>"),
        ]
        with closing(sqlite3.connect(self.root / "catalog.sqlite3")) as db, db:
            db.executescript(SCHEMA)
            db.executemany("INSERT INTO meta VALUES (?, ?)", [("version", "1"), ("site_hash", "x")])
            for path, tag, _, html in self.records:
                db.execute("INSERT INTO pages VALUES (?, 'fixture', 'ready', '', NULL)", (path,))
                db.execute(
                    "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?)",
                    (path, sha256(html), tag, archive_name(path), len(html), "2026-11-20"),
                )
            db.execute("INSERT INTO pages VALUES ('/uma_detail/2', 'horse', 'ready', '', NULL)")
        package_catalog(self.root, {})
        self.catalog = self.root / "catalog.zip"
        for tag, name in (("pilot-1-1", "batch.zip"), ("site-batch-2-1", "pages.zip")):
            directory = self.releases / tag
            directory.mkdir()
            with ZipFile(directory / name, "w") as archive:
                for path, record_tag, _, html in self.records:
                    if tag == record_tag:
                        archive.writestr(archive_name(path), html)
            write_checksum(directory / name)

    def test_verifies_both_release_formats_and_keeps_pending_coverage_separate(self):
        audit = audit_catalog(self.catalog, self.releases)
        self.assertEqual(audit.release_count, 2)
        self.assertEqual(audit.catalog_versions, 3)
        self.assertEqual(audit.verified_versions, 3)
        self.assertEqual(audit.unverified_versions, 0)
        self.assertEqual(audit.issues, [])
        self.assertEqual(audit.coverage["by_state"], {"saved": 3, "pending": 1})

    def test_audits_legacy_catalog_without_validation_pending_table(self):
        with closing(sqlite3.connect(self.root / "catalog.sqlite3")) as db, db:
            db.execute("DROP TABLE validation_pending")
        self.catalog.unlink()
        package_catalog(self.root, {})
        audit = audit_catalog(self.catalog, self.releases)
        self.assertEqual(audit.verified_versions, 3)
        self.assertEqual(audit.coverage["by_state"], {"saved": 3, "pending": 1})

    def test_reports_missing_archive_and_bad_member_even_if_zip_hash_matches(self):
        (self.releases / "site-batch-2-1/pages.zip").unlink()
        path, tag, name, html = self.records[0]
        archive_path = self.releases / tag / name
        with ZipFile(archive_path, "w") as archive:
            archive.writestr(archive_name(path), b"x" * len(html))
            result_path, _, _, result = self.records[1]
            archive.writestr(archive_name(result_path), result)
        write_checksum(archive_path)
        audit = audit_catalog(self.catalog, self.releases)
        self.assertEqual(audit.verified_versions, 1)
        self.assertEqual(audit.unverified_versions, 2)
        self.assertCountEqual(
            [entry["cause"] for entry in audit.issues],
            ["missing_archive_or_checksum", "invalid_html_member"],
        )
