"""Restore and verify one archived HTML version using downloaded Release files."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from batch_capture import archive_name, sha256
from fetch_page import MAX_BYTES
from site_capture import BATCH_TAG
from site_catalog import RACE_TAG, restore_catalog, summarize, verify_checksum


@dataclass(frozen=True)
class Version:
    tag: str
    archive: str
    member: str
    digest: str
    size: int


def archive_filename(tag: str) -> str:
    if RACE_TAG.fullmatch(tag):
        return "batch.zip"
    if BATCH_TAG.fullmatch(tag):
        return "pages.zip"
    raise ValueError("Unexpected release tag in the catalog.")


def checked_version(path: str, tag: str, member: str, digest: str, size: int) -> Version:
    if member != archive_name(path) or not 0 < size <= MAX_BYTES:
        raise ValueError("Invalid HTML member in the catalog.")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid HTML digest in the catalog.")
    return Version(tag, archive_filename(tag), member, digest, size)


def locate_version(catalog: Path, path: str, digest: str | None = None) -> Version:
    if not path.startswith("/") or path.startswith("//") or "#" in path:
        raise ValueError("Use the exact same-site path from the catalog.")
    if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Expected a lowercase SHA-256 digest.")
    with TemporaryDirectory() as scratch:
        database = Path(scratch) / "catalog.sqlite3"
        restore_catalog(database, catalog, None)
        with closing(sqlite3.connect(database, autocommit=True)) as db:
            if (
                digest is None
                and db.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='page_changes'"
                ).fetchone()
            ):
                last_saved = db.execute(
                    """SELECT sha256 FROM page_changes
                       WHERE path=? AND status='saved'
                       ORDER BY observed_at DESC, archive_tag DESC LIMIT 1""",
                    (path,),
                ).fetchone()
                if last_saved:
                    digest = last_saved[0]
            version = db.execute(
                """SELECT archive_tag, member, sha256, bytes FROM versions
                   WHERE path=? AND (? IS NULL OR sha256=?)
                   ORDER BY indexed_at DESC, sha256 LIMIT 1""",
                (path, digest, digest),
            ).fetchone()
    if version is None:
        raise ValueError("That page and version have no verified archive entry.")
    tag, member, found_digest, size = version
    return checked_version(path, tag, member, found_digest, size)


def read_html(bundle: ZipFile, version: Version) -> bytes:
    if bundle.getinfo(version.member).file_size != version.size:
        raise ValueError("Archived HTML size did not match the catalog.")
    raw = bundle.read(version.member)
    if sha256(raw) != version.digest:
        raise ValueError("Archived HTML SHA-256 did not match the catalog.")
    return raw


def restore_html(version: Version, downloaded: Path, output: Path) -> None:
    archive = downloaded / version.tag / version.archive
    verify_checksum(archive)
    with ZipFile(archive) as bundle:
        raw = read_html(bundle, version)
    with output.open("xb") as destination:
        destination.write(raw)


@dataclass
class ArchiveAudit:
    release_count: int
    catalog_versions: int
    verified_versions: int
    issues: list[dict[str, str | int]]
    coverage: dict[str, object]

    @property
    def unverified_versions(self) -> int:
        return self.catalog_versions - self.verified_versions


def audit_catalog(catalog: Path, downloaded: Path) -> ArchiveAudit:
    """Check all indexed HTML versions. Read each referenced ZIP only once."""
    with TemporaryDirectory() as scratch:
        database = Path(scratch) / "catalog.sqlite3"
        restore_catalog(database, catalog, None)
        with closing(sqlite3.connect(database, autocommit=True)) as db:
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Catalog has dangling references.")
            audit = ArchiveAudit(0, 0, 0, [], summarize(db))
            rows = db.execute("""
                SELECT archive_tag, path, member, sha256, bytes FROM versions
                ORDER BY archive_tag, member, sha256
            """)
            for tag, entries in groupby(rows, key=lambda row: row[0]):
                audit.release_count += 1
                records = list(entries)
                audit.catalog_versions += len(records)
                try:
                    archive = downloaded / tag / archive_filename(tag)
                    verify_checksum(archive)
                    with ZipFile(archive) as bundle:
                        members = bundle.namelist()
                        if len(set(members)) != len(members):
                            raise ValueError("The archive has duplicate member names.")
                        for _, path, member, digest, size in records:
                            try:
                                read_html(bundle, checked_version(path, tag, member, digest, size))
                            except KeyError, ValueError, BadZipFile, OSError, RuntimeError:
                                audit.issues.append(
                                    {"tag": tag, "cause": "invalid_html_member", "sha256": digest}
                                )
                            else:
                                audit.verified_versions += 1
                except FileNotFoundError:
                    audit.issues.append(
                        {
                            "tag": tag,
                            "cause": "missing_archive_or_checksum",
                            "versions": len(records),
                        }
                    )
                except ValueError, BadZipFile, OSError, RuntimeError:
                    audit.issues.append(
                        {"tag": tag, "cause": "invalid_zip_or_checksum", "versions": len(records)}
                    )
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument("--path")
    task.add_argument("--audit", action="store_true", help="Verify every indexed HTML version.")
    parser.add_argument("--sha256")
    parser.add_argument("--releases", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.audit:
        if not args.releases or args.output or args.sha256:
            parser.error("--audit requires --releases and only returns an audit report.")
        try:
            audit = audit_catalog(args.catalog, args.releases)
        except ValueError, TypeError, OSError, KeyError, BadZipFile, sqlite3.Error:
            parser.exit(1, "Could not verify the archive catalog.\n")
        print(
            json.dumps(
                {**asdict(audit), "unverified_versions": audit.unverified_versions}, indent=2
            )
        )
        return int(bool(audit.unverified_versions))
    if bool(args.releases) != bool(args.output):
        parser.error("--releases and --output must be supplied together.")
    try:
        version = locate_version(args.catalog, args.path, args.sha256)
        if args.releases:
            restore_html(version, args.releases, args.output)
    except ValueError, TypeError, OSError, KeyError, BadZipFile, sqlite3.Error:
        parser.exit(1, "Could not find, verify, or restore the requested page version.\n")
    print(json.dumps(asdict(version), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
