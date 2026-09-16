#!/usr/bin/env python3
"""Validate and execute an authenticated downloaded Sher draft artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path


TARGETS = {
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
}


class SmokeError(ValueError):
    pass


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def request_json(url: str) -> dict[str, object]:
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise SmokeError("GH_TOKEN is required to read an authenticated draft")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "sher-release-smoke",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise SmokeError("GitHub release response is not an object")
    return value


def download(url: str, destination: Path, expected_size: int) -> None:
    token = os.environ["GH_TOKEN"]
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "sher-release-smoke",
        },
    )
    size = 0
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("xb") as output:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            size += len(block)
            if size > expected_size:
                raise SmokeError(f"downloaded asset exceeded its declared size: {destination.name}")
            output.write(block)
    if size != expected_size:
        raise SmokeError(f"downloaded asset has the wrong size: {destination.name}")


def exact_assets(release: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = release.get("assets")
    if not isinstance(raw, list):
        raise SmokeError("release has no asset list")
    result: dict[str, dict[str, object]] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise SmokeError("release has a malformed asset")
        name = item["name"]
        if name in result:
            raise SmokeError(f"release has duplicate asset {name}")
        result[name] = item
    return result


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise SmokeError(f"{path.name} is not an object")
    return value


def fetch(args: argparse.Namespace) -> None:
    if args.target not in TARGETS:
        raise SmokeError(f"unsupported target {args.target}")
    release = request_json(
        f"https://api.github.com/repos/{args.repository}/releases/{args.release_id}"
    )
    if release.get("id") != args.release_id:
        raise SmokeError("GitHub returned a different release ID")
    if release.get("tag_name") != f"v{args.version}" or release.get("draft") is not True:
        raise SmokeError("release is not the expected draft identity")

    assets = exact_assets(release)
    archive_name = f"sher-{args.target}.tar.gz"
    names = {
        archive_name,
        f"{archive_name}.sha256",
        "sher-installer.sh",
        "sher-release.json",
        "dist-manifest.json",
        "asset-manifest.json",
        "provenance.json",
        "bundle.sha256",
    }
    missing = names - set(assets)
    if missing:
        raise SmokeError(f"draft is missing assets: {', '.join(sorted(missing))}")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    for name in sorted(names):
        url = assets[name].get("url")
        size = assets[name].get("size")
        if not isinstance(url, str) or not isinstance(size, int) or size < 0:
            raise SmokeError(f"draft asset has no valid API URL and size: {name}")
        download(url, output / name, size)

    sealed = read_json(output / "asset-manifest.json")
    if (
        sealed.get("schema_version") != 1
        or sealed.get("version") != args.version
        or sealed.get("source_commit") != args.source_commit
        or sealed.get("public_tag") != f"v{args.version}"
    ):
        raise SmokeError("sealed asset manifest identity does not match the smoke inputs")
    records = sealed.get("assets")
    if not isinstance(records, list):
        raise SmokeError("sealed asset manifest has no asset records")
    by_name = {
        record.get("name"): record
        for record in records
        if isinstance(record, dict) and isinstance(record.get("name"), str)
    }
    for name in names - {"asset-manifest.json", "provenance.json", "bundle.sha256"}:
        record = by_name.get(name)
        if not isinstance(record, dict):
            raise SmokeError(f"sealed allowlist omits {name}")
        path = output / name
        if record.get("size") != path.stat().st_size or record.get("sha256") != digest(path):
            raise SmokeError(f"downloaded asset disagrees with sealed record: {name}")

    metadata = read_json(output / "sher-release.json")
    expected_channel = "dev" if "-" in args.version else "stable"
    if (
        metadata.get("schema_version") != 1
        or metadata.get("version") != args.version
        or metadata.get("channel") != expected_channel
        or metadata.get("source_commit") != args.source_commit
        or metadata.get("state_impact") != "none"
    ):
        raise SmokeError("Sher release metadata is incompatible or has the wrong identity")

    provenance = read_json(output / "provenance.json")
    if (
        provenance.get("private_source_commit") != args.source_commit
        or provenance.get("public_distribution_tag") != f"v{args.version}"
    ):
        raise SmokeError("public provenance does not bind the expected source and tag")

    manifest = read_json(output / "dist-manifest.json")
    if manifest.get("announcement_tag") != f"v{args.version}":
        raise SmokeError("cargo-dist manifest has the wrong announcement tag")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise SmokeError("cargo-dist manifest has no artifact map")
    artifact = artifacts.get(archive_name)
    if (
        not isinstance(artifact, dict)
        or artifact.get("target_triples") != [args.target]
        or artifact.get("checksum") != f"{archive_name}.sha256"
    ):
        raise SmokeError("cargo-dist manifest does not describe the selected archive")

    checksum = (output / f"{archive_name}.sha256").read_text().split()
    if len(checksum) != 2 or checksum[0] != digest(output / archive_name):
        raise SmokeError("downloaded archive checksum does not match")

    binary_root = output / "archive"
    binary_root.mkdir()
    with tarfile.open(output / archive_name, "r:gz") as archive:
        for member in archive.getmembers():
            member_path = Path(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise SmokeError("archive contains an unsafe path")
            if not (member.isfile() or member.isdir()):
                raise SmokeError("archive contains a link or special file")
        archive.extractall(binary_root)
    binaries = [path for path in binary_root.rglob("sher") if path.is_file()]
    if len(binaries) != 1:
        raise SmokeError(f"archive must contain exactly one sher binary, found {len(binaries)}")
    shutil.copy2(binaries[0], output / "sher")
    (output / "sher").chmod(0o755)


def run(command: list[str], env: dict[str, str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise SmokeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def exercise_tui(binary: Path, env: dict[str, str]) -> None:
    pid, master = pty.fork()
    if pid == 0:
        os.execve(str(binary), [str(binary)], env)
    output = bytearray()
    deadline = time.monotonic() + 15
    sent_quit = False
    status: int | None = None
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([master], [], [], 0.25)
            if readable:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    pass
            if not sent_quit and time.monotonic() + 13 < deadline:
                os.write(master, b"q")
                sent_quit = True
            done, value = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                status = value
                break
        if status is None:
            os.kill(pid, signal.SIGTERM)
            _, status = os.waitpid(pid, 0)
            raise SmokeError("TUI did not exit after q")
        if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
            raise SmokeError(f"TUI failed in a pseudo-terminal: {output.decode(errors='replace')}")
    finally:
        os.close(master)


def exercise_update_check(binary: Path, env: dict[str, str], channel: str) -> None:
    check: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, 4):
        check = subprocess.run(
            [str(binary), "update", "--check", "--channel", channel],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if check.returncode == 0:
            break
        if check.returncode != 20:
            raise SmokeError(
                f"explicit update check failed after {attempt} attempt(s) with status "
                f"{check.returncode}: {check.stdout}{check.stderr}"
            )
        if attempt == 3:
            break
        time.sleep(attempt)
    assert check is not None
    report = check.stdout + check.stderr
    if check.returncode == 0:
        expected = (f"Selected channel: {channel}", "Action:")
    else:
        # Draft smoke runs before the release is public. GitHub can rate-limit
        # the updater's anonymous discovery request on hosted runners. Status
        # 20 is the updater's documented safe failure: it must explain the
        # network error and prove that it did not mutate the installation.
        expected = (
            f"Selected channel: {channel}",
            "sher: update check failed:",
            "No changes were made.",
        )
    missing = [line for line in expected if line not in report]
    if missing:
        raise SmokeError(
            "explicit update check did not produce its expected report "
            f"({', '.join(missing)}): {report}"
        )


def exercise(args: argparse.Namespace) -> None:
    assets = args.assets.resolve()
    binary = assets / "sher"
    identity = json.loads(run([str(binary), "--version", "--json"], os.environ.copy()).stdout)
    if (
        identity.get("version") != args.version
        or identity.get("channel") != args.channel
        or identity.get("source_commit") != args.source_commit
        or identity.get("target") != args.target
    ):
        raise SmokeError(f"binary identity mismatch: {identity}")

    with tempfile.TemporaryDirectory(prefix="sher-release-smoke-") as directory:
        root = Path(directory)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(root / "home"),
                "CARGO_HOME": str(root / "cargo"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "SHER_DATA_DIR": str(root / "data" / "sher"),
                "SHER_NO_MODIFY_PATH": "1",
                "PATH": f"{root / 'cargo' / 'bin'}:{os.environ.get('PATH', '')}",
            }
        )
        env.setdefault("TERM", "xterm-256color")
        for path in (root / "home", root / "cargo", root / "config", root / "data", root / "cache"):
            path.mkdir(parents=True)

        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", "38471", "--bind", "127.0.0.1"],
            cwd=assets,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.5)
            install_env = env | {"SHER_DOWNLOAD_URL": "http://127.0.0.1:38471"}
            run(["sh", str(assets / "sher-installer.sh")], install_env, timeout=120)
        finally:
            server.terminate()
            server.wait(timeout=5)

        installed = root / "cargo" / "bin" / "sher"
        installed_identity = json.loads(
            run([str(installed), "--version", "--json"], env).stdout
        )
        if installed_identity != identity:
            raise SmokeError("installed binary identity differs from downloaded archive")
        receipt = read_json(root / "config" / "sher" / "sher-receipt.json")
        if (
            receipt.get("version") != args.version
            or receipt.get("install_layout") != "cargo-home"
            or receipt.get("install_prefix") != str(root / "cargo")
        ):
            raise SmokeError("cargo-dist receipt does not own the installed binary")

        exercise_tui(installed, env)
        run([str(installed), "daemon", "--once"], env, timeout=60)
        run([str(installed), "daemon", "start"], env, timeout=30)
        status = run([str(installed), "daemon", "status"], env).stdout
        if "daemon: detached/manual" not in status:
            raise SmokeError(f"detached daemon did not report ownership: {status}")
        run([str(installed), "daemon", "stop"], env, timeout=30)
        run([str(installed), "daemon", "install"], env, timeout=30)
        registered = run([str(installed), "daemon", "status"], env).stdout
        if "registration: none" in registered:
            raise SmokeError("daemon install did not create a platform registration")
        run([str(installed), "daemon", "uninstall"], env, timeout=30)
        unregistered = run([str(installed), "daemon", "status"], env).stdout
        if "registration: none" not in unregistered:
            raise SmokeError("daemon uninstall left a registration behind")

        exercise_update_check(installed, env, args.channel)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    get = commands.add_parser("fetch")
    get.add_argument("--repository", required=True)
    get.add_argument("--release-id", required=True, type=int)
    get.add_argument("--version", required=True)
    get.add_argument("--source-commit", required=True)
    get.add_argument("--target", choices=sorted(TARGETS), required=True)
    get.add_argument("--output", type=Path, required=True)
    run_parser = commands.add_parser("exercise")
    run_parser.add_argument("--assets", type=Path, required=True)
    run_parser.add_argument("--version", required=True)
    run_parser.add_argument("--channel", choices=("stable", "dev"), required=True)
    run_parser.add_argument("--source-commit", required=True)
    run_parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "fetch":
            fetch(args)
        else:
            exercise(args)
        return 0
    except (SmokeError, OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        print(f"release_smoke: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
