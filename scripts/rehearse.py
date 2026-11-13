#!/usr/bin/env python3
"""Opt-in rehearsal using a new file-backed pool and disposable Docker resources.

Run from the repository as root with SERVER_DEPLOY_REHEARSE=yes. No real HDD/SSD
dataset names are passed to ZFS. Image publication is stubbed; activation, copying,
ZFS lifecycle, adoption artifacts, health, and rollback use the real host clients.
"""

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server_deploy.core import Blocked, Store, run as engine_run
from server_deploy.engine import Engine
from server_deploy.project import Project


def run(args, **kwargs):
    check = kwargs.pop("check", True)
    result = engine_run(args, check=False, **kwargs)
    if check and result.returncode:
        raise RuntimeError(
            f"Disposable rehearsal command failed: {args}: {result.stderr}"
        )
    return result


if os.geteuid() != 0 or os.environ.get("SERVER_DEPLOY_REHEARSE") != "yes":
    sys.exit("Requires root and SERVER_DEPLOY_REHEARSE=yes")
identifier = "sdrehearsal" + uuid.uuid4().hex[:10]
pool_created = False
with tempfile.TemporaryDirectory(prefix="server-deploy-rehearsal-") as tmp:
    root = Path(tmp)
    try:
        vdev = root / "pool.img"
        with open(vdev, "wb") as image:
            image.truncate(256 * 1024 * 1024)
        run(
            [
                "zpool",
                "create",
                "-o",
                "cachefile=none",
                "-O",
                f"mountpoint={root / 'pool'}",
                identifier,
                str(vdev),
            ]
        )
        pool_created = True
        run(["docker", "pull", "python:3.12-slim"])
        digest = json.loads(
            run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "python:3.12-slim",
                    "--format",
                    "{{json .RepoDigests}}",
                ]
            ).stdout
        )[0]
        source = root / "source"
        source.mkdir()
        original_data = root / "original"
        original_data.mkdir()
        (original_data / "preserve.txt").write_text("original data")
        model = {
            "services": {
                "web": {
                    "image": digest,
                    "command": ["python", "-m", "http.server", "8080"],
                    "healthcheck": {
                        "test": [
                            "CMD",
                            "python",
                            "-c",
                            "import pathlib; assert pathlib.Path('/data/preserve.txt').exists()",
                        ],
                        "interval": "1s",
                        "timeout": "2s",
                        "retries": 5,
                    },
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(original_data),
                            "target": "/data",
                        }
                    ],
                }
            }
        }
        (source / "compose.json").write_text(json.dumps(model))
        (source / "deploy").mkdir()
        (source / "deploy/app.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "app": identifier,
                    "compose": "compose.json",
                    "primary": "web",
                    "port": 8080,
                    "stores": [
                        {
                            "service": "web",
                            "name": "data",
                            "target": "/data",
                            "kind": "database",
                        }
                    ],
                }
            )
        )
        run(["git", "init", "-b", "main", str(source)])
        run(["git", "-C", str(source), "config", "user.name", "Rehearsal"])
        run(
            [
                "git",
                "-C",
                str(source),
                "config",
                "user.email",
                "rehearsal@example.invalid",
            ]
        )
        run(
            [
                "git",
                "-C",
                str(source),
                "remote",
                "add",
                "origin",
                "https://github.com/ftechhelp/server-deploy-actions.git",
            ]
        )
        run(["git", "-C", str(source), "add", "."])
        run(["git", "-C", str(source), "commit", "-m", "Disposable fixture"])
        run(["docker", "network", "create", identifier + "-old"])
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                identifier,
                "--network",
                identifier + "-old",
                "--label",
                f"com.docker.compose.project={identifier}",
                "--label",
                "com.docker.compose.service=web",
                "--health-cmd",
                "python -c \"import pathlib; assert pathlib.Path('/data/preserve.txt').exists()\"",
                "--health-interval",
                "1s",
                "-v",
                f"{original_data}:/data",
                digest,
                "python",
                "-m",
                "http.server",
                "8080",
            ]
        )
        config = {
            "source_roots": [tmp],
            "state_root": str(root / "state"),
            "stabilization_seconds": 0,
            "apps": {
                identifier: {
                    "repository": "ftechhelp/server-deploy-actions",
                    "storage": "hdd",
                }
            },
        }

        def make_engine():
            instance = Engine.__new__(Engine)
            instance.config, instance.run = config, run
            instance.project = Project(config, identifier, source, runner=run)
            fixture_storage = instance.project.storage
            fixture_storage.app_dataset = identifier + "/app"
            fixture_storage.app_root = str(root / "pool/app")
            fixture_storage.db_dataset = identifier + "/db"
            fixture_storage.db_root = str(root / "pool/db")
            instance.store = Store(config["state_root"], identifier)
            instance.proxy = Mock()
            instance.proxy.observe.return_value = {}
            instance.publish = Mock(return_value={"web": digest})
            return instance

        engine = make_engine()
        storage = engine.project.storage
        plan = engine.plan()
        assert set(plan["approvals"]) == {
            "deployment",
            "downtime",
            "adoption",
            "data-migration",
        }
        result = engine.apply(plan["id"], plan["approvals"])
        assert result["status"] == "succeeded", result
        destination = Path(storage.db_root) / "web/data/preserve.txt"
        assert destination.read_text() == "original data"
        assert (original_data / "preserve.txt").read_text() == "original data"
        assert storage.properties(storage.db_dataset)["canmount"] == "on"
        journal = engine.store.read("journal.json")
        baseline = journal["previous"]
        plan = engine.plan("rollback", baseline["id"])
        assert engine.apply(plan["id"], plan["approvals"])["status"] == "succeeded"
        actual = json.loads(run(["docker", "inspect", identifier]).stdout)[0]
        assert actual["Mounts"][0]["Source"] == str(original_data)
        assert destination.read_text() == "original data"
        print(
            "PASS: real ZFS creation, boot mounts, migration, retained source, held snapshots, health, and exact-artifact rollback"
        )
        destination.parent.rename(destination.parent.with_name("data.retained-success"))
        plan = engine.plan()
        engine.activate = Mock(side_effect=KeyboardInterrupt)
        try:
            engine.apply(plan["id"], plan["approvals"])
            raise AssertionError("Interruption was not injected")
        except KeyboardInterrupt:
            pass
        assert engine.store.read("journal.json")["status"] == "activating"
        engine = make_engine()  # All recovery information must come from durable state.
        plan = engine.plan("recover")
        assert engine.apply(plan["id"], plan["approvals"])["status"] == "restored"
        assert (original_data / "preserve.txt").read_text() == "original data"
        print("PASS: interrupted-operation recovery from a fresh engine instance")
        engine = make_engine()
        plan = engine.plan()
        desired = engine.project.desired

        def failing_desired(images):
            bad = desired(images)
            bad["services"]["web"]["command"] = [
                "python",
                "-c",
                "from pathlib import Path; Path('/data/failed-write.txt').write_text('retain me'); raise SystemExit(1)",
            ]
            return bad

        engine.project.desired = failing_desired
        try:
            engine.apply(plan["id"], plan["approvals"])
            raise AssertionError("Unhealthy fixture unexpectedly deployed")
        except Blocked as error:
            assert "exit 7" in str(error), error
        failed = destination.parent.with_name("data.failed-" + plan["id"])
        assert (failed / "failed-write.txt").read_text() == "retain me"
        assert not (original_data / "failed-write.txt").exists()
        assert (original_data / "preserve.txt").read_text() == "original data"
        print(
            "PASS: unhealthy deployment restored previous app and retained failed-release writes"
        )
        engine = make_engine()
        plan = engine.plan()
        assert engine.apply(plan["id"], plan["approvals"])["status"] == "succeeded"
        plan = engine.plan("accept")
        assert engine.apply(plan["id"], plan["approvals"])["status"] == "accepted"
        assert (original_data / "preserve.txt").exists()
        assert (failed / "failed-write.txt").exists()
        print("PASS: safe migration retry and acceptance preserve all recovery data")
    finally:
        run(["docker", "rm", "-f", identifier], check=False)
        retained = run(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=com.docker.compose.project={identifier}",
            ]
        ).stdout.split()
        if retained:
            run(["docker", "rm", "-f", *retained], check=False)
        run(["docker", "network", "rm", identifier + "-local"], check=False)
        run(["docker", "network", "rm", identifier + "-old"], check=False)
        if pool_created:
            run(["zpool", "destroy", identifier])
