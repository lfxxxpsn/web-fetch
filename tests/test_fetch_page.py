from __future__ import annotations

import gzip
import io
import json
import tempfile
import threading
import unittest
import zlib
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import fetch_page as probe

TEST_URL = "https://example.invalid/results/123?token=test-secret"

HTML = """<!doctype html><html><h2>出走馬</h2><table>
<tr><th>馬<br>名</th><th>オッズ</th></tr>
<tr><td rowspan="2">テストホース &amp; 馬</td><td>2.7</td></tr>
</table></html>"""


def response(body: str = HTML, status: int = 200, content_type: str = "text/html; charset=utf-8"):
    headers = Message()
    headers["Content-Type"] = content_type
    return probe.Response(status, headers, body.encode("utf-8"))


class ConfigurationTests(unittest.TestCase):
    def test_github_output_identifies_only_the_current_successful_capture(self):
        for success in (True, False):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "github-output"
                output.write_text("existing=value\n", encoding="utf-8")
                directory = Path("artifacts/current-capture")
                with (
                    patch.dict(probe.os.environ, {"TARGET_URL": TEST_URL}),
                    patch("sys.argv", ["fetch_page.py", "--github-output", str(output)]),
                    patch("sys.stdout", new_callable=io.StringIO),
                    patch.object(probe, "capture", return_value=(success, directory)),
                ):
                    self.assertEqual(probe.main(), 0 if success else 1)
                expected = "existing=value\n"
                if success:
                    expected += "capture_dir=artifacts/current-capture\n"
                self.assertEqual(output.read_text(encoding="utf-8"), expected)

    def test_missing_or_empty_secret_stops_before_capture(self):
        for environment in ({}, {"TARGET_URL": ""}, {"TARGET_URL": "   "}):
            with (
                self.subTest(environment=environment),
                patch.dict(probe.os.environ, environment, clear=True),
                patch("sys.argv", ["fetch_page.py"]),
                patch("sys.stderr", new_callable=io.StringIO) as stderr,
                patch.object(probe, "capture") as capture,
                self.assertRaises(SystemExit) as error,
            ):
                probe.main()
            self.assertEqual(error.exception.code, 2)
            self.assertIn("TARGET_URL", stderr.getvalue())
            capture.assert_not_called()

    def test_environment_accepts_https_url_with_query(self):
        with patch.dict(probe.os.environ, {"TARGET_URL": " " + TEST_URL + " "}):
            self.assertEqual(probe.load_target_url(), TEST_URL)

    def test_main_uses_environment_without_echoing_secret(self):
        with (
            patch.dict(probe.os.environ, {"TARGET_URL": TEST_URL}),
            patch("sys.argv", ["fetch_page.py"]),
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
            patch.object(probe, "capture", return_value=(True, Path("artifacts/test"))) as capture,
        ):
            self.assertEqual(probe.main(), 0)
        capture.assert_called_once_with(TEST_URL, Path("artifacts"), 10)
        self.assertNotIn(TEST_URL, stdout.getvalue())
        self.assertNotIn("test-secret", stdout.getvalue())

    def test_invalid_secret_does_not_appear_in_error_output(self):
        invalid = "http://private-user:private-password@example.invalid/page"
        with (
            patch.dict(probe.os.environ, {"TARGET_URL": invalid}),
            patch("sys.argv", ["fetch_page.py"]),
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
            patch.object(probe, "capture") as capture,
            self.assertRaises(SystemExit) as error,
        ):
            probe.main()
        self.assertEqual(error.exception.code, 2)
        capture.assert_not_called()
        self.assertNotIn("private-password", stderr.getvalue())
        self.assertNotIn(invalid, stderr.getvalue())


