"""One-request page capture configured by TARGET_URL; runtime uses only the stdlib."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import time
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import IO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
MIN_DELAY = 5
MAX_BYTES = 8 * 1024 * 1024
RACE_VENUES = r"札幌|函館|福島|新潟|東京|中山|中京|京都|阪神|小倉"
RATING_LINEUP_HEADERS = frozenset({"馬名", "総合値", "SP値", "AG値", "SA値"})
DEBUT_LINEUP_HEADERS = frozenset(
    {"馬名", "総合値", "騎手名", "父", "母", "父の子新馬戦成績", "母の子新馬戦成績"}
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        # A redirect would be an additional, unthrottled request.
        return None


@dataclass
class Response:
    status: int
    headers: Message
    # HTML entity bytes after HTTP compression is decoded, before character decoding.
    body: bytes
    wire_bytes: int | None = None


@dataclass
class Cell:
    tag: str
    text: str = ""
    rowspan: str = "1"
    colspan: str = "1"


@dataclass
class Table:
    rows: list[list[Cell]] = field(default_factory=list)


class TablesParser(HTMLParser):
    """Keep source row/column spans instead of guessing a flattened table schema."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[Table] = []
        self.stack: list[Table] = []
        self.parent_cells: list[Cell | None] = []
        self.cell: Cell | None = None
        self.ignored: str | None = None
        self.json_ld: list[list[str]] = []
        self.in_json_ld = False
        self.visible: list[str] = []
        self.in_title = False
        self.title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored = tag
            self.in_json_ld = tag == "script" and dict(attrs).get("type") == "application/ld+json"
            if self.in_json_ld:
                self.json_ld.append([])
        if tag == "title":
            self.in_title = True
        if self.ignored:
            return
        attributes = dict(attrs)
        if tag == "table":
            table = Table()
            self.tables.append(table)
            self.stack.append(table)
            self.parent_cells.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.stack:
            self.stack[-1].rows.append([])
            self.cell = None
        elif tag in {"td", "th"} and self.stack:
            if not self.stack[-1].rows:
                self.stack[-1].rows.append([])
            self.cell = Cell(
                tag=tag,
                rowspan=attributes.get("rowspan") or "1",
                colspan=attributes.get("colspan") or "1",
            )
            self.stack[-1].rows[-1].append(self.cell)
        elif tag == "br" and self.cell is not None:
            self.cell.text += "\n"

    def handle_data(self, data: str) -> None:
        if self.in_json_ld:
            self.json_ld[-1].append(data)
        if not self.ignored:
            self.visible.append(data)
            if self.in_title:
                self.title.append(data)
            if self.cell is not None:
                self.cell.text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == self.ignored:
            self.ignored = None
            self.in_json_ld = False
        elif tag == "title":
            self.in_title = False
        elif self.ignored:
            return
        elif tag == "table" and self.stack:
            self.stack.pop()
            self.cell = self.parent_cells.pop()
        elif tag in {"td", "th", "tr"}:
            self.cell = None


def validate_url(url: str) -> None:
    message = "TARGET_URL must be an HTTPS URL without credentials, fragments or whitespace."
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and parsed.port != 0
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)
        )
    except ValueError:
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)


def load_target_url() -> str:
    url = os.environ.get("TARGET_URL", "").strip()
    if not url:
        raise ValueError("Set the TARGET_URL environment variable (GitHub Secret: TARGET_URL).")
    validate_url(url)
    return url


def decode_http_body(body: bytes, content_encoding: str) -> bytes:
    if len(body) > MAX_BYTES:
        raise ValueError("Compressed response exceeded the 8 MiB size limit.")
    encoding = content_encoding.strip().lower()
    try:
        if encoding in {"", "identity"}:
            decoded = body
        elif encoding == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                decoded = stream.read(MAX_BYTES + 1)
        elif encoding == "deflate":
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(body, MAX_BYTES + 1)
            if len(decoded) <= MAX_BYTES and not decoder.eof:
                raise ValueError("Incomplete deflate response.")
        else:
            raise ValueError("Unsupported HTTP Content-Encoding.")
    except (OSError, EOFError, zlib.error) as error:
        raise ValueError("Invalid compressed HTTP response.") from error
    if len(decoded) > MAX_BYTES:
        raise ValueError("Decoded response exceeded the 8 MiB size limit.")
    return decoded


