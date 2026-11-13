import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from server_deploy.core import Blocked, Store
from server_deploy.engine import Engine


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine.__new__(Engine)
        self.engine.store = Store(self.temp.name, "example")
        self.engine.run = Mock()
        self.engine.proxy = Mock()
        self.engine.activate = Mock()
        self.engine.project = Mock()
        self.engine.project.name = "example"
        self.engine.project.identity.return_value = "identity"
        self.engine.project.database = False
        self.engine.project.env_files = {}
        self.engine.observe = Mock(return_value=("observed", {}, {}))
        self.engine.baseline = Mock(return_value=None)

    def plan(self, operation="deploy", previous=None):
        plan = {
            "id": "one",
            "expires": time.time() + 100,
            "observed": "observed",
            "approvals": ["deployment", "downtime"],
            "operation": operation,
            "previous": previous,
            "migrations": [],
            "routes": [],
            "prior_routes": {},
            "source_identity": "identity",
            "target": "old",
        }
        self.engine.store.write("plans/one.json", plan)
        return plan

    def test_preparation_failure_leaves_healthy_previous_container_running(self):
        previous = {"id": "old", "compose": "old.json"}
        self.plan(previous=previous)
        self.engine.project.storage.ensure.side_effect = Blocked("capacity")
        with self.assertRaisesRegex(Blocked, "exit 7"):
            self.engine.apply("one", ["deployment", "downtime"])
        self.engine.activate.assert_not_called()
        self.assertEqual(self.engine.store.read("journal.json")["status"], "restored")

    def test_application_failure_restores_exact_prior_artifact(self):
        old = {"id": "old", "compose": "/protected/releases/old/compose.json"}
        new = {"id": "new", "compose": "/protected/releases/new/compose.json"}
        self.plan("rollback", old)
        self.engine.store.write("releases/old/release.json", new)
        self.engine.activate.side_effect = [Blocked("unhealthy"), None]
        with self.assertRaisesRegex(Blocked, "exit 7"):
            self.engine.apply("one", ["deployment", "downtime"])
        self.assertEqual(self.engine.activate.call_args_list[0].args[0], new)
        self.assertEqual(self.engine.activate.call_args_list[1].args[0], old)
        self.assertEqual(self.engine.store.read("current.json"), old)

    def test_failed_compensation_requires_recovery(self):
        old = {"id": "old", "compose": "old.json"}
        self.plan("rollback", old)
        self.engine.store.write("releases/old/release.json", {"id": "new"})
        self.engine.activate.side_effect = Blocked("failure")
        with self.assertRaisesRegex(Blocked, "exit 8"):
            self.engine.apply("one", ["deployment", "downtime"])
        self.assertEqual(
            self.engine.store.read("journal.json")["status"], "recovery-required"
        )

    def test_plan_drift_prevents_all_mutations(self):
        self.plan()
        self.engine.observe.return_value = ("changed", {}, {})
        with self.assertRaises(Blocked):
            self.engine.apply("one", ["deployment", "downtime"])
        self.engine.run.assert_not_called()
        self.engine.project.storage.ensure.assert_not_called()

    def test_recovery_uses_journal_previous_artifact(self):
        plan = self.plan("recover")
        old = {"id": "old", "compose": "old.json"}
        plan["prior_journal"] = {
            "id": "interrupted",
            "previous": old,
            "production_stopped": True,
        }
        self.engine.store.write("plans/one.json", plan)
        self.assertEqual(
            self.engine.apply("one", ["deployment", "downtime"])["status"], "restored"
        )
        self.engine.activate.assert_called_once_with(old)

    def test_environment_is_immutable_per_release(self):
        directory = Path(self.temp.name, "release")
        directory.mkdir()
        env = Path(self.temp.name, "production.env")
        env.write_text("TOKEN=old-secret\n")
        model = {"services": {"web": {"env_file": [{"path": str(env)}]}}}
        frozen, hashes = self.engine.freeze_environment(model, directory)
        env.write_text("TOKEN=new-secret\n")
        saved = Path(frozen["services"]["web"]["env_file"][0]["path"])
        self.assertEqual(saved.read_text(), "TOKEN=old-secret\n")
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    def test_noop_does_not_build_or_change_resources(self):
        plan = self.plan(previous={"id": "old"})
        plan["noop"] = True
        self.engine.store.write("plans/one.json", plan)
        result = self.engine.apply("one", ["deployment", "downtime"])
        self.assertEqual(result["status"], "unchanged")
        self.engine.run.assert_not_called()
        self.engine.project.storage.ensure.assert_not_called()
