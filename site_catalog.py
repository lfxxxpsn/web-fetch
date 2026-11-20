"""Build a verified inventory of public HTML and reconcile existing race backups."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, date, datetime
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from batch_capture import (
    RACE_PATH,
    Blocked,
    Checkpoint,
    Site,
    archive_name,
    restore,
    same_site_path,
    sha256,
)
from fetch_page import USER_AGENT, load_target_url

RACE_TAG = re.compile(r"pilot-[0-9]+-[0-9]+")
RACE_ANALYSIS_LINK = re.compile(r"/race_(?:analytics|aredo)/[0-9]+\?race_id=[0-9]{9,32}")
MAX_CATALOG_BYTES = 256 * 1024**2
MAX_SITEMAPS = 12
STATIC_SUFFIXES = {
    ".avif",
    ".css",
    ".csv",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".m4v",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".webp",
    ".woff",
    ".woff2",
    ".xml",
    ".zip",
}
ENTRY_ROUTES = {
    "",
    "race",
    "schedule",
    "jockey",
    "uma",
    "nar",
    "myosou",
    "ai_isis",
    "ai_sopdet",
}
CATEGORIES = {
    "race_result": "race",
    "race_kekka": "result",
    "nar_result": "local_race",
    "race_rhis": "race_history",
    "uma_detail": "horse",
    "jockey_history": "jockey",
    "uma_sire": "pedigree",
    "race_analytics": "analysis",
    "race_aredo": "analysis",
    "articles": "article",
    "race_graphSp": "chart",
    "race_graphAg": "chart",
    "race_graphSa": "chart",
    "race_graphOdds": "chart",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pages (
    path TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('ready', 'review', 'excluded')),
    reason TEXT NOT NULL,
    race_day TEXT
);
CREATE TABLE IF NOT EXISTS discoveries (
    path TEXT NOT NULL REFERENCES pages(path),
    source TEXT NOT NULL,
    lastmod TEXT,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (path, source)
);
CREATE TABLE IF NOT EXISTS versions (
    path TEXT NOT NULL REFERENCES pages(path),
    sha256 TEXT NOT NULL,
    archive_tag TEXT NOT NULL,
    member TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (path, sha256)
);
CREATE TABLE IF NOT EXISTS missing (
    path TEXT PRIMARY KEY REFERENCES pages(path),
    reason TEXT NOT NULL,
    archive_tag TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_hold (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status INTEGER NOT NULL CHECK (status IN (403, 429)),
    retry_at TEXT
);
CREATE TABLE IF NOT EXISTS page_checks (
    path TEXT PRIMARY KEY REFERENCES pages(path),
    checked_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('saved', 'missing')),
    sha256 TEXT,
    archive_tag TEXT NOT NULL,
    FOREIGN KEY (path, sha256) REFERENCES versions(path, sha256)
);
CREATE TABLE IF NOT EXISTS page_changes (
    path TEXT NOT NULL REFERENCES pages(path),
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('saved', 'missing')),
    sha256 TEXT,
    archive_tag TEXT NOT NULL,
    PRIMARY KEY (path, observed_at, archive_tag),
    FOREIGN KEY (path, sha256) REFERENCES versions(path, sha256)
);
CREATE INDEX IF NOT EXISTS pages_race_day ON pages(race_day);
CREATE INDEX IF NOT EXISTS pages_queue ON pages(decision, category, path);
"""


def address(origin: str, base: str, link: str) -> str | None:
    if not link or any(ord(char) < 32 or ord(char) == 127 for char in link):
        return None
    try:
        url = urlsplit(urljoin(origin + base, link))
    except ValueError:
        return None
    if f"{url.scheme}://{url.netloc}" != origin:
        return None
    path = url.path or "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in unquote(path):
        return None
    return path + ("?" + url.query if url.query else "")