class ExtractionTests(unittest.TestCase):
    def test_extracts_japanese_entities_and_keeps_spans(self):
        tables = probe.extract_tables(response())
        self.assertEqual(tables[0].rows[1][0].text, "テストホース & 馬")
        self.assertEqual(tables[0].rows[1][0].rowspan, "2")
        self.assertEqual(tables[0].rows[0][0].tag, "th")

    def test_nested_table_does_not_lose_parent_cell_text(self):
        body = HTML.replace(
            "テストホース &amp; 馬",
            "前<table><tr><td>内側</td></tr></table>後",
        )
        tables = probe.extract_tables(response(body))
        self.assertEqual(tables[0].rows[1][0].text, "前後")
        self.assertEqual(tables[1].rows[0][0].text, "内側")

    def test_rejects_status_200_challenge(self):
        with self.assertRaisesRegex(ValueError, "race table"):
            probe.extract_tables(response("<html>Please verify you are human.</html>"))

    def test_script_contents_do_not_count_as_race_content(self):
        with self.assertRaisesRegex(ValueError, "race table"):
            probe.extract_tables(response(f"<script>{HTML}</script>"))

    def test_rejects_header_only_table(self):
        with self.assertRaisesRegex(ValueError, "race table"):
            probe.extract_tables(response("出走馬<table><tr><th>馬名 オッズ</th></tr></table>"))

    def test_rejects_non_html(self):
        with self.assertRaisesRegex(ValueError, "not HTML"):
            probe.extract_tables(response(content_type="application/json"))

    def test_rejects_oversized_body(self):
        with patch.object(probe, "MAX_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "size limit"):
                probe.extract_tables(response())

    def test_declared_japanese_encoding(self):
        headers = Message()
        headers["Content-Type"] = "text/html; charset=shift_jis"
        tables = probe.extract_tables(probe.Response(200, headers, HTML.encode("shift_jis")))
        self.assertEqual(len(tables), 1)


class CompressionTests(unittest.TestCase):
    def test_decodes_gzip_without_changing_html_bytes(self):
        raw = HTML.encode("utf-8")
        self.assertEqual(probe.decode_http_body(gzip.compress(raw), "gzip"), raw)

    def test_decodes_deflate(self):
        raw = HTML.encode("utf-8")
        self.assertEqual(probe.decode_http_body(zlib.compress(raw), "deflate"), raw)

    def test_identity_body_is_unchanged(self):
        raw = HTML.encode("utf-8")
        self.assertEqual(probe.decode_http_body(raw, ""), raw)

    def test_limits_decompressed_size_for_both_codings(self):
        for coding, compressed in (
            ("gzip", gzip.compress(b"a" * 1024)),
            ("deflate", zlib.compress(b"a" * 1024)),
        ):
            with self.subTest(coding=coding), patch.object(probe, "MAX_BYTES", 64):
                with self.assertRaisesRegex(ValueError, "Decoded response exceeded"):
                    probe.decode_http_body(compressed, coding)

    def test_truncated_or_invalid_compressed_body_is_rejected(self):
        for coding, body in (
            ("gzip", gzip.compress(b"test")[:-4]),
            ("gzip", b"not gzip"),
            ("deflate", zlib.compress(b"test")[:-2]),
            ("deflate", b"not deflate"),
        ):
            with self.subTest(coding=coding, body=body), self.assertRaises(ValueError):
                probe.decode_http_body(body, coding)

    def test_unadvertised_compression_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            probe.decode_http_body(b"data", "br")


class CaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.fetch = self.enterContext(patch.object(probe, "fetch_once"))
        self.fetch.side_effect = AssertionError("No real network is allowed in unit tests.")
        self.sleep = self.enterContext(patch.object(probe.time, "sleep"))

    def run_capture(self, result):
        self.fetch.side_effect = None
        self.fetch.return_value = result
        ok, directory = probe.capture(TEST_URL, self.output)
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        return ok, directory, report

    def test_waits_before_the_only_request_and_preserves_html(self):
        events = []
        self.sleep.side_effect = lambda seconds: events.append(("wait", seconds))
        self.fetch.side_effect = lambda url: events.append(("get", url)) or response()
        ok, directory = probe.capture(TEST_URL, self.output)
        self.assertTrue(ok)
        self.assertEqual(events, [("wait", 10), ("get", TEST_URL)])
        self.assertEqual((directory / "page.html").read_bytes(), HTML.encode("utf-8"))
        self.assertEqual({file.name for file in directory.iterdir()}, {"page.html", "report.json"})
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["success"])
        self.assertEqual(report["table_count"], 1)

    def test_http_errors_stop_without_retry_or_false_archive(self):
        for status in (301, 302, 403, 429, 500):
            with self.subTest(status=status):
                self.fetch.reset_mock()
                result = response("Blocked", status)
                result.headers["Retry-After"] = "3600"
                ok, directory, report = self.run_capture(result)
                self.assertFalse(ok)
                self.assertEqual(report["status"], status)
                self.assertEqual(report["retry_after"], "3600")
                self.assertEqual(report["request_count"], 1)
                self.fetch.assert_called_once_with(TEST_URL)
                self.assertEqual({f.name for f in directory.iterdir()}, {"report.json"})

    def test_status_200_error_is_not_saved_as_page(self):
        ok, directory, report = self.run_capture(response("<title>Access denied</title>"))
        self.assertFalse(ok)
        self.assertIn("race table", report["error"])
        self.assertFalse((directory / "page.html").exists())

    def test_network_error_writes_diagnostic_without_retry(self):
        self.fetch.side_effect = URLError("DNS unavailable")
        ok, directory = probe.capture(TEST_URL, self.output)
        self.assertFalse(ok)
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        self.assertIn("Network request failed", report["error"])
        self.fetch.assert_called_once()

    def test_invalid_charset_is_reported(self):
        ok, directory, report = self.run_capture(
            response(content_type="text/html; charset=invalid")
        )
        self.assertFalse(ok)
        self.assertIn("encoding", report["error"])
        self.assertFalse((directory / "page.html").exists())

    def test_previous_success_survives_later_failure(self):
        _, first, _ = self.run_capture(response())
        _, second, _ = self.run_capture(response("Blocked", 403))
        self.assertNotEqual(first, second)
        self.assertEqual((first / "page.html").read_bytes(), HTML.encode("utf-8"))
        self.assertFalse((second / "page.html").exists())

    def test_bad_url_is_rejected_before_network_or_sleep(self):
        for url in (
            "",
            "https:///missing-host",
            "https://example.invalid:bad/",
            "https://user:password@example.invalid/",
            TEST_URL + "#fragment",
            TEST_URL + "\n",
            TEST_URL.replace("https:", "http:"),
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                probe.capture(url, self.output)
        self.fetch.assert_not_called()
        self.sleep.assert_not_called()

    def test_cannot_reduce_delay_below_ten_seconds(self):
        with self.assertRaises(ValueError):
            probe.capture(TEST_URL, self.output, delay=9)
        self.fetch.assert_not_called()
        self.sleep.assert_not_called()

    def test_success_report_omits_target_url(self):
        ok, _, report = self.run_capture(response())
        self.assertTrue(ok)
        self.assertNotIn("url", report)
        self.assertEqual(report["target_source"], "TARGET_URL")
        self.assertNotIn(TEST_URL, json.dumps(report))

    def test_redirect_report_omits_location_and_secret(self):
        result = response("Redirect", 302)
        result.headers["Location"] = TEST_URL
        _, _, report = self.run_capture(result)
        self.assertTrue(report["redirect_received"])
        self.assertNotIn("location", report)
        self.assertNotIn(TEST_URL, json.dumps(report))
        self.assertNotIn("test-secret", json.dumps(report))

    def test_network_error_does_not_echo_url_or_query_secret(self):
        self.fetch.side_effect = URLError(f"Cannot connect to {TEST_URL}")
        ok, directory = probe.capture(TEST_URL, self.output)
        self.assertFalse(ok)
        report = (directory / "report.json").read_text(encoding="utf-8")
        self.assertNotIn(TEST_URL, report)
        self.assertNotIn("test-secret", report)
        self.assertIn("Network request failed", report)


class TransportTests(unittest.TestCase):
    def test_navigation_headers_and_gzip_work_together(self):
        paths = []
        raw = HTML.encode("utf-8")
        compressed = gzip.compress(raw)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                compatible = (
                    self.headers.get("Accept-Language", "").startswith("ja")
                    and self.headers.get("Sec-Fetch-Mode") == "navigate"
                    and "gzip" in self.headers.get("Accept-Encoding", "")
                    and "Chrome/" in self.headers.get("User-Agent", "")
                )
                self.send_response(200 if compatible else 403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(compressed)

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        thread.start()
        try:
            result = probe.fetch_once(f"http://127.0.0.1:{server.server_port}/race")
            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, raw)
            self.assertEqual(result.wire_bytes, len(compressed))
            self.assertEqual(len(probe.extract_tables(result)), 1)
            self.assertEqual(paths, ["/race"])
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_redirect_does_not_make_a_second_request(self):
        paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/destination")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        thread.start()
        try:
            result = probe.fetch_once(f"http://127.0.0.1:{server.server_port}/start")
            self.assertEqual(result.status, 302)
            self.assertEqual(paths, ["/start"])
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
