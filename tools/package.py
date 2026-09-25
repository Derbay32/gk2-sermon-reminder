#!/usr/bin/env python3
"""GKSA-10 distribution packaging helper.

Assembles the distributable set from an EXPLICIT allowlist and nothing else:

  * the plugin's own DLL (``GK2.SermonReminder.dll``);
  * the repository ``LICENSE``;
  * the plugin's own metadata (this helper's emitted metadata JSON), which is
    generated from the plugin assembly rather than copied from a third party.

Anything not on the allowlist is refused. Native game, BepInEx, framework and
any other third-party assembly can therefore never enter the archive.

The output is written under an ignored ``artifacts/`` path and is local-only:
it is not a hosted CI result and not a game execution/E2E verdict. Only the
Python 3 standard library is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

# Explicit distribution allowlist. Only these repository-relative sources may
# be packaged. No globs, no directory sweeps.
ALLOWLIST = (
    "src/GK2.SermonReminder/bin/Release/netstandard2.1/GK2.SermonReminder.dll",
    "LICENSE",
)

# Archive member paths for each allowlisted source, so the archive layout is
# as explicit as the allowlist itself.
ARCHIVE_LAYOUT = {
    "src/GK2.SermonReminder/bin/Release/netstandard2.1/GK2.SermonReminder.dll":
        "BepInEx/plugins/GK2.SermonReminder/GK2.SermonReminder.dll",
    "LICENSE": "BepInEx/plugins/GK2.SermonReminder/LICENSE",
}

METADATA_ARCHIVE_PATH = "BepInEx/plugins/GK2.SermonReminder/gksa10-metadata.json"

PROHIBITED_BASENAMES = (
    "assembly-csharp.dll", "lazybeartechnology.dll", "unityengine.dll",
    "bepinex.dll", "0harmony.dll", "gk2.framework.dll", "newtonsoft.json.dll",
)

GUID_RE = re.compile(rb"com\.derbay32\.gk2\.sermonreminder")
INFORMATIONAL_VERSION_RE = re.compile(rb"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?\+[0-9a-f]{40})")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GKSA-10 distribution packaging helper.")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--artifacts-dir", default="artifacts/dist")
    parser.add_argument("--output-name", default="GK2.SermonReminder.zip")
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    artifacts = (root / args.artifacts_dir).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    archive_path = artifacts / args.output_name
    metadata_path = artifacts / "gksa10-metadata.json"

    errors = []

    # 1. Every allowlisted source must exist exactly where declared.
    members = []
    for relative in ALLOWLIST:
        source = root / relative
        if not source.is_file():
            errors.append(f"allowlisted distribution source missing: {relative}")
            continue
        lower = source.name.lower()
        if lower in PROHIBITED_BASENAMES:
            errors.append(f"allowlist contains a prohibited third-party assembly: {relative}")
        members.append((source, ARCHIVE_LAYOUT[relative]))

    if errors:
        for error in errors:
            sys.stderr.write(f"FAIL [package] {error}\n")
        return 2

    dll_relative = ALLOWLIST[0]
    dll_path = root / dll_relative
    dll_bytes = dll_path.read_bytes()

    # 2. Own metadata only, derived from the plugin assembly itself.
    informational = INFORMATIONAL_VERSION_RE.search(dll_bytes)
    metadata = {
        "artifactVersion": 1,
        "kind": "gksa10-distribution-metadata",
        "localOnly": True,
        "hostedCiResult": False,
        "gameE2eVerdict": None,
        "mod": {
            "name": "GK2 Sermon Reminder",
            "version": "0.1.0",
            "guid": "com.derbay32.gk2.sermonreminder",
            "assembly": "GK2.SermonReminder",
            "informationalVersion": informational.group(1).decode("ascii") if informational else None,
            "sha256": sha256_file(dll_path),
        },
        "files": [
            {
                "archivePath": archive_path_in_zip,
                "sourceRelativePath": str(source.relative_to(root)).replace("\\", "/"),
                "sha256": sha256_file(source),
                "sizeBytes": source.stat().st_size,
            }
            for source, archive_path_in_zip in members
        ],
        "note": (
            "Distribution metadata generated locally from the plugin's own build output "
            "and the repository LICENSE. Not a hosted CI result and not a game E2E verdict."
        ),
    }

    if not GUID_RE.search(dll_bytes):
        sys.stderr.write(
            "FAIL [package] plugin DLL does not contain the expected plugin GUID constant\n"
        )
        return 2

    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    # 3. Write the archive with exactly the allowlisted members + own metadata.
    if archive_path.exists():
        archive_path.unlink()
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, archive_name in members:
            archive.write(source, archive_name)
        archive.write(metadata_path, METADATA_ARCHIVE_PATH)

    # 4. Verify the archive contents match the allowlist exactly.
    with zipfile.ZipFile(archive_path) as archive:
        actual = sorted(archive.namelist())
    expected = sorted([name for _, name in members] + [METADATA_ARCHIVE_PATH])
    if actual != expected:
        sys.stderr.write(f"FAIL [package] archive members {actual} != allowlist {expected}\n")
        return 2

    for name in actual:
        if Path(name).name.lower() in PROHIBITED_BASENAMES:
            sys.stderr.write(f"FAIL [package] prohibited assembly in archive: {name}\n")
            return 2

    sys.stderr.write(
        f"RESULT: PASS (distribution allowlist; {len(actual)} members; "
        f"archive {archive_path.relative_to(root)}; dll sha256 {metadata['mod']['sha256']})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