def classify(path: str, robots: RobotFileParser) -> tuple[str, str, str, str | None]:
    parts = urlsplit(path)
    decoded = unquote(parts.path)
    route = decoded.lstrip("/").partition("/")[0]
    category = CATEGORIES.get(route, "entry" if route in ENTRY_ROUTES else "unknown")
    day = None
    race_path = parts.path.replace("/race_kekka/", "/race_result/", 1)
    if RACE_PATH.fullmatch(race_path):
        try:
            day = date.fromisoformat(parts.path[-8:]).isoformat()
        except ValueError:
            pass
    if decoded.startswith("/edit") or route.casefold() in {"login", "logout", "account", "admin"}:
        return category, "excluded", "Editing or account page", day
    if PurePosixPath(decoded).suffix.lower() in STATIC_SUFFIXES:
        return category, "excluded", "Non-HTML asset", day
    if not robots.can_fetch(USER_AGENT, path):
        return category, "excluded", "Disallowed by robots.txt", day
    if parts.query and not RACE_ANALYSIS_LINK.fullmatch(path):
        return category, "review", "Unclassified query parameters", day
    if category == "unknown":
        return category, "review", "Unclassified page type", day
    return category, "ready", "", day


def observe(
    db: sqlite3.Connection,
    robots: RobotFileParser,
    path: str,
    source: str,
    lastmod: str | None,
    now: str,
) -> None:
    db.execute(
        """INSERT INTO pages VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET category=excluded.category,
           decision=excluded.decision, reason=excluded.reason, race_day=excluded.race_day""",
        (path, *classify(path, robots)),
    )
    db.execute(
        """INSERT INTO discoveries VALUES (?, ?, ?, ?)
           ON CONFLICT(path, source) DO UPDATE SET
           lastmod=excluded.lastmod, seen_at=excluded.seen_at""",
        (path, source, lastmod, now),
    )


def sitemap_paths(site: Site) -> Iterator[tuple[str, ElementTree.Element]]:
    indexes = {
        path
        for link in site.robots.site_maps() or []
        if (path := same_site_path(site.origin, link)) and path.startswith("/sitemap")
    }
    if not indexes:
        raise ValueError("Robots.txt did not declare a same-site sitemap.")
    pending = sorted(indexes)
    visited: set[str] = set()
    found_document = False
    while pending:
        path = pending.pop(0)
        if path in visited:
            continue
        if len(visited) >= MAX_SITEMAPS:
            raise ValueError("Sitemap count exceeds the inventory run limit.")
        visited.add(path)
        element = site.sitemap_document(path)
        match element.tag.rsplit("}", 1)[-1]:
            case "urlset":
                found_document = True
                yield path, element
            case "sitemapindex":
                children = sorted(
                    {
                        child
                        for link in element.findall("./*/{*}loc")
                        if link.text
                        and (child := same_site_path(site.origin, link.text.strip()))
                        and child.startswith("/sitemap")
                    }
                )
                if not children:
                    raise ValueError("The index has no supported same-site sitemap entries.")
                pending.extend(children)
            case _:
                raise ValueError("Unexpected sitemap XML root.")
    if not found_document:
        raise ValueError("No sitemap URL lists were found.")


class Links(HTMLParser):
    def __init__(self, origin: str, base: str) -> None:
        super().__init__()
        self.origin = origin
        self.base = base
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a" and (link := dict(attrs).get("href")):
            if (path := address(self.origin, self.base, link)) is not None:
                self.paths.add(path)


def observe_links(
    db: sqlite3.Connection, site: Site, path: str, text: str, observed_at: str
) -> None:
    parser = Links(site.origin, path)
    parser.feed(text)
    for linked in sorted(parser.paths):
        # Keep one verified referring page per URL; do not store the whole link graph.
        if (
            db.execute(
                "SELECT 1 FROM discoveries WHERE path=? AND source LIKE 'link:%' LIMIT 1",
                (linked,),
            ).fetchone()
            is None
        ):
            observe(db, site.robots, linked, "link:" + path, None, observed_at)


def check_hold(db: sqlite3.Connection) -> None:
    hold = db.execute("SELECT status, retry_at FROM site_hold WHERE id=1").fetchone()
    if hold is None:
        return
    status, retry_at = hold
    if status == 403:
        raise ValueError("The latest catalog records an HTTP 403 stop.")
    if retry_at is None or datetime.fromisoformat(retry_at) > datetime.now(UTC):
        raise ValueError(f"The site requested a pause until {retry_at or 'review'}.")
    db.execute("DELETE FROM site_hold WHERE id=1")


