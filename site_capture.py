"""Archive an ordered batch of public HTML from a verified site catalog."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from batch_capture import (
    Blocked,
    Checkpoint,
    Site,
    Unavailable,
    archive_name,
    official_result,
    sha256,
)
from fetch_page import Response, TablesParser, extract_tables, load_target_url
from site_catalog import (
    CATEGORIES,
    SCHEMA,
    address,
    check_hold,
    classify,
    observe_links,
    package_catalog,
    record_hold,
    restore_catalog,
    summarize,
    write_checksum,
)

BATCH_TAG = re.compile(r"site-batch-[0-9]+-[0-9]+")
MAX_BATCH_PAGES = 100
PAGE_CATEGORIES = sorted({"all", "entry", *CATEGORIES.values()})
ERROR_HEADING = re.compile(
    r"access denied|forbidden|just a moment|attention required|"
    r"verify you are human|service unavailable|page not found",
    re.IGNORECASE,
)
REQUIRED_MARKERS = {
    "horse": ("競走馬", "出走履歴", "の情報"),
    "jockey": ("騎手の成績", "出走予定・履歴", "騎手成績"),
}


class Document(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.heading: str | None = None
        self.headings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.add(tag)
        if tag in {"h1", "title"}:
            self.heading = tag

    def handle_endtag(self, tag: str) -> None:
        if tag == self.heading:
            self.heading = None

    def handle_data(self, data: str) -> None:
        if self.heading:
            self.headings.append(data)


def validated_html(response: Response, category: str) -> str:
    if response.status != 200 or response.headers.get_content_type() not in {
        "text/html",
        "application/xhtml+xml",
    }:
        raise ValueError("Expected a successful HTML response.")
    html = response.body.decode(response.headers.get_content_charset() or "utf-8")
    document = Document()
    document.feed(html)
    headings = " ".join(document.headings).strip()
    if not {"html", "body", "title"} <= document.tags or not headings:
        raise ValueError("The response does not contain a full HTML document.")
    if ERROR_HEADING.search(headings):
        raise ValueError("The page title or main heading indicates an access or server error.")
    if category == "race":
        extract_tables(response)
    elif category == "result":
        official_result(response)
    elif category in REQUIRED_MARKERS:
        parser = TablesParser()
        parser.feed(html)
        visible = re.sub(r"\s+", "", "".join(parser.visible))
        if not any(marker in visible for marker in REQUIRED_MARKERS[category]):
            raise ValueError("Expected page markers were absent.")
    return html


def next_pages(
    db: sqlite3.Connection, category: str, limit: int, refresh_before: str | None = None
) -> list[tuple[str, str]]:
    if refresh_before is not None:
        return db.execute(
            """WITH candidates AS (
                   SELECT p.path, p.category, COALESCE(c.checked_at,
                       (SELECT MAX(indexed_at) FROM versions WHERE versions.path=p.path),
                       '') AS checked_at
                   FROM pages p LEFT JOIN page_checks c ON c.path=p.path
                   WHERE p.decision='ready' AND (?='all' OR p.category=?)
                   AND (EXISTS (SELECT 1 FROM versions WHERE versions.path=p.path)
                        OR EXISTS (SELECT 1 FROM missing WHERE missing.path=p.path))
               )
               SELECT path, category FROM candidates WHERE checked_at <= ?
               ORDER BY EXISTS (
                   SELECT 1 FROM discoveries d
                   WHERE d.path=candidates.path AND d.source LIKE 'sitemap:%'
                   AND julianday(d.lastmod) > julianday(candidates.checked_at)
                   AND julianday(d.lastmod) <= julianday(d.seen_at)
               ) DESC, checked_at, path
               LIMIT ?""",
            (category, category, refresh_before, limit),
        ).fetchall()
    return db.execute(
        """SELECT path, category FROM pages
           WHERE decision='ready' AND (?='all' OR category=?)
           AND NOT EXISTS (SELECT 1 FROM versions WHERE versions.path=pages.path)
           AND NOT EXISTS (SELECT 1 FROM missing WHERE missing.path=pages.path)
           ORDER BY CASE category WHEN 'entry' THEN 0 WHEN 'result' THEN 1
                    WHEN 'race' THEN 2 ELSE 3 END, path
           LIMIT ?""",
        (category, category, limit),
    ).fetchall()


def prior_state(db: sqlite3.Connection, path: str) -> tuple[str, str | None] | None:
    current = db.execute("SELECT status, sha256 FROM page_checks WHERE path=?", (path,)).fetchone()
    if current:
        return str(current[0]), current[1]
    if db.execute("SELECT 1 FROM missing WHERE path=?", (path,)).fetchone():
        return "missing", None
    record = db.execute(
        "SELECT sha256 FROM versions WHERE path=? ORDER BY indexed_at DESC, sha256 LIMIT 1",
        (path,),
    ).fetchone()
    return ("saved", record[0]) if record else None


def record_check(
    db: sqlite3.Connection,
    path: str,
    now: str,
    tag: str,
    prior: tuple[str, str | None] | None,
    current: tuple[str, str | None],
) -> None:
    data = (path, now, *current, tag)
    db.execute(
        """INSERT INTO page_checks VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET checked_at=excluded.checked_at,
             status=excluded.status, sha256=excluded.sha256, archive_tag=excluded.archive_tag""",
        data,
    )
    if prior != current:
        db.execute("INSERT INTO page_changes VALUES (?, ?, ?, ?, ?)", data)


def capture_batch(
    origin: str,
    previous: Path,
    output: Path,
    tag: str,
    category: str,
    limit: int,
    *,
    refresh_hours: int | None = None,
) -> tuple[dict[str, object], str]:
    if not BATCH_TAG.fullmatch(tag) or category not in PAGE_CATEGORIES:
        raise ValueError("Invalid archive tag or page category.")
    if not 1 <= limit <= MAX_BATCH_PAGES:
        raise ValueError("Invalid per-run page limit.")
    if refresh_hours is not None and not 0 <= refresh_hours <= 24 * 365:
        raise ValueError("Invalid minimum age for refreshing pages.")
    output.mkdir(parents=True, exist_ok=False)
    database = output / "catalog.sqlite3"
    restore_catalog(database, previous, origin)
    started = datetime.now(UTC)
    now = started.isoformat()
    cutoff = (
        (started - timedelta(hours=refresh_hours)).isoformat()
        if refresh_hours is not None
        else None
    )
    site = Site(origin, Checkpoint("", sha256(origin.encode())))
    saved: dict[str, dict[str, str | int]] = {}
    absent: dict[str, str] = {}
    unchanged = 0
    reused = 0
    status = "success"
    last_path = None
    with (
        closing(sqlite3.connect(database)) as db,
        db,
        ZipFile(output / "pages.zip", "x", compression=ZIP_DEFLATED, compresslevel=9) as pages,
    ):
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        check_hold(db)
        pending = next_pages(db, category, limit, cutoff)
        try:
            if pending:
                site.read_robots()
            for path, _ in pending:
                last_path = path
                if address(origin, "/", path) != path:
                    raise ValueError("The catalog contains an invalid same-site path.")
                current = classify(path, site.robots)
                db.execute(
                    "UPDATE pages SET category=?, decision=?, reason=?, race_day=? WHERE path=?",
                    (*current, path),
                )
                if current[1] != "ready":
                    continue
                prior = prior_state(db, path)
                try:
                    response = site.get(path)
                except Unavailable as error:
                    absent[path] = str(error)
                    db.execute(
                        "INSERT OR REPLACE INTO missing VALUES (?, ?, ?)", (path, str(error), tag)
                    )
                    record_check(db, path, now, tag, prior, ("missing", None))
                    continue
                html = validated_html(response, current[0])
                digest = sha256(response.body)
                member = archive_name(path)
                if db.execute(
                    "SELECT 1 FROM versions WHERE path=? AND sha256=?", (path, digest)
                ).fetchone():
                    if prior == ("saved", digest):
                        unchanged += 1
                    else:
                        reused += 1
                else:
                    pages.writestr(member, response.body)
                    db.execute(
                        "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?)",
                        (path, digest, tag, member, len(response.body), now),
                    )
                    saved[path] = {
                        "sha256": digest,
                        "bytes": len(response.body),
                        "member": member,
                        "content_type": response.headers.get("Content-Type", ""),
                    }
                db.execute("DELETE FROM missing WHERE path=?", (path,))
                record_check(db, path, now, tag, prior, ("saved", digest))
                observe_links(db, site, path, html, now)
        except Blocked as error:
            status = "forbidden" if error.status == 403 else "rate_limited"
            record_hold(db, error)
        except OSError, ValueError, URLError, LookupError:
            status = "failed"
        batch: dict[str, object] = {
            "format_version": 1,
            "tag": tag,
            "captured_at_utc": now,
            "category": category,
            "mode": "backfill" if cutoff is None else "refresh",
            "state": status,
            "last_path": last_path,
            "site_requests": site.checkpoint.requests_this_run,
            "pages": saved,
            "missing": absent,
            "unchanged": unchanged,
            "reused_version": reused,
        }
        hold = db.execute("SELECT retry_at FROM site_hold WHERE id=1").fetchone()
        if hold:
            batch["retry_at"] = hold[0]
        pages.writestr("batch.json", json.dumps(batch, indent=2) + "\n")
        summary = summarize(db)
    summary["last_batch"] = {key: value for key, value in batch.items() if key != "pages"}
    summary["last_batch_saved"] = len(saved)
    write_checksum(output / "pages.zip")
    package_catalog(output, summary)
    return summary, status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--category", choices=PAGE_CATEGORIES, default="all")
    parser.add_argument("--limit", type=int, choices=range(1, MAX_BATCH_PAGES + 1), default=20)
    parser.add_argument(
        "--refresh-hours", type=int, help="Check archived or missing pages this old"
    )
    args = parser.parse_args()
    try:
        target = urlsplit(load_target_url())
        summary, status = capture_batch(
            f"{target.scheme}://{target.netloc}",
            args.previous,
            args.output,
            args.tag,
            args.category,
            args.limit,
            refresh_hours=args.refresh_hours,
        )
    except (ValueError, TypeError, OSError, KeyError, BadZipFile, sqlite3.Error) as error:
        message = (
            str(error)
            if str(error).startswith(("The latest catalog", "The site requested"))
            else ("Could not restore or initialize the site backup batch.")
        )
        parser.exit(1, message + "\n")
    # Keep site-relative URLs and archive paths in the private assets instead of job logs.
    print(f"{status}; {summary['last_batch_saved']} new HTML pages.")
    return 0 if status == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
