"""Capture same-day races and linked results in bounded, resumable batches."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from fetch_page import (
    MAX_BYTES,
    MIN_DELAY,
    USER_AGENT,
    Response,
    TablesParser,
    extract_tables,
    fetch_once,
    load_target_url,
)

RACE_COUNT = 3
DAY_BATCH_RACES = 10
MAX_DAY_RACES = 100  # Keep a full checkpoint below the Release attachment limit.
SITEMAP_MAX_BYTES = 50 * 1024**2
RACE_PATH = re.compile(r"/race_result/[0-9]{9,32}")
ABSENT_REASONS = {"HTTP 404", "HTTP 410", "No result link", "Race page unavailable"}


@dataclass
class SavedPage:
    sha256: str
    bytes: int
    content_type: str


@dataclass
class Checkpoint:
    day: str
    site_hash: str
    races: list[str] = field(default_factory=list)
    pages: dict[str, SavedPage] = field(default_factory=dict)
    state: str = "pending"
    error: str = ""
    retry_at: str | None = None
    requests_this_run: int = 0
    full_day: bool = False
    missing: dict[str, str] = field(default_factory=dict)

    def addresses(self) -> set[str]:
        return {*self.races, *(result_path(path) for path in self.races)}

    def pending(self) -> int:
        return len(self.addresses()) - len(self.pages) - len(self.missing)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def result_path(race: str) -> str:
    return "/race_kekka/" + race.rsplit("/", 1)[1]


def archive_name(path: str) -> str:
    return f"pages/{sha256(path.encode())}.html"


def same_site_path(origin: str, url: str) -> str | None:
    parts = urlsplit(url)
    if f"{parts.scheme}://{parts.netloc}" != origin or parts.query or parts.fragment:
        return None
    return parts.path


class Blocked(ValueError):
    def __init__(
        self, status: int, retry_after: str | None, *, response: Response | None = None
    ) -> None:
        super().__init__(f"HTTP {status}; stopped the batch without retry.")
        self.status = status
        self.response = response
        self.retry_at = None
        if status == 429:
            now = datetime.now(UTC)
            try:
                if retry_after and retry_after.isascii() and retry_after.isdigit():
                    until = now + timedelta(seconds=int(retry_after))
                else:
                    until = parsedate_to_datetime(retry_after or "").astimezone(UTC)
            except ValueError, TypeError, OverflowError:
                until = now + timedelta(hours=1)
            self.retry_at = max(until, now).isoformat()


class Unavailable(ValueError):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")


class UnexpectedResponse(ValueError):
    def __init__(self, response: Response) -> None:
        super().__init__(f"HTTP {response.status}; stopped without retry or redirect.")
        self.response = response


class Site:
    def __init__(self, origin: str, checkpoint: Checkpoint) -> None:
        self.origin = origin
        self.checkpoint = checkpoint
        self.delay = MIN_DELAY
        self.robots = RobotFileParser()
        self.robots.set_url(origin + "/robots.txt")

    def get(self, path: str) -> Response:
        if path.startswith("/edit") or (
            path != "/robots.txt" and not self.robots.can_fetch(USER_AGENT, self.origin + path)
        ):
            raise ValueError("Robots rules disallow a requested path.")
        time.sleep(self.delay)
        self.checkpoint.requests_this_run += 1
        response = fetch_once(self.origin + path)
        if response.status in (403, 429):
            raise Blocked(response.status, response.headers.get("Retry-After"), response=response)
        if response.status in (404, 410):
            raise Unavailable(response.status)
        if response.status != 200:
            raise UnexpectedResponse(response)
        return response

    def read_robots(self) -> None:
        body = self.get("/robots.txt")
        if body.headers.get_content_type() != "text/plain":
            raise ValueError("Expected a plain-text robots.txt response.")
        self.robots.parse(body.body.decode("utf-8-sig").splitlines())
        delay = self.robots.crawl_delay(USER_AGENT)
        if isinstance(delay, int):
            self.delay = max(MIN_DELAY, delay)

    def sitemap_document(self, path: str) -> ElementTree.Element:
        raw = self.get(path).body
        if raw.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(SITEMAP_MAX_BYTES + 1)
        if len(raw) > SITEMAP_MAX_BYTES or "<!DOCTYPE" in raw.decode("utf-8").upper():
            raise ValueError("Unexpected sitemap size or XML structure.")
        return ElementTree.fromstring(raw)

    def sitemap(self, path: str, root: str) -> list[str]:
        element = self.sitemap_document(path)
        if element.tag.split("}")[-1] != root:
            raise ValueError(f"Expected sitemap element {root}.")
        return [loc.text.strip() for loc in element.findall("./*/{*}loc") if loc.text]

    def select_races(self, day: date, *, full_day: bool = False) -> list[str]:
        index = next(
            (
                path
                for url in self.robots.site_maps() or []
                if (path := same_site_path(self.origin, url)) == "/sitemap.gz"
            ),
            None,
        )
        if index is None:
            raise ValueError("The expected same-site sitemap index is absent from robots.txt.")
        child = "/sitemap_race.gz"
        if child not in (
            same_site_path(self.origin, url) for url in self.sitemap(index, "sitemapindex")
        ):
            raise ValueError("The sitemap index does not list the race sitemap.")
        paths = {
            path
            for url in self.sitemap(child, "urlset")
            if (path := same_site_path(self.origin, url))
            and RACE_PATH.fullmatch(path)
            and path.endswith(day.strftime("%Y%m%d"))
        }
        if full_day:
            if not paths or len(paths) > MAX_DAY_RACES:
                raise ValueError("The sitemap returned an empty or oversized race day.")
            return sorted(paths)
        if len(paths) < RACE_COUNT:
            raise ValueError("The race sitemap has fewer than three races on the selected date.")
        return sorted(paths)[:RACE_COUNT]


class ResultLink(HTMLParser):
    def __init__(self, origin: str, race: str) -> None:
        super().__init__()
        self.base = origin + race
        self.origin = origin
        self.expected = result_path(race)
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and (href := dict(attrs).get("href")):
            self.found |= same_site_path(self.origin, urljoin(self.base, href)) == self.expected


def official_result(response: Response) -> None:
    if response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
        raise ValueError("The result response is not HTML.")
    parser = TablesParser()
    parser.feed(response.body.decode(response.headers.get_content_charset() or "utf-8"))
    visible = re.sub(r"\s+", "", "".join(parser.visible))
    if "着順" not in visible or not any(
        len(table.rows) >= 2
        and "馬名" in re.sub(r"\s+", "", "".join(cell.text for cell in table.rows[0]))
        for table in parser.tables
    ):
        raise ValueError("The expected official result table is absent.")


def restore(directory: Path, archive: Path, day: date, origin: str) -> Checkpoint:
    checksum = archive.with_suffix(".zip.sha256").read_text(encoding="ascii").split()
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != [digest, archive.name]:
        raise ValueError("Checkpoint ZIP failed SHA-256 validation.")
    with ZipFile(archive) as bundle:
        if bundle.getinfo("checkpoint.json").file_size > 64 * 1024:
            raise ValueError("Checkpoint manifest is too large.")
        data = json.loads(bundle.read("checkpoint.json"))
        if data.pop("format_version", None) != 1:
            raise ValueError("Unsupported checkpoint format.")
        saved_pages = data.pop("pages")
        state = Checkpoint(**data)
        if state.day != day.isoformat() or state.site_hash != sha256(origin.encode()):
            raise ValueError("Checkpoint date or site does not match the requested batch.")
        if not isinstance(state.full_day, bool) or not isinstance(state.races, list):
            raise ValueError("Checkpoint contains an invalid race selection.")
        count = len(state.races)
        if (
            count
            and not state.full_day
            and count != RACE_COUNT
            or count > MAX_DAY_RACES
            or any(not isinstance(race, str) for race in state.races)
            or len(set(state.races)) != count
            or any(
                not RACE_PATH.fullmatch(race) or not race.endswith(day.strftime("%Y%m%d"))
                for race in state.races
            )
        ):
            raise ValueError("Checkpoint contains an invalid race selection.")
        if (
            not isinstance(saved_pages, dict)
            or not isinstance(state.missing, dict)
            or not (saved_pages.keys() | state.missing.keys()) <= state.addresses()
            or saved_pages.keys() & state.missing.keys()
            or any(reason not in ABSENT_REASONS for reason in state.missing.values())
            or any(
                result_path(race) in saved_pages and race not in saved_pages for race in state.races
            )
        ):
            raise ValueError("Checkpoint contains unexpected pages.")
        for path, metadata in saved_pages.items():
            page = SavedPage(**metadata)
            member = archive_name(path)
            if not 0 < page.bytes <= MAX_BYTES or bundle.getinfo(member).file_size != page.bytes:
                raise ValueError("Checkpoint page size is invalid.")
            raw = bundle.read(member)
            if sha256(raw) != page.sha256:
                raise ValueError("Checkpoint page failed SHA-256 validation.")
            (directory / member).write_bytes(raw)
            state.pages[path] = page
    state.requests_this_run = 0
    return state


def save_page(directory: Path, state: Checkpoint, path: str, response: Response) -> None:
    (directory / archive_name(path)).write_bytes(response.body)
    state.pages[path] = SavedPage(
        sha256(response.body), len(response.body), response.headers.get("Content-Type", "")
    )


def capture_race(
    directory: Path, site: Site, state: Checkpoint, race: str, page_budget: int
) -> None:
    result = result_path(race)
    if race not in state.pages:
        try:
            response = site.get(race)
        except Unavailable as error:
            if not state.full_day:
                raise
            state.missing[race] = str(error)
            state.missing[result] = "Race page unavailable"
            return
        extract_tables(response)
        save_page(directory, state, race, response)
        if page_budget == 1:
            return

    headers = Message()
    headers["content-type"] = state.pages[race].content_type
    page = (directory / archive_name(race)).read_bytes()
    parser = ResultLink(site.origin, race)
    parser.feed(page.decode(headers.get_content_charset() or "utf-8"))
    if not parser.found:
        if not state.full_day:
            raise ValueError("The selected race has no matching official result link.")
        state.missing[result] = "No result link"
        return
    try:
        response = site.get(result)
    except Unavailable as error:
        if not state.full_day:
            raise
        state.missing[result] = str(error)
        return
    official_result(response)
    save_page(directory, state, result, response)


def capture_missing(directory: Path, site: Site, state: Checkpoint, budget: int) -> None:
    saved_before = len(state.pages)
    races_processed = 0
    for race in state.races:
        result = result_path(race)
        if result in state.pages or result in state.missing:
            continue
        remaining = 2 if state.full_day else budget - (len(state.pages) - saved_before)
        if remaining <= 0 or (state.full_day and races_processed >= DAY_BATCH_RACES):
            return
        capture_race(directory, site, state, race, remaining)
        races_processed += 1


def run_batch(
    origin: str,
    day: date,
    directory: Path,
    budget: int,
    previous: Path | None,
    *,
    full_day: bool = False,
    list_only: bool = False,
) -> Checkpoint:
    if list_only and not full_day:
        raise ValueError("List-only mode requires a full race day.")
    (directory / "pages").mkdir(parents=True, exist_ok=False)
    state = (
        restore(directory, previous, day, origin)
        if previous
        else Checkpoint(day.isoformat(), sha256(origin.encode()))
    )
    if state.state == "forbidden" or (
        state.retry_at and datetime.fromisoformat(state.retry_at) > datetime.now(UTC)
    ):
        raise ValueError("The saved checkpoint is blocked by the site's access response.")
    if state.full_day and not full_day:
        raise ValueError("A full-day checkpoint requires full-day mode.")
    state.error = ""
    state.retry_at = None
    try:
        discover = not state.races or (full_day and not state.full_day)
        if discover or (not list_only and state.pending()):
            site = Site(origin, state)
            site.read_robots()
            if discover:
                races = sorted(set(site.select_races(day, full_day=full_day)) | set(state.races))
                if len(races) > MAX_DAY_RACES:
                    raise ValueError("The combined race-day list exceeds the checkpoint limit.")
                state.races = races
                state.full_day = full_day
            if not list_only:
                capture_missing(directory, site, state, budget)
        if state.pending():
            state.state = "paused"
        else:
            state.state = "complete_with_missing" if state.missing else "complete"
    except Blocked as error:
        state.state = "forbidden" if error.status == 403 else "rate_limited"
        state.error = str(error)
        state.retry_at = error.retry_at
    except ValueError, URLError, ElementTree.ParseError, OSError, EOFError, LookupError:
        state.state = "failed"
        state.error = "Stopped after a network, page verification, sitemap or file error."
    manifest = {"format_version": 1, **asdict(state)}
    (directory / "checkpoint.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = directory / "batch.zip"
    with ZipFile(archive, "x", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
        bundle.write(directory / "checkpoint.json", "checkpoint.json")
        for path in state.pages:
            member = archive_name(path)
            bundle.write(directory / member, member)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (directory / "batch.zip.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="ascii", newline="\n"
    )
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--pause-after", type=int, choices=range(1, 7), default=6)
    parser.add_argument("--full-day", action="store_true")
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()
    try:
        target = urlsplit(load_target_url())
        state = run_batch(
            f"{target.scheme}://{target.netloc}",
            args.day,
            args.output,
            args.pause_after,
            args.resume,
            full_day=args.full_day,
            list_only=args.list_only,
        )
    except ValueError, TypeError, OSError, KeyError, BadZipFile:
        parser.exit(1, "Could not initialize or verify the batch checkpoint.\n")
    print(
        f"{state.state}: {len(state.races)} races, {len(state.pages)} HTML pages, "
        f"{len(state.missing)} missing, {state.pending()} pending; "
        f"{state.requests_this_run} site requests."
    )
    if state.error:
        print(state.error)
    if state.retry_at:
        print(f"Retry no earlier than {state.retry_at}.")
    return 0 if state.state in {"complete", "complete_with_missing", "paused"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
