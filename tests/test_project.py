import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from server_deploy.core import Blocked
from server_deploy.project import Project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "deploy").mkdir()
        self.manifest = {
            "schema": 1,
            "app": "example",
            "primary": "web",
            "port": 8080,
            "stores": [
                {
                    "name": "data",
                    "service": "web",
                    "target": "/data",
                    "kind": "database",
                }
            ],
        }
        (self.root / "deploy/app.json").write_text(json.dumps(self.manifest))
        self.env = self.root / "production.env"
        self.env.write_text("TOKEN=protected")
        self.env.chmod(0o600)
        self.model = {
            "services": {
                "web": {
                    "build": {"context": str(self.root)},
                    "env_file": [".env"],
                    "healthcheck": {"test": ["CMD", "true"]},
                    "ports": ["8080:8080"],
                    "volumes": [
                        {"type": "volume", "source": "data", "target": "/data"}
                    ],
                }
            }
        }
        self.config = {
            "source_roots": [str(self.root)],
            "apps": {
                "example": {
                    "repository": "owner/example",
                    "env_files": {"web": str(self.env)},
                    "storage": "hdd",
                }
            },
        }
        self.current_sha = "a" * 40

    def runner(self, args, **kwargs):
        if args[0] == "git":
            if "rev-parse" in args:
                output = self.current_sha
            elif "remote" in args:
                output = "https://github.com/owner/example.git"
            else:
                output = ""
        else:
            output = json.dumps(self.model)
        return SimpleNamespace(stdout=output, returncode=0)

    def project(self):
        return Project(self.config, "example", self.root, runner=self.runner)

    def test_database_placement_networks_and_production_identity(self):
        project = self.project()
        model = project.desired({"web": "docker.io/owner/example@sha256:" + "a" * 64})
        web = model["services"]["web"]
        self.assertEqual(web["container_name"], "example")
        self.assertEqual(web["restart"], "unless-stopped")
        self.assertNotIn("ports", web)
        self.assertNotIn("build", web)
        self.assertEqual(web["env_file"][0]["path"], str(self.env))
        self.assertEqual(web["volumes"][0]["source"], "/SSD/databases/example/web/data")
        self.assertEqual(set(web["networks"]), {"private", "app-local"})

    def test_unregistered_repository_is_blocked(self):
        self.config["apps"]["example"]["repository"] = "someone/else"
        with self.assertRaises(Blocked):
            self.project()

    def test_compose_list_environment_is_normalized(self):
        self.model["services"]["web"]["environment"] = ["TZ=UTC", "OPTIONAL"]
        self.model["services"]["web"]["labels"] = ["example=label"]
        model = self.project().desired({"web": "image"})
        self.assertEqual(
            model["services"]["web"]["environment"], {"TZ": "UTC", "OPTIONAL": None}
        )
        self.assertEqual(model["services"]["web"]["labels"]["example"], "label")

    def test_undeclared_mount_cannot_enter_production(self):
        self.model["services"]["web"]["volumes"].append(
            {"type": "bind", "source": "/", "target": "/host"}
        )
        with self.assertRaises(Blocked):
            self.project().desired({"web": "image"})

    def test_privileged_runtime_is_blocked(self):
        self.model["services"]["web"]["privileged"] = True
        with self.assertRaises(Blocked):
            self.project().desired({"web": "image"})

    def test_source_switch_during_build_is_detected(self):
        project = self.project()
        self.current_sha = "b" * 40
        with self.assertRaises(Blocked):
            project.clean()

    def test_environment_change_alters_plan_identity(self):
        project = self.project()
        before = project.identity()
        self.env.write_text("TOKEN=changed")
        self.assertNotEqual(before, project.identity())
