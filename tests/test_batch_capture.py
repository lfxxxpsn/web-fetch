from __future__ import annotations

import gzip
import tempfile
import unittest
from datetime import UTC, date, datetime
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from batch_capture import archive_name, result_path, run_batch, sha256
from fetch_page import Response

ORIGIN = "https://example.invalid"
DAY = date(2026, 11, 14)
RACES = [f"/race_result/120260102{i:02}20261114" for i in (1, 2, 3)]


def response(body, status=200, content_type="text/html;charset=utf-8", retry_after=None):
    headers = Message()
    headers["Content-Type"] = content_type
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return Response(status, headers, body if isinstance(body, bytes) else body.encode())


def race_html(path, result_href=None):
    return (
        '<!doctype html><html lang="ja"><body><h2>出走馬</h2>'
        "<table><tr><th>馬名</th><th>オッズ</th></tr><tr><td>試験馬</td><td>3.1</td></tr></table>"
        f'<a href="{result_path(path) if result_href is None else result_href}">結果</a>'
        "</body></html>"
    ).encode()


RESULT = (
    "<html><h1>着順・結果</h1><table><tr><th>馬名</th></tr><tr><td>試験馬</td></tr></table></html>"
)


class BatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.events = []
        sitemap = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{ORIGIN + race}</loc></url>" for race in reversed(RACES))
            + f"<url><loc>{ORIGIN + RACES[0]}</loc></url>"
            + "<url><loc>https://other.invalid/race_result/1202601020020261114</loc></url>"
            + f"<url><loc>{ORIGIN}/race_result/1202601020020261113</loc></url>"
            + "</urlset>"
        )
        self.responses = {
            "/robots.txt": response(
                f"User-agent: *\nDisallow: /edit\nCrawl-delay: 1\nSitemap: {ORIGIN}/sitemap.gz\n",
                content_type="text/plain",
            ),
            "/sitemap.gz": response(
                f"<sitemapindex><sitemap><loc>{ORIGIN}/sitemap_race.gz</loc></sitemap></sitemapindex>"
            ),
            "/sitemap_race.gz": response(gzip.compress(sitemap.encode())),
        }
        for race in RACES:
            self.responses[race] = response(race_html(race))
            self.responses[result_path(race)] = response(RESULT)
        self.enterContext(patch("batch_capture.fetch_once", side_effect=self.fetch))
        self.enterContext(patch("batch_capture.time.sleep", side_effect=self.wait))

    def fetch(self, url):
        self.assertTrue(url.startswith(ORIGIN + "/"))
        path = url.removeprefix(ORIGIN)
        self.events.append(("get", path))
        return self.responses[path]

    def wait(self, seconds):
        self.events.append(("wait", seconds))

    def test_pause_restore_and_skip_saved_full_html(self):
        paused = run_batch(ORIGIN, DAY, self.root / "first", 1, None)
        self.assertEqual(paused.races, RACES)
        self.assertEqual(paused.state, "paused")
        self.assertEqual(list(paused.pages), RACES[:1])
        self.assertEqual(
            self.events,
            [
                event
                for path in ("/robots.txt", "/sitemap.gz", "/sitemap_race.gz", RACES[0])
                for event in (("wait", 1), ("get", path))
            ],
        )
        self.events.clear()
        complete = run_batch(ORIGIN, DAY, self.root / "resumed", 6, self.root / "first/batch.zip")
        self.assertEqual(complete.state, "complete")
        self.assertEqual(complete.requests_this_run, 6)
        self.assertNotIn(("get", RACES[0]), self.events)
        self.assertNotIn(("get", "/sitemap.gz"), self.events)
        self.assertEqual(self.events[3], ("get", result_path(RACES[0])))
        with ZipFile(self.root / "resumed/batch.zip") as bundle:
            self.assertEqual(
                set(bundle.namelist()),
                {"checkpoint.json", *(archive_name(path) for path in complete.pages)},
            )
            for race in RACES:
                self.assertEqual(bundle.read(archive_name(race)), race_html(race))
                self.assertEqual(bundle.read(archive_name(result_path(race))), RESULT.encode())

    def test_missing_and_cross_site_result_links_are_not_requested(self):
        for href in ("/unrelated", "https://other.invalid" + result_path(RACES[0])):
            with self.subTest(href=href):
                self.events.clear()
                self.responses[RACES[0]] = response(race_html(RACES[0], href))
                state = run_batch(
                    ORIGIN, DAY, Path(tempfile.mkdtemp(dir=self.root)) / "run", 6, None
                )
                self.assertEqual(state.state, "failed")
                self.assertEqual(state.requests_this_run, 4)
                self.assertEqual(list(state.pages), RACES[:1])

    def test_forbidden_stops_batch_and_same_checkpoint(self):
        self.responses[result_path(RACES[0])] = response("Denied", 403)
        state = run_batch(ORIGIN, DAY, self.root / "first", 6, None)
        self.assertEqual(state.state, "forbidden")
        self.assertEqual(list(state.pages), RACES[:1])
        self.assertNotIn(("get", RACES[1]), self.events)
        self.events.clear()
        with self.assertRaisesRegex(ValueError, "blocked"):
            run_batch(ORIGIN, DAY, self.root / "blocked", 6, self.root / "first/batch.zip")
        self.assertEqual(self.events, [])

    def test_retry_after_prevents_requests_from_an_early_resume(self):
        self.responses[result_path(RACES[0])] = response("Limited", 429, retry_after="3600")
        state = run_batch(ORIGIN, DAY, self.root / "first", 6, None)
        self.assertEqual(state.state, "rate_limited")
        self.assertGreater(datetime.fromisoformat(state.retry_at), datetime.now(UTC))
        self.events.clear()
        with self.assertRaisesRegex(ValueError, "blocked"):
            run_batch(ORIGIN, DAY, self.root / "blocked", 6, self.root / "first/batch.zip")
        self.assertEqual(self.events, [])

    def test_does_not_publish_challenge_as_result(self):
        self.responses[result_path(RACES[0])] = response("<title>Verify you are human</title>")
        state = run_batch(ORIGIN, DAY, self.root / "failed", 6, None)
        self.assertEqual(state.state, "failed")
        self.assertEqual(list(state.pages), RACES[:1])

    def test_tampered_saved_html_detected_before_any_network_request(self):
        run_batch(ORIGIN, DAY, self.root / "first", 1, None)
        previous = self.root / "first/batch.zip"
        with ZipFile(previous) as bundle:
            manifest = bundle.read("checkpoint.json")
        altered = self.root / "batch.zip"
        with ZipFile(altered, "w") as bundle:
            bundle.writestr("checkpoint.json", manifest)
            bundle.writestr(archive_name(RACES[0]), b"X" * len(race_html(RACES[0])))
        altered.with_suffix(".zip.sha256").write_text(
            f"{sha256(altered.read_bytes())}  batch.zip\n"
        )
        self.events.clear()
        with self.assertRaisesRegex(ValueError, "page failed SHA-256"):
            run_batch(ORIGIN, DAY, self.root / "resumed", 6, altered)
        self.assertEqual(self.events, [])
