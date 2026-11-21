from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, date, datetime
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from batch_capture import archive_name, sha256
from fetch_page import Response
from site_capture import capture_batch
from site_catalog import SCHEMA, package_catalog, verify_checksum, write_checksum
from site_restore import locate_version, restore_html

ORIGIN = "https://example.invalid"
HORSE = "/uma_detail/100"
JOCKEY = "/jockey_history/2"
ARTICLE = "/articles/guide.html"
CHART = "/race_graphSp/22"
TAG = "site-batch-1-1"
HORSE_HTML = (
    "<html><head><title>サンプル競走馬</title></head><body>出走履歴 "
    '<a href="/race_graphSp/22">図</a></body></html>'
).encode()
JOCKEY_HTML = "<html><title>騎手の成績</title><body>出走予定・履歴</body></html>".encode()
ARTICLE_HTML = "<html><title>記事</title><body>競馬の記事本文</body></html>".encode()


def response(body, status=200, content_type="text/html;charset=utf-8", retry_after=None):
    headers = Message()
    headers["Content-Type"] = content_type
    if retry_after:
        headers["Retry-After"] = retry_after
    return Response(status, headers, body)


class SiteCaptureTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.seed = self.root / "seed"
        self.seed.mkdir()
        with closing(sqlite3.connect(self.seed / "catalog.sqlite3")) as db, db:
            db.executescript(SCHEMA)
            db.executemany(
                "INSERT INTO meta VALUES (?, ?)",
                [("version", "1"), ("site_hash", sha256(ORIGIN.encode()))],
            )
            db.executemany(
                "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
                [
                    (HORSE, "horse", "ready", "", None),
                    (JOCKEY, "jockey", "ready", "", None),
                    (ARTICLE, "article", "ready", "", None),
                    (HORSE + "?variant=1", "horse", "review", "query", None),
                ],
            )
        package_catalog(self.seed, {})
        self.responses = {
            "/robots.txt": response(b"User-agent: *\nDisallow: /edit\n", content_type="text/plain"),
            HORSE: response(HORSE_HTML),
            JOCKEY: response(JOCKEY_HTML),
            ARTICLE: response(ARTICLE_HTML),
        }
        self.calls = []
        self.enterContext(patch("batch_capture.fetch_once", side_effect=self.fetch))
        self.sleep = self.enterContext(patch("batch_capture.time.sleep"))

    def fetch(self, url):
        self.assertTrue(url.startswith(ORIGIN + "/"))
        path = url.removeprefix(ORIGIN)
        self.calls.append(path)
        return self.responses[path]

    def test_next_runner_deduplicates_and_archives_only_new_html(self):
        first = self.root / "first"
        summary, status = capture_batch(ORIGIN, self.seed / "catalog.zip", first, TAG, "horse", 10)
        self.assertEqual(status, "success")
        self.assertEqual(self.calls, ["/robots.txt", HORSE])
        self.assertEqual(summary["last_batch_saved"], 1)
        verify_checksum(first / "catalog.zip")
        verify_checksum(first / "pages.zip")
        with ZipFile(first / "pages.zip") as bundle:
            self.assertEqual(set(bundle.namelist()), {"batch.json", archive_name(HORSE)})
            self.assertEqual(bundle.read(archive_name(HORSE)), HORSE_HTML)

        self.calls.clear()
        resumed = self.root / "resumed"
        self.responses[CHART] = response(b"<html><title>Graph</title><body>Plot</body></html>")
        summary, status = capture_batch(
            ORIGIN, first / "catalog.zip", resumed, "site-batch-2-1", "all", 10
        )
        self.assertEqual(status, "success")
        self.assertCountEqual(self.calls, ["/robots.txt", ARTICLE, JOCKEY, CHART])
        self.assertNotIn(HORSE, self.calls)
        with ZipFile(resumed / "pages.zip") as bundle:
            manifest = json.loads(bundle.read("batch.json"))
            self.assertEqual(set(manifest["pages"]), {ARTICLE, JOCKEY, CHART})
            self.assertNotIn(archive_name(HORSE), bundle.namelist())
        with closing(sqlite3.connect(resumed / "catalog.sqlite3")) as db:
            tags = dict(db.execute("SELECT path, archive_tag FROM versions"))
            self.assertEqual(tags[HORSE], TAG)
            self.assertEqual(len(tags), 4)
        self.assertEqual(summary["by_state"], {"saved": 4, "review": 1})
        self.assertTrue(all(call.args == (10,) for call in self.sleep.call_args_list))

    def test_race_year_filters_backfill_and_refresh(self):
        clock = self.enterContext(patch("site_capture.datetime", wraps=datetime))
        clock.now.return_value = datetime(2020, 12, 31, 15, 1, tzinfo=UTC)
        prior_race = "/race_result/1201901020120191231"
        race = "/race_result/1202001020120201231"
        result = "/race_kekka/1202001020120201231"
        held_race = "/race_result/1202001020120201024"
        next_race = "/race_result/1202101010120210101"
        skip_day = date(2020, 10, 24)
        race_html = (
            "<html><title>出走馬</title><body><h2>出走馬</h2>"
            "<table><tr><th>馬名</th><th>オッズ</th></tr>"
            "<tr><td>試験馬</td><td>3.1</td></tr></table>"
            "</body></html>"
        ).encode()
        result_html = (
            "<html><title>着順・結果</title><body><h1>着順・結果</h1>"
            "<table><tr><th>着順</th><th>馬名</th></tr>"
            "<tr><td>1</td><td>試験馬</td></tr></table>"
            "</body></html>"
        ).encode()
        with closing(sqlite3.connect(self.seed / "catalog.sqlite3")) as db, db:
            db.executemany(
                "INSERT INTO pages VALUES (?, ?, 'ready', '', ?)",
                [
                    (prior_race, "race", "2019-12-31"),
                    (race, "race", "2020-12-31"),
                    (result, "result", "2020-12-31"),
                    (held_race, "race", "2020-10-24"),
                    (next_race, "race", "2021-01-01"),
                ],
            )
        (self.seed / "catalog.zip").unlink()
        package_catalog(self.seed, {})
        self.responses.update(
            {
                prior_race: response(race_html),
                race: response(race_html),
                result: response(result_html),
            }
        )
        first = self.root / "indexed-prior-year"
        _, status = capture_batch(
            ORIGIN, self.seed / "catalog.zip", first, TAG, "all", 10, race_year=2019
        )
        self.assertEqual(status, "success")
        self.assertEqual(self.calls, ["/robots.txt", prior_race])

        self.calls.clear()
        dated = self.root / "dated"
        summary, status = capture_batch(
            ORIGIN,
            first / "catalog.zip",
            dated,
            "site-batch-2-1",
            "all",
            1,
            race_year=2020,
            skip_race_day=skip_day,
        )
        self.assertEqual(status, "success")
        self.assertEqual(self.calls, ["/robots.txt", result])
        self.assertEqual(summary["last_batch"]["race_year"], 2020)
        self.assertTrue(summary["last_batch"]["has_more"])
        with ZipFile(dated / "pages.zip") as bundle:
            self.assertEqual(bundle.read(archive_name(result)), result_html)

        self.calls.clear()
        resumed = self.root / "resumed-dated"
        summary, status = capture_batch(
            ORIGIN,
            dated / "catalog.zip",
            resumed,
            "site-batch-3-1",
            "all",
            10,
            race_year=2020,
            skip_race_day=skip_day,
        )
        self.assertEqual(status, "success")
        self.assertEqual(self.calls, ["/robots.txt", race])
        self.assertFalse(summary["last_batch"]["has_more"])
        with ZipFile(resumed / "pages.zip") as bundle:
            self.assertEqual(bundle.read(archive_name(race)), race_html)

        self.calls.clear()
        summary, status = capture_batch(
            ORIGIN,
            resumed / "catalog.zip",
            self.root / "rechecked",
            "site-batch-4-1",
            "all",
            10,
            race_year=2020,
            skip_race_day=skip_day,
            refresh_hours=0,
        )
        self.assertEqual(status, "success")
        self.assertCountEqual(self.calls, ["/robots.txt", race, result])
        self.assertEqual(summary["last_batch"]["unchanged"], 2)

        self.calls.clear()
        _, status = capture_batch(
            ORIGIN,
            dated / "catalog.zip",
            self.root / "today",
            "site-batch-5-1",
            "all",
            10,
            race_year=2021,
        )
        self.assertEqual(status, "success")
        self.assertEqual(self.calls, [])

        self.responses[held_race] = response(b"denied", 403)
        summary, status = capture_batch(
            ORIGIN,
            resumed / "catalog.zip",
            self.root / "denied-year",
            "site-batch-6-1",
            "all",
            10,
            race_year=2020,
        )
        self.assertEqual(status, "forbidden")
        self.assertEqual(self.calls, ["/robots.txt", held_race])
        self.assertFalse(summary["last_batch"]["has_more"])

    def test_429_retains_valid_pages_and_persists_retry_guard(self):
        self.responses[JOCKEY] = response(b"limited", 429, retry_after="3600")
        first = self.root / "limited"
        summary, status = capture_batch(ORIGIN, self.seed / "catalog.zip", first, TAG, "all", 10)
        self.assertEqual(status, "rate_limited")
        self.assertEqual(self.calls, ["/robots.txt", ARTICLE, JOCKEY])
        self.assertEqual(summary["last_batch_saved"], 1)
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "site requested a pause"):
            capture_batch(
                ORIGIN, first / "catalog.zip", self.root / "resumed", "site-batch-2-1", "all", 10
            )
        self.assertEqual(self.calls, [])

    def test_forbidden_stops_following_requests(self):
        self.responses[JOCKEY] = response(b"denied", 403)
        first = self.root / "forbidden"
        _, status = capture_batch(ORIGIN, self.seed / "catalog.zip", first, TAG, "all", 10)
        self.assertEqual(status, "forbidden")
        self.assertEqual(self.calls, ["/robots.txt", ARTICLE, JOCKEY])
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, "HTTP 403 stop"):
            capture_batch(
                ORIGIN, first / "catalog.zip", self.root / "resumed", "site-batch-2-1", "all", 10
            )
        self.assertEqual(self.calls, [])

    def test_challenge_and_gone_response_are_kept_out_of_html_archive(self):
        self.responses[ARTICLE] = response(b"gone", 410)
        self.responses[JOCKEY] = response(
            b"<html><title>Verify you are human</title><body>Challenge</body></html>"
        )
        output = self.root / "challenge"
        summary, status = capture_batch(ORIGIN, self.seed / "catalog.zip", output, TAG, "all", 10)
        self.assertEqual(status, "failed")
        self.assertEqual(summary["last_batch_saved"], 0)
        self.assertEqual(summary["by_state"], {"pending": 2, "missing": 1, "review": 1})
        with ZipFile(output / "pages.zip") as bundle:
            self.assertEqual(bundle.namelist(), ["batch.json"])
            self.assertEqual(
                json.loads(bundle.read("batch.json"))["missing"], {ARTICLE: "HTTP 410"}
            )

    def test_newly_disallowed_target_is_not_requested(self):
        self.responses["/robots.txt"] = response(
            b"User-agent: *\nDisallow: /uma_detail/\n", content_type="text/plain"
        )
        summary, status = capture_batch(
            ORIGIN, self.seed / "catalog.zip", self.root / "disallowed", TAG, "horse", 10
        )
        self.assertEqual(status, "success")
        self.assertEqual(self.calls, ["/robots.txt"])
        self.assertEqual(summary["by_state"], {"pending": 2, "excluded": 1, "review": 1})

    def test_refresh_keeps_history_remembers_reversion_and_restores_both_versions(self):
        releases = self.root / "releases"
        releases.mkdir()
        previous = self.seed / "catalog.zip"
        changed = HORSE_HTML.replace("出走履歴".encode(), "新しい出走履歴".encode())
        for number, reply in enumerate(
            (
                response(HORSE_HTML),
                response(HORSE_HTML),
                response(changed),
                response(b"gone", 410),
                response(HORSE_HTML),
            ),
            1,
        ):
            self.responses[HORSE] = reply
            tag = f"site-batch-{number}-1"
            output = releases / tag
            summary, status = capture_batch(
                ORIGIN,
                previous,
                output,
                tag,
                "horse",
                1,
                refresh_hours=0 if number > 1 else None,
            )
            previous = output / "catalog.zip"
            self.assertEqual(status, "success")
            self.assertEqual(summary["last_batch_saved"], int(number in (1, 3)))
            if number == 2:
                self.assertEqual(summary["last_batch"]["unchanged"], 1)
            if number == 4:
                self.assertEqual(summary["by_state"]["saved_but_unavailable"], 1)
                self.assertEqual(locate_version(previous, HORSE).digest, sha256(changed))
            if number == 5:
                self.assertEqual(summary["last_batch"]["reused_version"], 1)

        self.assertEqual(summary["archived_html_versions"], 2)
        with closing(sqlite3.connect(output / "catalog.sqlite3")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT status, sha256 FROM page_changes ORDER BY observed_at"
                ).fetchall(),
                [
                    ("saved", sha256(HORSE_HTML)),
                    ("saved", sha256(changed)),
                    ("missing", None),
                    ("saved", sha256(HORSE_HTML)),
                ],
            )
        default = locate_version(previous, HORSE)
        old_change = locate_version(previous, HORSE, sha256(changed))
        self.assertEqual(default.tag, "site-batch-1-1")
        self.assertEqual(old_change.tag, "site-batch-3-1")
        for version, expected in ((default, HORSE_HTML), (old_change, changed)):
            target = self.root / (version.tag + ".html")
            restore_html(version, releases, target)
            self.assertEqual(target.read_bytes(), expected)

        self.calls.clear()
        capture_batch(
            ORIGIN,
            previous,
            releases / "site-batch-6-1",
            "site-batch-6-1",
            "horse",
            1,
            refresh_hours=24,
        )
        self.assertEqual(self.calls, [])

    def test_refresh_prioritizes_observed_sitemap_changes_and_keeps_fallback(self):
        previous = self.root / "saved"
        capture_batch(ORIGIN, self.seed / "catalog.zip", previous, TAG, "all", 10)
        with closing(sqlite3.connect(previous / "catalog.sqlite3")) as db, db:
            db.executemany(
                "UPDATE page_checks SET checked_at=? WHERE path=?",
                [
                    ("2020-01-01T00:00:00+00:00", ARTICLE),
                    ("2020-01-02T04:00:00+00:00", JOCKEY),
                    ("2020-01-03T00:30:00+00:00", HORSE),
                ],
            )
            observed = "2020-01-04T00:00:00+00:00"
            db.executemany(
                "INSERT INTO discoveries VALUES (?, ?, ?, ?)",
                [
                    (ARTICLE, "sitemap:/future.gz", "2020-02-01", observed),
                    (ARTICLE, "sitemap:/malformed.gz", "unavailable", observed),
                    (JOCKEY, "sitemap:/people.gz", "2020-01-02T12:00:00+09:00", observed),
                    (HORSE, "sitemap:/horses.gz", "2020-01-03T10:00:00+09:00", observed),
                ],
            )
        (previous / "catalog.zip").unlink()
        package_catalog(previous, {})

        for number, expected in [(2, HORSE), (3, ARTICLE), (4, JOCKEY)]:
            self.calls.clear()
            output = self.root / f"refresh-{number}"
            summary, status = capture_batch(
                ORIGIN,
                previous / "catalog.zip",
                output,
                f"site-batch-{number}-1",
                "all",
                1,
                refresh_hours=24,
            )
            self.assertEqual(status, "success")
            self.assertEqual(self.calls, ["/robots.txt", expected])
            self.assertEqual(summary["last_batch"]["unchanged"], 1)
            previous = output

    def test_restore_detects_changed_member_after_zip_checksum_is_recomputed(self):
        releases = self.root / "releases"
        releases.mkdir()
        output = releases / TAG
        capture_batch(ORIGIN, self.seed / "catalog.zip", output, TAG, "horse", 1)
        version = locate_version(output / "catalog.zip", HORSE)
        archive = output / "pages.zip"
        with ZipFile(archive, "w") as modified:
            modified.writestr(version.member, b"x" * version.size)
        write_checksum(archive)
        target = self.root / "tampered.html"
        with self.assertRaisesRegex(ValueError, "HTML SHA-256"):
            restore_html(version, releases, target)
        self.assertFalse(target.exists())
