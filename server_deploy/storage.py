import re
from pathlib import Path

from .core import Blocked, inside, run


class Storage:
    """ZFS operations are constrained to declared per-application datasets."""

    def __init__(self, app, storage_class, runner=run):
        self.app = app
        self.storage_class = storage_class
        self.run = runner
        self.app_dataset = f"{storage_class.upper()}/app-{app}"
        self.app_root = (
            f"/HDD/docker-data/apps/{app}"
            if storage_class == "hdd"
            else f"/SSD/apps/{app}"
        )
        self.db_dataset = f"SSD/database-{app}"
        self.db_root = f"/SSD/databases/{app}"

    def properties(self, dataset):
        result = self.run(
            [
                "zfs",
                "list",
                "-Hp",
                "-o",
                "name,mountpoint,mounted,canmount,used,available",
                dataset,
            ],
            check=False,
        )
        if result.returncode:
            return None
        fields = result.stdout.strip().split("\t")
        if len(fields) != 6:
            raise Blocked("Invalid ZFS inspection result")
        return dict(
            zip(
                ("name", "mountpoint", "mounted", "canmount", "used", "available"),
                fields,
            )
        )

    def observe(self, database):
        result = {}
        datasets = [self.app_dataset] + ([self.db_dataset] if database else [])
        for dataset in datasets:
            props = self.properties(dataset)
            result[dataset] = (
                None
                if props is None
                else {
                    key: props[key]
                    for key in ("name", "mountpoint", "mounted", "canmount")
                }
            )
        return result

    def capacity(self, dataset, required=0):
        pool = self.properties(dataset.split("/")[0])
        if not pool:
            raise Blocked("Required ZFS pool is unavailable")
        available, used = int(pool["available"]), int(pool["used"])
        if available - required * 1.2 < (available + used) * 0.10:
            raise Blocked("Operation would breach the 10% pool free-space floor")

    def ensure(self, database=False):
        pairs = [(self.app_dataset, self.app_root)]
        if database:
            pairs.append((self.db_dataset, self.db_root))
        for dataset, mountpoint in pairs:
            self.capacity(dataset)
            existing = self.properties(dataset)
            path = Path(mountpoint)
            if existing and existing["mountpoint"] != mountpoint:
                raise Blocked(
                    "Dataset mountpoint conflicts with declared application storage"
                )
            if not existing or existing["mounted"] != "yes":
                if path.is_symlink() or (path.exists() and any(path.iterdir())):
                    raise Blocked("Refusing to mount over a populated directory")
                if not existing:
                    self.run(
                        [
                            "zfs",
                            "create",
                            "-o",
                            "canmount=noauto",
                            "-o",
                            f"mountpoint={mountpoint}",
                            dataset,
                        ]
                    )
                self.run(["zfs", "mount", dataset])
            self.run(["zfs", "set", "canmount=on", dataset])
            observed = self.properties(dataset)
            if (
                not observed
                or observed["mounted"] != "yes"
                or observed["canmount"] != "on"
            ):
                raise Blocked("Dataset boot-mount verification failed")

    def destination(self, entry):
        root = self.db_root if entry["kind"] == "database" else self.app_root
        path = str(Path(root) / entry["service"] / entry["name"])
        if not inside(path, root):
            raise Blocked("Storage destination escapes application dataset")
        return path

    def snapshot(self, operation, database=False):
        if not re.fullmatch(r"[a-z0-9-]+", operation):
            raise Blocked("Invalid operation ID")
        snapshots = []
        for dataset in [self.app_dataset] + ([self.db_dataset] if database else []):
            snapshot = f"{dataset}@actions-{operation}"
            existing = self.run(
                ["zfs", "list", "-H", "-t", "snapshot", snapshot], check=False
            )
            if existing.returncode:
                self.run(["zfs", "snapshot", snapshot])
            holds = self.run(["zfs", "holds", "-H", snapshot]).stdout
            if "server-deploy" not in holds:
                self.run(["zfs", "hold", "server-deploy", snapshot])
            snapshots.append(snapshot)
        return snapshots

    def validate_copy(self, source, destination):
        allowed = [self.app_root, self.db_root]
        if not any(inside(destination, root) for root in allowed):
            raise Blocked("Migration destination is outside application storage")
        if Path(source).is_symlink() or Path(destination).is_symlink():
            raise Blocked("Migration paths cannot be symlinks")
        if inside(source, destination) or inside(destination, source):
            raise Blocked("Migration paths overlap")

    def copy(self, source, destination, verify=False):
        self.validate_copy(source, destination)
        Path(destination).mkdir(parents=True, exist_ok=True)
        flags = [
            "--archive",
            "--hard-links",
            "--acls",
            "--xattrs",
            "--numeric-ids",
            "--delete",
            "--checksum",
        ]
        if verify:
            flags += ["--dry-run", "--itemize-changes"]
        result = self.run(
            [
                "rsync",
                *flags,
                "--",
                source.rstrip("/") + "/",
                destination.rstrip("/") + "/",
            ]
        )
        if verify and result.stdout.strip():
            raise Blocked("Migration checksum/metadata verification failed")
