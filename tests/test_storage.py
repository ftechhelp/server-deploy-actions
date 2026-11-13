import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from server_deploy.core import Blocked
from server_deploy.storage import Storage


class StorageTests(unittest.TestCase):
    def test_cannot_mount_over_populated_unmounted_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Storage("example", "hdd", Mock())
            storage.app_root = tmp
            Path(tmp, "live-data").write_text("preserve")
            storage.capacity = Mock()
            storage.properties = Mock(return_value={"mountpoint": tmp, "mounted": "no"})
            with self.assertRaises(Blocked):
                storage.ensure()
            storage.run.assert_not_called()

    def test_boot_mount_repair_is_verified(self):
        storage = Storage("example", "hdd", Mock())
        storage.capacity = Mock()
        storage.properties = Mock(
            side_effect=[
                {
                    "mountpoint": storage.app_root,
                    "mounted": "yes",
                    "canmount": "noauto",
                },
                {"mountpoint": storage.app_root, "mounted": "yes", "canmount": "on"},
            ]
        )
        storage.ensure()
        storage.run.assert_called_once_with(
            ["zfs", "set", "canmount=on", "HDD/app-example"]
        )

    def test_capacity_reserves_overhead_and_ten_percent(self):
        storage = Storage("example", "hdd", Mock())
        storage.properties = Mock(return_value={"available": "200", "used": "800"})
        storage.capacity("HDD/app-example", 50)
        with self.assertRaises(Blocked):
            storage.capacity("HDD/app-example", 100)

    def test_copy_cannot_escape_or_overlap(self):
        storage = Storage("example", "hdd", Mock())
        for source, destination in [
            ("/tmp/source", "/etc/other"),
            (storage.app_root, storage.app_root + "/child"),
        ]:
            with self.assertRaises(Blocked):
                storage.validate_copy(source, destination)

    def test_copy_preserves_metadata_and_verifies_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock(return_value=SimpleNamespace(stdout=""))
            storage = Storage("example", "hdd", runner)
            storage.app_root = str(Path(tmp, "app"))
            source, destination = (
                str(Path(tmp, "source")),
                str(Path(storage.app_root, "data")),
            )
            Path(source).mkdir()
            storage.copy(source, destination, verify=True)
            args = runner.call_args.args[0]
            for flag in (
                "--checksum",
                "--acls",
                "--xattrs",
                "--numeric-ids",
                "--dry-run",
                "--delete",
            ):
                self.assertIn(flag, args)
            runner.return_value.stdout = ">f.s....... data.db"
            with self.assertRaises(Blocked):
                storage.copy(source, destination, verify=True)
