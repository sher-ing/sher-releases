#!/usr/bin/env python3
"""Create and verify the closed artifact set handed to the public publisher."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path


SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SUPPORTED_TARGETS = {
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
}


class BundleError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def exact_changelog_section(path: Path, version: str) -> str:
    text = path.read_text()
    heading = re.compile(
        rf"^## \[{re.escape(version)}\](?: - \d{{4}}-\d{{2}}-\d{{2}})?$", re.MULTILINE
    )
    matches = list(heading.finditer(text))
    if len(matches) != 1:
        raise BundleError(f"expected one exact changelog section for {version}")
    start = matches[0].start()
    following = re.search(r"^## ", text[matches[0].end() :], re.MULTILINE)
    end = matches[0].end() + following.start() if following else len(text)
    section = text[start:end].rstrip() + "\n"
    if not re.search(r"^- \S", section, re.MULTILINE):
        raise BundleError(f"changelog section for {version} has no release note")
    return section


def canonical_bundle_digest(bundle: Path) -> str:
    digest = hashlib.sha256()
    for name in ("asset-manifest.json", "provenance.json", "release-notes.md"):
        path = bundle / name
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def asset_records(directory: Path) -> list[dict[str, object]]:
    if not directory.is_dir():
        raise BundleError(f"assets directory does not exist: {directory}")
    records: list[dict[str, object]] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise BundleError(f"asset set must contain only regular files: {path.name}")
        records.append({"name": path.name, "size": path.stat().st_size, "sha256": sha256(path)})
    if not records:
        raise BundleError("asset set is empty")
    names = {record["name"] for record in records}
    required = {"dist-manifest.json", "sher-release.json"}
    if not required.issubset(names):
        raise BundleError(f"asset set is missing: {', '.join(sorted(required - names))}")
    return records


def validate_asset_contract(records: list[dict[str, object]], channel: str) -> None:
    names = {str(record["name"]) for record in records}
    required_global = {
        "dist-manifest.json",
        "sher-installer.sh",
        "sher-release.json",
        "sha256.sum",
    }
    missing_global = required_global - names
    if missing_global:
        raise BundleError(
            f"asset set is missing global artifacts: {', '.join(sorted(missing_global))}"
        )

    archive_targets: set[str] = set()
    for name in names:
        if not name.startswith("sher-") or not name.endswith(".tar.gz"):
            continue
        target = name.removeprefix("sher-").removesuffix(".tar.gz")
        if target not in SUPPORTED_TARGETS:
            raise BundleError(f"unsupported release archive: {name}")
        archive_targets.add(target)
        if f"{name}.sha256" not in names:
            raise BundleError(f"release archive has no checksum asset: {name}")

    if not archive_targets:
        raise BundleError("asset set has no supported release archive")
    if channel == "stable" and archive_targets != SUPPORTED_TARGETS:
        missing = SUPPORTED_TARGETS - archive_targets
        raise BundleError(
            f"stable asset set is missing targets: {', '.join(sorted(missing))}"
        )


def create(args: argparse.Namespace) -> None:
    if not COMMIT.fullmatch(args.source_commit):
        raise BundleError("source commit must be a full lowercase SHA")
    if args.channel not in {"stable", "dev"}:
        raise BundleError("channel must be stable or dev")
    expected_tag = f"v{args.version}"
    if args.public_tag != expected_tag:
        raise BundleError(f"public tag must be {expected_tag}")
    if args.channel == "stable" and args.source_tag != expected_tag:
        raise BundleError(f"stable source tag must be {expected_tag}")
    if args.channel == "dev" and args.source_tag:
        raise BundleError("dev bundles must not claim a private source tag")

    output = args.output.resolve()
    if output.exists():
        raise BundleError(f"output already exists: {output}")
    output.mkdir(parents=True)
    shutil.copytree(args.assets, output / "assets")

    if args.changelog:
        notes = exact_changelog_section(args.changelog, args.version)
    else:
        notes = args.release_notes.read_text()
        if not notes.startswith(f"## [{args.version}]"):
            raise BundleError("release notes do not begin with the exact version heading")
    (output / "release-notes.md").write_text(notes.rstrip() + "\n")

    records = asset_records(output / "assets")
    validate_asset_contract(records, args.channel)
    manifest = {
        "schema_version": 1,
        "version": args.version,
        "channel": args.channel,
        "source_repository": args.source_repository,
        "source_commit": args.source_commit,
        "source_tag": args.source_tag or None,
        "public_tag": args.public_tag,
        "assets": records,
    }
    (output / "asset-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    provenance = {
        "schema_version": 1,
        "private_source_repository": args.source_repository,
        "private_source_commit": args.source_commit,
        "private_source_tag": args.source_tag or None,
        "public_distribution_tag": args.public_tag,
        "version": args.version,
        "channel": args.channel,
    }
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    (output / "bundle.sha256").write_text(canonical_bundle_digest(output) + "\n")
    verify_bundle(output)


def verify_bundle(bundle: Path) -> None:
    expected = (bundle / "bundle.sha256").read_text().strip()
    if not SHA256.fullmatch(expected):
        raise BundleError("bundle.sha256 is not one lowercase SHA-256 digest")
    actual = canonical_bundle_digest(bundle)
    if actual != expected:
        raise BundleError("sealed bundle digest does not match")
    manifest = json.loads((bundle / "asset-manifest.json").read_text())
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("assets"), list):
        raise BundleError("unsupported asset manifest")
    expected_names: set[str] = set()
    for record in manifest["assets"]:
        if set(record) != {"name", "size", "sha256"}:
            raise BundleError("asset record has unexpected fields")
        name = record["name"]
        if not isinstance(name, str) or Path(name).name != name or name in expected_names:
            raise BundleError("asset names must be unique basenames")
        expected_names.add(name)
        path = bundle / "assets" / name
        if not path.is_file() or path.is_symlink():
            raise BundleError(f"missing regular asset: {name}")
        if path.stat().st_size != record["size"] or sha256(path) != record["sha256"]:
            raise BundleError(f"asset changed after sealing: {name}")
    actual_names = {path.name for path in (bundle / "assets").iterdir()}
    if actual_names != expected_names:
        raise BundleError("asset allowlist does not match bundle contents")
    channel = manifest.get("channel")
    if channel not in {"stable", "dev"}:
        raise BundleError("sealed bundle has an invalid channel")
    validate_asset_contract(manifest["assets"], str(channel))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    make = commands.add_parser("create")
    make.add_argument("--assets", type=Path, required=True)
    make.add_argument("--output", type=Path, required=True)
    make.add_argument("--version", required=True)
    make.add_argument("--channel", choices=("stable", "dev"), required=True)
    make.add_argument("--source-repository", required=True)
    make.add_argument("--source-commit", required=True)
    make.add_argument("--source-tag", default="")
    make.add_argument("--public-tag", required=False)
    notes = make.add_mutually_exclusive_group(required=True)
    notes.add_argument("--changelog", type=Path)
    notes.add_argument("--release-notes", type=Path)
    check = commands.add_parser("verify")
    check.add_argument("--bundle", type=Path, required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "create":
            args.public_tag = args.public_tag or f"v{args.version}"
            create(args)
        else:
            verify_bundle(args.bundle.resolve())
        return 0
    except (BundleError, OSError, json.JSONDecodeError) as error:
        print(f"build_release_bundle: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
