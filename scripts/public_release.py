#!/usr/bin/env python3
"""Verify and publish a sealed GitHub draft by immutable release ID."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


class PublishError(ValueError):
    pass


def api(repository: str, path: str, *, method: str = "GET", body: object | None = None) -> object:
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise PublishError("GH_TOKEN is required")
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sher-release-publisher",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise PublishError(f"GitHub API {method} {path} failed: {error.code}") from error


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def expected_assets(bundle: Path) -> dict[str, tuple[int, str]]:
    manifest = json.loads((bundle / "asset-manifest.json").read_text())
    expected = {
        item["name"]: (item["size"], item["sha256"])
        for item in manifest["assets"]
    }
    for name in ("asset-manifest.json", "provenance.json", "bundle.sha256"):
        data = (bundle / name).read_bytes()
        expected[name] = (len(data), sha256(data))
    return expected


def download_asset(url: str) -> tuple[int, str]:
    token = os.environ["GH_TOKEN"]
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sher-release-publisher",
        },
    )
    size = 0
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=60) as response:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def verify(repository: str, release_id: int, bundle: Path) -> dict[str, object]:
    release = api(repository, f"releases/{release_id}")
    if not isinstance(release, dict):
        raise PublishError("release response is not an object")
    manifest = json.loads((bundle / "asset-manifest.json").read_text())
    if release.get("tag_name") != manifest["public_tag"]:
        raise PublishError("draft tag changed")
    if not release.get("draft"):
        raise PublishError("release is no longer the expected draft")
    release_body = release.get("body")
    if not isinstance(release_body, str):
        raise PublishError("release notes are missing")
    if release_body.rstrip() != (bundle / "release-notes.md").read_text().rstrip():
        raise PublishError("release notes changed after sealing")

    expected = expected_assets(bundle)
    remote_assets = release.get("assets")
    if not isinstance(remote_assets, list):
        raise PublishError("release assets are missing")
    by_name = {asset.get("name"): asset for asset in remote_assets}
    if set(by_name) != set(expected):
        raise PublishError("draft asset allowlist changed")
    for name, (size, digest) in expected.items():
        asset = by_name[name]
        if asset.get("size") != size:
            raise PublishError(f"draft asset size changed: {name}")
        downloaded_size, downloaded_digest = download_asset(asset["url"])
        if downloaded_size != size or downloaded_digest != digest:
            raise PublishError(f"draft asset digest changed: {name}")
    return release


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-id", type=int, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--verify-draft", action="store_true")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.verify_draft == args.publish:
        parser.error("choose exactly one of --verify-draft or --publish")
    try:
        verify(args.repository, args.release_id, args.bundle.resolve())
        if args.publish:
            result = api(
                args.repository,
                f"releases/{args.release_id}",
                method="PATCH",
                body={"draft": False},
            )
            if not isinstance(result, dict) or result.get("draft"):
                raise PublishError("GitHub did not publish the verified draft")
        return 0
    except (OSError, KeyError, json.JSONDecodeError, PublishError, urllib.error.URLError) as error:
        print(f"public_release: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
