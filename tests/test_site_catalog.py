from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from batch_capture import Blocked, archive_name, sha256
from fetch_page import Response
from site_catalog import build_catalog, verify_checksum

ORIGIN = "https://example.invalid"
RACES = [f"/race_result/120260102{i:02}20261114" for i in range(1, 4)]
RESULT = RACES[0].replace("race_result", "race_kekka")


class SiteCatalogTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.calls = []
        self.robots = f"User-agent: *\nDisallow: /edit\nSitemap: {ORIGIN}/sitemap.gz\n"
        self.locations = [ORIGIN + RACES[0], ORIGIN + "/uma_detail/1"]
        self.sitemaps = ["/sitemap_race.gz"]
        self.enterContext(patch("batch_capture.fetch_once", side_effect=self.fetch))
        self.sleep = self.enterContext(patch("batch_capture.time.sleep"))

    def fetch(self, url):
        self.calls.append(url.removeprefix(ORIGIN))
        headers = Message()
        headers["Content-Type"] = "text/plain"
        if url == ORIGIN + "/robots.txt":
            return Response(200, headers, self.robots.encode())
        if url == ORIGIN + "/sitemap.gz":
            children = "".join(
                f"<sitemap><loc>{ORIGIN}{path}</loc></sitemap>" for path in self.sitemaps
            )
            return Response(200, headers, f"<sitemapindex>{children}</sitemapindex>".encode())
        if url == ORIGIN + "/sitemap_race.gz":
            rows = "".join(f"<url><loc>{location}</loc></url>" for location in self.locations)
            return Response(200, headers, f"<urlset>{rows}</urlset>".encode())
        if url == ORIGIN + "/sitemap_z_stop.gz":
            return Response(403, headers, b"Forbidden")
        raise AssertionError("The inventory must never request HTML pages.")

    def backup(self):
        race = RACES[0]
        html = (
            f'<a href="{RESULT}">Results</a><a href="{RESULT}#top">Alias</a>'
            '<a href="/uma_sire/2?race=1">Variant</a><a href="/img/graph.png">Image</a>'
            '<a href="https://outside.invalid/leak">Outside</a><a href="/edit/page">Edit</a>'
            '<a href="/unclassified">Other</a>'
        ).encode()
        state = {
            "format_version": 1,
            "day": "2026-11-14",
            "site_hash": sha256(ORIGIN.encode()),
            "races": RACES,
            "pages": {
                race: {
                    "bytes": len(html),
                    "sha256": sha256(html),
                    "content_type": "text/html;charset=utf-8",
                }
            },
            "state": "paused",
        }
        archive = self.root / "batch.zip"
        with ZipFile(archive, "w") as bundle:
            bundle.writestr("checkpoint.json", json.dumps(state))
            bundle.writestr(archive_name(race), html)
        archive.with_suffix(".zip.sha256").write_text(f"{sha256(archive.read_bytes())}  batch.zip")
        return archive

    def test_archive_import_deduplicates_and_accounts_for_sources(self):
        self.locations += [ORIGIN + RACES[0] + "#top", "https://other.invalid/outside"]
        summary = build_catalog(
            ORIGIN,
            self.root / "inventory",
            previous=None,
            race_backups={"pilot-1-1": self.backup()},
        )
        self.assertEqual(
            summary["by_state"], {"saved": 1, "pending": 2, "excluded": 2, "review": 2}
        )
        self.assertEqual(summary["known_urls"], 7)
        self.assertEqual(summary["sitemap_urls_ever_seen"], 2)
        self.assertEqual(self.calls, ["/robots.txt", "/sitemap.gz", "/sitemap_race.gz"])
        self.assertEqual([args.args[0] for args in self.sleep.call_args_list], [5, 5, 5])
        with closing(sqlite3.connect(self.root / "inventory/catalog.sqlite3")) as db:
            self.assertEqual(
                db.execute("SELECT path, archive_tag, member FROM versions").fetchall(),
                [(RACES[0], "pilot-1-1", archive_name(RACES[0]))],
            )
            self.assertEqual(
                db.execute("SELECT race_day FROM pages WHERE path=?", (RESULT,)).fetchone(),
                ("2026-11-14",),
            )

    def test_inventory_queues_observed_local_races_and_specific_analysis_queries(self):
        race_id = RACES[0].rsplit("/", 1)[1]
        local_race = "/nar_result/z362026071605"
        analytics = f"/race_analytics/653?race_id={race_id}"
        variation = f"/race_aredo/3?race_id={race_id}"
        blocked_variation = f"/race_aredo/4?race_id={race_id}"
        extra_parameter = analytics + "&page=2"
        invalid_race_id = "/race_analytics/653?race_id=unknown"
        unclassified_filter = "/uma_sire/2?rid=" + race_id
        self.robots += "Disallow: /race_aredo/4\n"
        self.locations += [
            ORIGIN + path.replace("&", "&amp;")
            for path in (
                local_race,
                analytics,
                variation,
                blocked_variation,
                extra_parameter,
                invalid_race_id,
                unclassified_filter,
            )
        ]
        build_catalog(ORIGIN, self.root / "inventory", previous=None, race_backups={})
        with closing(sqlite3.connect(self.root / "inventory/catalog.sqlite3")) as db:
            rows = {
                path: (category, decision)
                for path, category, decision in db.execute(
                    "SELECT path, category, decision FROM pages"
                )
            }
        self.assertEqual(rows[local_race], ("local_race", "ready"))
        self.assertEqual(rows[analytics], ("analysis", "ready"))
        self.assertEqual(rows[variation], ("analysis", "ready"))
        self.assertEqual(rows[blocked_variation], ("analysis", "excluded"))
        self.assertEqual(rows[extra_parameter], ("analysis", "review"))
        self.assertEqual(rows[invalid_race_id], ("analysis", "review"))
        self.assertEqual(rows[unclassified_filter], ("pedigree", "review"))
        self.assertEqual(self.calls, ["/robots.txt", "/sitemap.gz", "/sitemap_race.gz"])

    def test_refresh_preserves_versions_and_reevaluates_old_paths(self):
        archive = self.backup()
        build_catalog(
            ORIGIN, self.root / "first", previous=None, race_backups={"pilot-1-1": archive}
        )
        self.robots = self.robots.replace("Disallow: /edit", "Disallow: /uma_detail/")
        self.locations = [ORIGIN + "/jockey_history/1"]
        updated = build_catalog(
            ORIGIN,
            self.root / "refreshed",
            previous=self.root / "first/catalog.zip",
            race_backups={"pilot-1-1": archive},
        )
        self.assertEqual(updated["known_urls"], 8)
        with closing(sqlite3.connect(self.root / "refreshed/catalog.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM versions").fetchone(), (1,))
            self.assertEqual(
                db.execute("SELECT decision FROM pages WHERE path='/uma_detail/1'").fetchone(),
                ("excluded",),
            )

    def test_damaged_backup_never_creates_a_catalog_zip(self):
        archive = self.backup()
        archive.with_suffix(".zip.sha256").write_text("invalid")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            build_catalog(
                ORIGIN, self.root / "damaged", previous=None, race_backups={"pilot-1-1": archive}
            )
        self.assertFalse((self.root / "damaged/catalog.zip").exists())

    def test_first_inventory_rate_limit_persists_guard(self):
        headers = Message()
        headers["Retry-After"] = "3600"
        output = self.root / "limited"
        with patch(
            "batch_capture.fetch_once", return_value=Response(429, headers, b"limited")
        ) as get:
            with self.assertRaises(Blocked) as stopped:
                build_catalog(ORIGIN, output, previous=None, race_backups={})
            verify_checksum(output / "catalog.zip")
            with ZipFile(output / "catalog.zip") as bundle:
                summary = json.loads(bundle.read("inventory.json"))
            self.assertEqual(summary["state"], "blocked")
            self.assertEqual(summary["retry_at"], stopped.exception.retry_at)
            self.assertEqual(summary["blocked_status"], 429)
            self.assertEqual(summary["site_requests"], 1)
            self.assertEqual(summary["known_urls"], 0)
            with self.assertRaisesRegex(ValueError, "site requested a pause"):
                build_catalog(
                    ORIGIN, self.root / "too-soon", previous=output / "catalog.zip", race_backups={}
                )
        get.assert_called_once_with(ORIGIN + "/robots.txt")

    def test_forbidden_sitemap_preserves_previous_inventory_without_partial_updates(self):
        before = self.root / "before"
        build_catalog(ORIGIN, before, previous=None, race_backups={"pilot-1-1": self.backup()})
        self.calls.clear()
        self.locations = [ORIGIN + "/jockey_history/new"]
        self.sitemaps.append("/sitemap_z_stop.gz")
        output = self.root / "blocked"
        with self.assertRaisesRegex(Blocked, "HTTP 403"):
            build_catalog(ORIGIN, output, previous=before / "catalog.zip", race_backups={})
        self.assertEqual(
            self.calls, ["/robots.txt", "/sitemap.gz", "/sitemap_race.gz", "/sitemap_z_stop.gz"]
        )
        verify_checksum(output / "catalog.zip")
        with ZipFile(output / "catalog.zip") as bundle:
            summary = json.loads(bundle.read("inventory.json"))
        self.assertEqual(summary["blocked_status"], 403)
        self.assertEqual(summary["sitemaps_read"], 1)
        self.assertEqual(summary["sitemap_rows_this_run"], 0)
        with (
            closing(sqlite3.connect(before / "catalog.sqlite3")) as old,
            closing(sqlite3.connect(output / "catalog.sqlite3")) as new,
        ):
            for table in ("pages", "discoveries", "versions"):
                self.assertEqual(
                    old.execute(f"SELECT * FROM {table}").fetchall(),
                    new.execute(f"SELECT * FROM {table}").fetchall(),
                )
            self.assertEqual(
                new.execute("SELECT status, retry_at FROM site_hold").fetchone(), (403, None)
            )
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "HTTP 403 stop"):
            build_catalog(
                ORIGIN,
                self.root / "forbidden-resume",
                previous=output / "catalog.zip",
                race_backups={},
            )
        self.assertEqual(self.calls, [])
