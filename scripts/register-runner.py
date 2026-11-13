#!/usr/bin/env python3
"""Register another repository on this host without giving its runner Docker access."""

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


parser = argparse.ArgumentParser()
parser.add_argument("repository", help="GitHub OWNER/REPOSITORY")
parser.add_argument("app")
args = parser.parse_args()
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
    parser.error("Invalid repository")
if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", args.app):
    parser.error("Invalid app name")
account = "sd-" + args.app[:28]
directory = Path("/var/lib/server-deploy-runners") / args.app
if directory.exists():
    parser.error("Runner directory already exists; refusing to replace it")
release = json.loads(
    command(
        "gh", "api", "repos/actions/runner/releases/latest", capture_output=True
    ).stdout
)
asset = next(
    a
    for a in release["assets"]
    if re.fullmatch(r"actions-runner-linux-x64-[0-9.]+\.tar\.gz", a["name"])
)
digest = asset.get("digest", "").removeprefix("sha256:")
if not re.fullmatch(r"[0-9a-f]{64}", digest):
    parser.error("Release has no official SHA256 checksum")
with tempfile.TemporaryDirectory() as tmp:
    archive = Path(tmp) / "runner.tar.gz"
    urllib.request.urlretrieve(asset["browser_download_url"], archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        parser.error("Runner download checksum mismatch")
    command(
        "sudo",
        "useradd",
        "--system",
        "--user-group",
        "--home-dir",
        str(directory),
        "--shell",
        "/bin/bash",
        account,
    )
    command(
        "sudo",
        "install",
        "-d",
        "-m",
        "0700",
        "-o",
        account,
        "-g",
        account,
        str(directory),
    )
    command("sudo", "tar", "-xzf", str(archive), "-C", str(directory))
command("sudo", "chown", "-R", f"{account}:{account}", str(directory))
token = json.loads(
    command(
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{args.repository}/actions/runners/registration-token",
        capture_output=True,
    ).stdout
)["token"]
command(
    "sudo",
    "-H",
    "-u",
    account,
    "bash",
    "-c",
    'cd "$1"; shift; exec ./config.sh "$@"',
    "--",
    str(directory),
    "--url",
    "https://github.com/" + args.repository,
    "--token",
    token,
    "--name",
    "server-" + args.app,
    "--labels",
    "server-deploy",
    "--unattended",
    "--work",
    "_work",
)
rule = f"{account} ALL=(root) NOPASSWD: /opt/server-deploy/bin/server-deploy\n"
with tempfile.NamedTemporaryFile(mode="w") as output:
    output.write(rule)
    output.flush()
    command("sudo", "visudo", "-cf", output.name)
    command(
        "sudo",
        "install",
        "-m",
        "0440",
        output.name,
        "/etc/sudoers.d/server-deploy-" + args.app,
    )
command(
    "sudo",
    "bash",
    "-c",
    'cd "$1"; exec ./svc.sh install "$2"',
    "--",
    str(directory),
    account,
)
command("sudo", "bash", "-c", 'cd "$1"; exec ./svc.sh start', "--", str(directory))
print(
    "Runner installed. Add its _work directory to source_roots in the root-managed host configuration."
)