def fetch_once(url: str) -> Response:
    request = Request(url, headers=REQUEST_HEADERS)
    opener = build_opener(NoRedirect())
    try:
        response = opener.open(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        wire_body = response.read(MAX_BYTES + 1)
        body = decode_http_body(wire_body, response.headers.get("Content-Encoding", ""))
        return Response(response.status, response.headers, body, wire_bytes=len(wire_body))


def has_named_race_heading(title: str, fragments: list[str]) -> bool:
    match = re.fullmatch(r"(.+?)\s+(\d{4})\s+予想(?:\s*\|.+)?", title.strip())
    if not match:
        return False
    race_name, year = match.groups()
    visible = [fragment.strip() for fragment in fragments if fragment.strip()]
    return any(re.fullmatch(rf"{year}/\d{{2}}/\d{{2}}", text) for text in visible) and any(
        re.fullmatch(RACE_VENUES, venue)
        and re.fullmatch(r"(?:[1-9]|1[0-2])R", race_number)
        and name == race_name
        and re.search(r"\d{1,2}:\d{2}\s*発走$", details)
        for venue, race_number, name, details in zip(
            visible, visible[1:], visible[2:], visible[3:], strict=False
        )
    )


def matches_race_breadcrumb(parser: TablesParser, expected_url: str) -> bool:
    request = urlsplit(expected_url)
    if not re.fullmatch(r"/race_(?:result|kekka)/[0-9]{9,32}", request.path):
        return False
    race_path = request.path.replace("/race_kekka/", "/race_result/", 1)
    race_url = f"{request.scheme}://{request.netloc}{race_path}"
    for block in parser.json_ld:
        try:
            data = json.loads("".join(block))
        except ValueError, RecursionError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "BreadcrumbList":
            continue
        items = data.get("itemListElement")
        if isinstance(items, list) and items and isinstance(items[-1], dict):
            item = items[-1].get("item")
            if isinstance(item, dict) and item.get("url") == race_url:
                return True
    return False


def has_header_only_table(tables: list[Table], *layouts: frozenset[str]) -> bool:
    for table in tables:
        if len(table.rows) == 1:
            headers = {"".join(cell.text.split()) for cell in table.rows[0] if cell.tag == "th"}
            if any(layout <= headers for layout in layouts):
                return True
    return False


def has_empty_ratings_lineup(tables: list[Table]) -> bool:
    return has_header_only_table(tables, frozenset({"仮番"})) and has_header_only_table(
        tables, RATING_LINEUP_HEADERS
    )


def has_bet_predictions(tables: list[Table]) -> bool:
    return any(
        {"種類", "馬券", "買い目"} <= {"".join(cell.text.split()) for cell in table.rows[0]}
        and any(
            cell.tag == "td"
            and re.fullmatch(r"[1-9][0-9]?(?:[,－-][1-9][0-9]?)+", cell.text.strip())
            for row in table.rows[1:]
            for cell in row
        )
        for table in tables
        if len(table.rows) >= 2
    )


def extract_tables(response: Response, *, expected_url: str | None = None) -> list[Table]:
    if response.status != 200:
        raise ValueError(f"HTTP {response.status}; stopped without retry or redirect.")
    if len(response.body) > MAX_BYTES:
        raise ValueError("Response exceeded the 8 MiB size limit.")
    if response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
        raise ValueError("Response is not HTML.")
    encoding = response.headers.get_content_charset() or "utf-8"
    text = response.body.decode(encoding)
    parser = TablesParser()
    parser.feed(text)
    visible = re.sub(r"\s+", "", "".join(parser.visible))
    if "出走馬" in visible:
        canceled = any(fragment.strip() == "競争中止" for fragment in parser.visible)
        race_title = " ".join(parser.title)
        has_race_title = bool(
            re.search(rf"(?:{RACE_VENUES})競馬", race_title)
            and re.search(r"第\s*\d+\s*レース", race_title)
            and re.search(r"\d{4}年\s*\d{1,2}月\s*\d{1,2}日", race_title)
        )
        for table in parser.tables:
            if len(table.rows) < 2:
                continue
            cells = re.sub(r"\s+", "", "".join(cell.text for row in table.rows for cell in row))
            if "馬名" in cells and "オッズ" in cells:
                return parser.tables
            if canceled:
                headers = [re.sub(r"\s+", "", cell.text) for cell in table.rows[0]]
                if {"馬名", "斤量", "騎手名", "調教師"}.issubset(headers):
                    name_column = headers.index("馬名")
                    if any(
                        len(row) > name_column and row[name_column].text.strip()
                        for row in table.rows[1:]
                    ):
                        return parser.tables
        identified_race = has_race_title or has_named_race_heading(race_title, parser.visible)
        if (
            canceled
            and identified_race
            and has_header_only_table(parser.tables, RATING_LINEUP_HEADERS, DEBUT_LINEUP_HEADERS)
        ):
            return parser.tables
        if (
            expected_url is not None
            and identified_race
            and matches_race_breadcrumb(parser, expected_url)
            and has_empty_ratings_lineup(parser.tables)
            and has_bet_predictions(parser.tables)
        ):
            return parser.tables
    raise ValueError("Expected race table is missing; this may be an error/challenge page.")


def capture(url: str, output_root: Path, delay: int = MIN_DELAY) -> tuple[bool, Path]:
    validate_url(url)
    if delay < MIN_DELAY:
        raise ValueError(f"Delay in seconds must be at least {MIN_DELAY}.")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    directory = output_root / run_id
    directory.mkdir(parents=True, exist_ok=False)
    report: dict[str, object] = {
        "target_source": "TARGET_URL",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "user_agent": USER_AGENT,
        "request_profile": "browser-compatible-v1",
        "initial_delay_seconds": delay,
        "request_count": 0,
        "success": False,
    }
    success = False
    try:
        # Also separates consecutive serialized workflow runs; no network occurs before this.
        time.sleep(delay)
        report["request_count"] = 1
        response = fetch_once(url)
        report.update(
            {
                "status": response.status,
                "bytes": len(response.body),
                "wire_bytes": response.wire_bytes,
                "content_encoding": response.headers.get("Content-Encoding", "identity"),
                "content_type": response.headers.get("Content-Type", ""),
                "retry_after": response.headers.get("Retry-After"),
                "redirect_received": bool(response.headers.get("Location")),
                "sha256": hashlib.sha256(response.body).hexdigest(),
            }
        )
        table_count = len(extract_tables(response, expected_url=url))
        (directory / "page.html").write_bytes(response.body)
        report["table_count"] = table_count
        report["success"] = success = True
    except URLError:
        report["error"] = "Network request failed; no retry performed."
    except OSError:
        report["error"] = "Network or file operation failed."
    except UnicodeError:
        report["error"] = "Response cannot be decoded with its declared character encoding."
    except LookupError:
        report["error"] = "Response character encoding is unsupported."
    except ValueError as error:
        report["error"] = str(error).replace(url, "[redacted]")
    finally:
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        # Secrets masking in CI logs does not apply to JSON artifacts.
        for key, value in report.items():
            if isinstance(value, str):
                report[key] = value.replace(url, "[redacted]")
        (directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return success, directory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    parser.add_argument("--delay-seconds", type=int, default=MIN_DELAY)
    parser.add_argument(
        "--github-output", type=Path, help="Append the successful capture directory."
    )
    args = parser.parse_args()
    try:
        success, directory = capture(load_target_url(), args.output, args.delay_seconds)
    except ValueError as error:
        parser.error(str(error))
    print(f"{'PASS' if success else 'FAIL'}: {directory / 'report.json'}")
    if success and args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"capture_dir={directory.as_posix()}\n")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