def record_hold(db: sqlite3.Connection, error: Blocked) -> None:
    db.execute("INSERT OR REPLACE INTO site_hold VALUES (1, ?, ?)", (error.status, error.retry_at))


def import_race_backup(
    db: sqlite3.Connection, site: Site, tag: str, archive: Path, indexed_at: str
) -> None:
    if not RACE_TAG.fullmatch(tag):
        raise ValueError("Invalid race Release tag.")
    with ZipFile(archive) as bundle:
        if bundle.getinfo("checkpoint.json").file_size > 64 * 1024:
            raise ValueError("Race checkpoint exceeds the supported size.")
        day = date.fromisoformat(json.loads(bundle.read("checkpoint.json"))["day"])
    with TemporaryDirectory() as temp:
        folder = Path(temp)
        (folder / "pages").mkdir()
        state = restore(folder, archive, day, site.origin)
        for path, saved in state.pages.items():
            observe(db, site.robots, path, "backup:" + tag, None, indexed_at)
            member = archive_name(path)
            db.execute(
                """INSERT OR IGNORE INTO versions VALUES (?, ?, ?, ?, ?, ?)""",
                (path, saved.sha256, tag, member, saved.bytes, indexed_at),
            )
            db.execute(
                """DELETE FROM missing WHERE path=?
                   AND NOT EXISTS (SELECT 1 FROM page_checks WHERE path=?)""",
                (path, path),
            )
            headers = Message()
            headers["Content-Type"] = saved.content_type
            html = (
                (folder / member)
                .read_bytes()
                .decode(headers.get_content_charset() or "utf-8", errors="replace")
            )
            observe_links(db, site, path, html, indexed_at)
        for path, reason in state.missing.items():
            observe(db, site.robots, path, "backup:" + tag, None, indexed_at)
            db.execute(
                """INSERT OR IGNORE INTO missing
                   SELECT ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM versions WHERE path = ?)""",
                (path, reason, tag, path),
            )


