import tempfile
import unittest
from pathlib import Path

from server_deploy.core import Blocked, Store, check_plan, env_hash, no_secrets


class CoreTests(unittest.TestCase):
    def test_plan_approval_expiry_and_drift(self):
        plan = {
            "expires": 200,
            "approvals": ["deployment", "data-migration"],
            "observed": "a",
        }
        check_plan(plan, ["deployment", "data-migration"], "a", now=100)
        for approvals, observed, now in [
            (["deployment"], "a", 100),
            (["deployment", "data-migration"], "b", 100),
            (["deployment", "data-migration"], "a", 201),
        ]:
            with self.assertRaises(Blocked):
                check_plan(plan, approvals, observed, now=now)

    def test_secret_values_are_never_in_errors(self):
        for key, value in [
            ("API_KEY", "sensitive-value"),
            ("PASSWORD", "${PASSWORD:-sensitive-value}"),
            ("DATABASE_URL", "postgres://user:sensitive-value@db/a"),
        ]:
            with self.assertRaises(Blocked) as error:
                no_secrets({"services": {"web": {"environment": {key: value}}}})
            self.assertNotIn("sensitive-value", str(error.exception))
        no_secrets({"services": {"web": {"environment": {"API_KEY": "${API_KEY}"}}}})

    def test_state_atomic_private_and_environment_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp, "example")
            store.write("current.json", {"release": "first"})
            store.write("current.json", {"release": "second"})
            self.assertEqual(store.read("current.json")["release"], "second")
            self.assertEqual(
                (store.root / "current.json").stat().st_mode & 0o777, 0o600
            )
            env = Path(tmp) / "prod.env"
            env.write_text("KEY=first")
            env.chmod(0o600)
            before = env_hash([env])
            env.write_text("KEY=second")
            self.assertNotEqual(before, env_hash([env]))
            env.chmod(0o644)
            with self.assertRaises(Blocked):
                env_hash([env])

    def test_application_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Blocked):
                Store(tmp, "../../other")
