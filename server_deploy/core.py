import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path


class Blocked(RuntimeError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        parent = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def run(args, *, cwd=None, env=None, timeout=600, check=True):
    result = subprocess.run(
        args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout
    )
    if check and result.returncode:
        # Command output can contain environment values, credentials, or build secrets.
        raise Blocked(
            f"{Path(args[0]).name} {args[1]} failed (exit {result.returncode})"
        )
    return result


def inside(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def name(value):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,61}", value):
        raise Blocked("Invalid application or store name")
    return value


def no_secrets(model):
    for service, spec in model.get("services", {}).items():
        for key, value in (spec.get("environment") or {}).items():
            if re.search(
                r"PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|CREDENTIAL", key, re.I
            ):
                if value and not re.fullmatch(
                    r"\$\{[A-Z_][A-Z0-9_]*(?::\?[^}]*)?\}", str(value)
                ):
                    raise Blocked(f"Inline secret in service {service}, variable {key}")
            if value and re.search(
                r"://[^/:\s]+:[^/@\s]+@|\b(?:ghp_|sk-proj-|sk-ant-)", str(value)
            ):
                raise Blocked(f"Inline credential in service {service}, variable {key}")
        build = spec.get("build") or {}
        if isinstance(build, dict):
            for key, value in (build.get("args") or {}).items():
                if value and re.search(
                    r"PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL", key, re.I
                ):
                    raise Blocked(f"Secret build argument in service {service}: {key}")


class Store:
    def __init__(self, root, app):
        self.root = Path(root) / name(app)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextlib.contextmanager
    def lock(self):
        # All apps share pool capacity and registry/proxy infrastructure.
        with open(self.root.parent / "host.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self, path, default=None):
        try:
            return read_json(self.root / path)
        except FileNotFoundError:
            return default

    def write(self, path, value):
        write_json(self.root / path, value)


def protected(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise Blocked(f"Protected file must be a regular file with mode 0600: {path}")
    return path


def env_hash(paths):
    return fingerprint(
        {str(p): hashlib.sha256(protected(p).read_bytes()).hexdigest() for p in paths}
    )


def check_plan(plan, approvals, observed, now=None):
    if (time.time() if now is None else now) > plan["expires"]:
        raise Blocked("Plan expired; create a new plan")
    missing = set(plan["approvals"]) - set(approvals)
    if missing:
        raise Blocked("Plan needs explicit approval: " + ", ".join(sorted(missing)))
    if plan["observed"] != observed:
        raise Blocked(
            "Source, configuration, environment, or live resources changed; replan"
        )