def restore_catalog(destination: Path, archive: Path, origin: str | None) -> None:
    verify_checksum(archive)
    with ZipFile(archive) as bundle:
        if bundle.getinfo("catalog.sqlite3").file_size > MAX_CATALOG_BYTES:
            raise ValueError("Previous catalog is too large.")
        destination.write_bytes(bundle.read("catalog.sqlite3"))
    with closing(sqlite3.connect(destination, autocommit=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Previous catalog failed SQLite integrity checks.")
        meta = dict(db.execute("SELECT key, value FROM meta"))
        if meta.get("version") != "1" or (
            origin is not None and meta.get("site_hash") != sha256(origin.encode())
        ):
            raise ValueError("Previous catalog belongs to a different site or format.")


def verify_checksum(archive: Path) -> None:
    fields = archive.with_suffix(".zip.sha256").read_text(encoding="ascii").split()
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if fields != [digest, archive.name]:
        raise ValueError("Archive SHA-256 did not match.")


def write_checksum(archive: Path) -> None:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")


def summarize(db: sqlite3.Connection) -> dict[str, object]:
    grouped = db.execute("""
        SELECT category,
          CASE
            WHEN EXISTS (SELECT 1 FROM missing WHERE missing.path = pages.path)
             AND EXISTS (SELECT 1 FROM versions WHERE versions.path = pages.path)
              THEN 'saved_but_unavailable'
            WHEN EXISTS (SELECT 1 FROM versions WHERE versions.path = pages.path) THEN 'saved'
            WHEN decision != 'ready' THEN decision
            WHEN EXISTS (SELECT 1 FROM missing WHERE missing.path = pages.path) THEN 'missing'
            ELSE 'pending'
          END AS state,
          COUNT(*)
        FROM pages GROUP BY category, state ORDER BY category, state
    """)
    categories: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for category, state, count in grouped:
        categories.setdefault(category, {})[state] = count
        totals[state] = totals.get(state, 0) + count
    return {
        "known_urls": sum(totals.values()),
        "by_state": totals,
        "by_category": categories,
        "archived_html_versions": db.execute("SELECT COUNT(*) FROM versions").fetchone()[0],
        "sitemap_urls_ever_seen": db.execute(
            "SELECT COUNT(DISTINCT path) FROM discoveries WHERE source LIKE 'sitemap:%'"
        ).fetchone()[0],
        "race_days": db.execute(
            "SELECT COUNT(DISTINCT race_day) FROM pages WHERE category = 'race'"
        ).fetchone()[0],
    }


def build_catalog(
    origin: str, output: Path, *, previous: Path | None, race_backups: dict[str, Path]
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=False)
    database = output / "catalog.sqlite3"
    if previous is not None:
        restore_catalog(database, previous, origin)
    now = datetime.now(UTC).isoformat()
    site = Site(origin, Checkpoint("", sha256(origin.encode())))
    listed = 0
    sitemaps = []
    blocked = None
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(SCHEMA)
        with db:
            check_hold(db)
            db.executemany(
                "INSERT OR IGNORE INTO meta VALUES (?, ?)",
                (("version", "1"), ("site_hash", sha256(origin.encode()))),
            )
        try:
            with db:
                site.read_robots()
                # Recheck old entries against the current rules even if their source disappeared.
                if previous is not None:
                    for (path,) in db.execute("SELECT path FROM pages").fetchall():
                        db.execute(
                            "UPDATE pages SET category=?, decision=?, reason=?, race_day=? "
                            "WHERE path=?",
                            (*classify(path, site.robots), path),
                        )
                for sitemap, xml in sitemap_paths(site):
                    sitemaps.append(sitemap)
                    for item in xml.findall("{*}url"):
                        if path := address(site.origin, "/", item.findtext("{*}loc", "").strip()):
                            observe(
                                db,
                                site.robots,
                                path,
                                "sitemap:" + sitemap,
                                item.findtext("{*}lastmod"),
                                now,
                            )
                            listed += 1
                for tag, archive in race_backups.items():
                    import_race_backup(db, site, tag, archive, now)
        except Blocked as error:
            blocked = error
            with db:
                record_hold(db, error)
        summary = summarize(db)
    summary.update(
        {
            "observed_at_utc": now,
            "state": "blocked" if blocked else "success",
            "sitemaps_read": len(sitemaps),
            "sitemap_rows_this_run": listed if blocked is None else 0,
            "site_requests": site.checkpoint.requests_this_run,
            "race_backups_imported": list(race_backups) if blocked is None else [],
        }
    )
    if blocked is not None:
        summary.update({"blocked_status": blocked.status, "retry_at": blocked.retry_at})
    package_catalog(output, summary)
    if blocked is not None:
        raise blocked
    return summary


def package_catalog(output: Path, summary: dict[str, object]) -> None:
    if (output / "catalog.sqlite3").stat().st_size > MAX_CATALOG_BYTES:
        raise ValueError("Catalog reached the supported size limit.")
    (output / "inventory.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    archive = output / "catalog.zip"
    with ZipFile(archive, "x", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in ("catalog.sqlite3", "inventory.json"):
            bundle.write(output / name, name)
    write_checksum(archive)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--race-backup", action="append", default=[], metavar="TAG=FILE")
    args = parser.parse_args()
    try:
        target = urlsplit(load_target_url())
        backups = dict(
            (tag, Path(path)) for tag, path in (item.split("=", 1) for item in args.race_backup)
        )
        summary = build_catalog(
            f"{target.scheme}://{target.netloc}",
            args.output,
            previous=args.previous,
            race_backups=backups,
        )
    except Blocked as error:
        print(error)
        if error.retry_at:
            print(f"Retry no earlier than {error.retry_at}.")
        return 2
    except (
        ValueError,
        TypeError,
        OSError,
        KeyError,
        URLError,
        BadZipFile,
        ElementTree.ParseError,
        sqlite3.Error,
    ):
        parser.exit(1, "Could not verify the inventory, source sitemaps, or previous backups.\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
