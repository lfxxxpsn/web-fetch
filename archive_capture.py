"""Package one successful capture for GitHub Releases without making network requests."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

SOURCE_FILES = ("page.html", "report.json")
MAX_ASSET_BYTES = 2 * 1024**3  # GitHub requires each Release asset to be strictly under 2 GiB.


def create_archive(directory: Path) -> tuple[Path, Path]:
    """Archive only validated capture files; never overwrite an existing backup."""
    payloads = {name: (directory / name).read_bytes() for name in SOURCE_FILES}
    report = json.loads(payloads["report.json"])
    if not isinstance(report, dict) or report.get("success") is not True:
        raise ValueError("Only successful captures can be archived.")
    html = payloads["page.html"]
    if (
        report.get("status") != 200
        or report.get("bytes") != len(html)
        or report.get("sha256") != hashlib.sha256(html).hexdigest()
    ):
        raise ValueError("HTML does not match the successful capture report.")

    manifest = {
        "format_version": 1,
        "files": {
            name: {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            for name, body in payloads.items()
        },
    }
    archive = directory / "snapshot.zip"
    checksum = directory / "snapshot.zip.sha256"
    if archive.exists() or checksum.exists():
        raise FileExistsError("Backup already exists; existing files were not replaced.")
    created_archive = created_checksum = False
    try:
        with archive.open("xb") as stream:
            created_archive = True
            with ZipFile(stream, "w", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
                for name, body in payloads.items():
                    bundle.writestr(name, body)
                bundle.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        if archive.stat().st_size >= MAX_ASSET_BYTES:
            raise ValueError("Release asset must be smaller than 2 GiB.")
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        with checksum.open("x", encoding="ascii", newline="\n") as stream:
            created_checksum = True
            stream.write(f"{digest}  {archive.name}\n")
    except OSError, ValueError:
        if created_archive:
            archive.unlink(missing_ok=True)
        if created_checksum:
            checksum.unlink(missing_ok=True)
        raise
    return archive, checksum


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Directory containing one successful capture.")
    args = parser.parse_args()
    try:
        archive, checksum = create_archive(args.directory)
    except OSError, ValueError:
        parser.exit(
            1, "Archive failed: check capture files, report, size limit and existing backup.\n"
        )
    print(f"Created: {archive}\nChecksum: {checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
