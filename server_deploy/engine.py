import copy
import datetime
import json
import os
import re
import time
import uuid
from pathlib import Path

from .core import (
    Blocked,
    Store,
    check_plan,
    fingerprint,
    inside,
    read_json,
    run,
    write_json,
)
from .project import Project
from .proxy import Proxy


class Engine:
    def __init__(self, config, app, source, manifest="deploy/app.json", runner=run):
        self.config = config
        self.run = runner
        self.project = Project(config, app, source, manifest, runner)
        self.store = Store(config["state_root"], app)
        self.proxy = Proxy(config["npm"]["api_url"], config["npm"]["login_file"])

    def containers(self):
        ids = self.run(["docker", "ps", "-aq"]).stdout.split()
        all_containers = (
            json.loads(self.run(["docker", "inspect", *ids]).stdout) if ids else []
        )
        selected = {}
        for container in all_containers:
            container_name = container["Name"].lstrip("/")
            labels = container["Config"].get("Labels") or {}
            if labels.get("com.docker.compose.project") == self.project.name:
                service = labels.get("com.docker.compose.service")
                if service:
                    selected[service] = container
            for service in self.project.model["services"]:
                if container_name == self.project.container_name(service):
                    labels = container["Config"].get("Labels") or {}
                    owner = labels.get("io.server-deploy.app") or labels.get(
                        "io.murphy-deploy.app"
                    )
                    compose_owner = labels.get("com.docker.compose.project")
                    if owner and owner != self.project.name:
                        raise Blocked(
                            "Container is conclusively owned by another application"
                        )
                    if not owner and compose_owner != self.project.name:
                        raise Blocked(
                            "Container ownership is ambiguous; supply explicit host adoption policy"
                        )
                    selected[service] = container
        return selected, all_containers

    def observe(self):
        selected, _ = self.containers()
        routes = self.proxy.observe(self.project.routes())
        observed = {
            "source": self.project.identity(),
            "containers": {
                s: {
                    "id": c["Id"],
                    "image": c["Image"],
                    "running": c["State"]["Running"],
                    "mounts": c["Mounts"],
                }
                for s, c in selected.items()
            },
            "storage": self.project.storage.observe(self.project.database),
            "routes": routes,
            "current": self.store.read("current.json"),
        }
        return fingerprint(observed), selected, routes

    def migrations(self, selected, seed=False):
        moves = []
        _, all_containers = self.containers()
        for store in self.project.stores:
            destination = self.project.storage.destination(store)
            container = selected.get(store["service"])
            if not container and seed:
                dev_name = self.project.name + "-dev"
                container = next(
                    (c for c in all_containers if c["Name"].lstrip("/") == dev_name),
                    None,
                )
            mounts = container["Mounts"] if container else []
            old = next((m for m in mounts if m["Destination"] == store["target"]), None)
            if old and old["Source"] != destination:
                path = Path(destination)
                if path.exists() and any(path.iterdir()):
                    raise Blocked(
                        "Migration destination contains data; refusing to merge or overwrite"
                    )
                self.project.storage.validate_copy(old["Source"], destination)
                writers = []
                for other in all_containers:
                    for mount in other["Mounts"]:
                        overlaps = inside(mount["Source"], old["Source"]) or inside(
                            old["Source"], mount["Source"]
                        )
                        if mount.get("RW") and overlaps and other["State"]["Running"]:
                            if (
                                other["Id"] not in {c["Id"] for c in selected.values()}
                                and other is not container
                            ):
                                raise Blocked(
                                    "A different application is writing to migration source"
                                )
                            writers.append(other["Id"])
                size = int(self.run(["du", "-sb", old["Source"]]).stdout.split()[0])
                dataset = (
                    self.project.storage.db_dataset
                    if store["kind"] == "database"
                    else self.project.storage.app_dataset
                )
                self.project.storage.capacity(dataset, size)
                moves.append(
                    {
                        "source": old["Source"],
                        "destination": destination,
                        "writers": sorted(set(writers)),
                        "bytes": size,
                        "development": not bool(selected.get(store["service"])),
                    }
                )
        return moves

    def plan(self, operation="deploy", target=None, seed=False):
        if operation not in (
            "deploy",
            "reconcile",
            "recreate",
            "rollback",
            "recover",
            "accept",
        ):
            raise Blocked("Unknown deployment operation")
        self.project.clean()
        journal = self.store.read("journal.json")
        pending = journal and journal["status"] not in (
            "succeeded",
            "restored",
            "accepted",
        )
        if pending and operation != "recover":
            raise Blocked(
                "An interrupted operation needs recovery before another deployment"
            )
        observed, selected, routes = self.observe()
        current = self.store.read("current.json")
        if operation in ("rollback", "recreate") and not current:
            raise Blocked("No adopted application release exists")
        if operation == "rollback":
            if not target or not re.fullmatch(r"[a-z0-9-]+", target):
                raise Blocked("A valid saved release ID is required")
            if not self.store.read(f"releases/{target}/release.json"):
                raise Blocked("Rollback release does not exist")
        if operation in ("recover", "accept") and not journal:
            raise Blocked("There is no operation to recover or accept")
        if operation == "accept" and journal["status"] != "succeeded":
            raise Blocked("Only a successfully verified migration can be accepted")
        moves = (
            self.migrations(selected, seed)
            if operation in ("deploy", "reconcile")
            else []
        )
        approvals = [] if operation == "accept" else ["downtime"]
        if operation in ("deploy", "reconcile"):
            approvals.append("deployment")
        if moves or seed:
            approvals.append("data-migration")
        if selected and not current:
            approvals.append("adoption")
        if operation == "accept":
            approvals = ["data-migration"]
        noop = (
            operation == "deploy"
            and current
            and current.get("identity") == self.project.identity()
            and not self.status()["findings"]
        )
        if noop:
            approvals = []
        operation_id = (
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
            + "-"
            + uuid.uuid4().hex[:12]
        )
        plan = {
            "schema": 1,
            "id": operation_id,
            "app": self.project.name,
            "operation": operation,
            "source": str(self.project.source),
            "revision": self.project.sha,
            "target": target,
            "created": time.time(),
            "source_identity": self.project.identity(),
            "expires": time.time() + 900,
            "observed": observed,
            "approvals": sorted(set(approvals)),
            "migrations": moves,
            "routes": self.project.routes(),
            "prior_routes": routes,
            "previous": current,
            "prior_journal": journal,
            "actions": [
                "validate-source-and-environment",
                "ensure-storage-and-boot-mounts",
                "build-test-publish-and-pull-digests",
                "quiesce-and-verify-data-migrations",
                "retain-held-zfs-recovery-points",
                "activate-compose-and-stabilize-health",
                "ensure-proxy-and-tls",
                "record-release",
            ],
            "data_rollback": False,
        }
        plan["noop"] = bool(noop)
        if noop:
            plan["actions"] = []
        if operation not in ("deploy", "reconcile"):
            plan["actions"] = [operation, "verify-health", "record-operation"]
        self.store.write(f"plans/{operation_id}.json", plan)
        return self.public_plan(plan)

    @staticmethod
    def public_plan(plan):
        return {
            key: plan[key]
            for key in (
                "id",
                "app",
                "operation",
                "revision",
                "target",
                "expires",
                "approvals",
                "migrations",
                "actions",
                "data_rollback",
            )
        }

    def compose(self, path, *args, timeout=600):
        return self.run(
            [
                "docker",
                "compose",
                "--project-name",
                self.project.name,
                "-f",
                str(path),
                *args,
            ],
            timeout=timeout,
        )

    def quiescent(self, moves):
        if not moves:
            return
        _, containers = self.containers()
        for container in containers:
            if not container["State"]["Running"]:
                continue
            for mount in container["Mounts"]:
                if not mount.get("RW") or not mount.get("Source"):
                    continue
                for move in moves:
                    for path in (move["source"], move["destination"]):
                        if inside(mount["Source"], path) or inside(
                            path, mount["Source"]
                        ):
                            raise Blocked("A writer is still active on migration data")

    def baseline(self, selected, operation):
        if not selected:
            return None
        release_id = "adopt-" + operation
        directory = self.store.root / "releases" / release_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        model = {
            "name": self.project.name,
            "services": {},
            "networks": {},
            "volumes": {},
        }
        for service, container in selected.items():
            cfg, host = container["Config"], container["HostConfig"]
            # Refuse unsupported runtime modes rather than silently weakening rollback.
            if host.get("NetworkMode") in ("host", "none") or host.get("Privileged"):
                raise Blocked(
                    "Adoption of host-network or privileged containers requires an explicit manifest"
                )
            env_file = directory / f"{service}.env"
            values = cfg.get("Env") or []
            if any("\n" in v or "\r" in v for v in values):
                raise Blocked(
                    "Cannot safely capture multiline runtime environment during adoption"
                )
            fd = os.open(env_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write("\n".join(values) + "\n")
            spec = {
                "image": container["Image"],
                "container_name": container["Name"].lstrip("/"),
                "env_file": [{"path": str(env_file), "format": "raw"}],
                "restart": host["RestartPolicy"]["Name"] or "no",
                "networks": {},
                "volumes": [],
                "command": cfg.get("Cmd"),
                "entrypoint": cfg.get("Entrypoint"),
            }
            for key, field in (
                ("working_dir", "WorkingDir"),
                ("user", "User"),
                ("hostname", "Hostname"),
            ):
                if cfg.get(field):
                    spec[key] = cfg[field]
            labels = cfg.get("Labels") or {}
            spec["labels"] = {
                k: v
                for k, v in labels.items()
                if not k.startswith("com.docker.compose.")
            }
            health = cfg.get("Healthcheck")
            if health:
                spec["healthcheck"] = {
                    k.lower().replace("startperiod", "start_period"): (
                        f"{v}ns" if k != "Test" and k != "Retries" else v
                    )
                    for k, v in health.items()
                    if k in ("Test", "Interval", "Timeout", "StartPeriod", "Retries")
                }
            for source_key, target_key in (
                ("Memory", "mem_limit"),
                ("MemorySwap", "memswap_limit"),
                ("CpuShares", "cpu_shares"),
                ("CpuQuota", "cpu_quota"),
                ("CpuPeriod", "cpu_period"),
                ("CpusetCpus", "cpuset"),
                ("ShmSize", "shm_size"),
                ("ReadonlyRootfs", "read_only"),
                ("CapAdd", "cap_add"),
                ("CapDrop", "cap_drop"),
                ("SecurityOpt", "security_opt"),
                ("Dns", "dns"),
                ("ExtraHosts", "extra_hosts"),
            ):
                if host.get(source_key):
                    spec[target_key] = host[source_key]
            if host.get("Tmpfs"):
                spec["tmpfs"] = [
                    f"{path}:{options}" for path, options in host["Tmpfs"].items()
                ]
            if host.get("LogConfig", {}).get("Type"):
                spec["logging"] = {
                    "driver": host["LogConfig"]["Type"],
                    "options": host["LogConfig"].get("Config") or {},
                }
            for network, info in container["NetworkSettings"]["Networks"].items():
                model["networks"][network] = {"external": True, "name": network}
                spec["networks"][network] = {"aliases": info.get("Aliases") or []}
            for mount in container["Mounts"]:
                source = (
                    mount.get("Name") if mount["Type"] == "volume" else mount["Source"]
                )
                spec["volumes"].append(
                    {
                        "type": mount["Type"],
                        "source": source,
                        "target": mount["Destination"],
                        "read_only": not mount.get("RW", True),
                    }
                )
                if mount["Type"] == "volume":
                    model["volumes"][source] = {"external": True, "name": source}
            for port, bindings in (host.get("PortBindings") or {}).items():
                number, protocol = port.split("/")
                for binding in bindings or []:
                    spec.setdefault("ports", []).append(
                        {
                            "target": int(number),
                            "published": binding["HostPort"],
                            "host_ip": binding.get("HostIp", ""),
                            "protocol": protocol,
                        }
                    )
            model["services"][service] = spec
        path = directory / "compose.json"
        write_json(path, model)
        self.compose(path, "config", "--quiet")
        release = {
            "id": release_id,
            "compose": str(path),
            "images": {s: c["Image"] for s, c in selected.items()},
            "created": time.time(),
            "revision": "adopted",
            "environment": {},
        }
        self.store.write(f"releases/{release_id}/release.json", release)
        return release

    def freeze_environment(self, model, directory):
        model = copy.deepcopy(model)
        hashes = {}
        for service, spec in model["services"].items():
            files = spec.get("env_file") or []
            if not files:
                continue
            destination_files = []
            for index, entry in enumerate(files):
                original = Path(entry["path"] if isinstance(entry, dict) else entry)
                data = original.read_bytes()
                destination = directory / f"{service}-{index}.env"
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                destination_files.append(
                    {
                        **(entry if isinstance(entry, dict) else {}),
                        "path": str(destination),
                    }
                )
                hashes[str(original)] = fingerprint(data.hex())
            spec["env_file"] = destination_files
        return model, hashes

    def publish(self, operation):
        images = {}
        namespace = self.config["registry_namespace"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]+", namespace):
            raise Blocked("Invalid Docker Hub namespace")
        for service, spec in self.project.model["services"].items():
            build = spec.get("build")
            if build:
                if isinstance(build, str):
                    build = {"context": build}
                context = Path(build.get("context", self.project.source)).resolve()
                if not inside(context, self.project.source):
                    raise Blocked("Build context escapes source checkout")
                dockerfile = context / build.get("dockerfile", "Dockerfile")
                if not inside(dockerfile, self.project.source):
                    raise Blocked("Dockerfile escapes source checkout")
                repo = f"docker.io/{namespace}/{self.project.name}" + (
                    "" if service == self.project.primary else f"-{service}"
                )
                tag = f"{repo}:{self.project.sha[:12]}-{operation}"
                args = ["docker", "build", "--tag", tag, "--file", str(dockerfile)]
                if build.get("target"):
                    args += ["--target", build["target"]]
                for key, value in (build.get("args") or {}).items():
                    if value is None or "${" in str(value):
                        raise Blocked("Unresolved build argument")
                    args += ["--build-arg", f"{key}={value}"]
                self.run([*args, str(context)], timeout=1200)
                if service == self.project.primary and self.project.manifest.get(
                    "test"
                ):
                    self.run(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "--network",
                            "none",
                            "--read-only",
                            "--tmpfs",
                            "/tmp:rw,exec,nosuid,nodev",
                            "-v",
                            f"{self.project.source}:/workspace:ro",
                            "--entrypoint",
                            self.project.manifest["test"][0],
                            tag,
                            *self.project.manifest["test"][1:],
                        ],
                        timeout=600,
                    )
                self.run(["docker", "image", "push", tag], timeout=1200)
            else:
                tag = spec.get("image", "")
                if not tag or "${" in tag:
                    raise Blocked("Missing or unresolved service image")
                self.run(["docker", "image", "pull", tag], timeout=1200)
            digests = json.loads(
                self.run(
                    [
                        "docker",
                        "image",
                        "inspect",
                        tag,
                        "--format",
                        "{{json .RepoDigests}}",
                    ]
                ).stdout
            )
            if not digests:
                raise Blocked("Published image has no registry digest")
            ref = next(
                (
                    d
                    for d in digests
                    if build
                    and d.split("@")[0].removeprefix("docker.io/")
                    == repo.removeprefix("docker.io/")
                ),
                digests[0],
            )
            if not re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", ref):
                raise Blocked("Registry returned an invalid immutable digest")
            self.run(["docker", "image", "pull", ref], timeout=1200)
            images[service] = ref
        return images

    def activate(self, release):
        self.compose(
            release["compose"],
            "up",
            "-d",
            "--no-build",
            "--remove-orphans",
            "--wait",
            "--wait-timeout",
            "120",
        )
        deadline = time.monotonic() + self.config.get("stabilization_seconds", 15)
        while True:
            selected, _ = self.containers()
            for service in read_json(release["compose"])["services"]:
                container = selected.get(service)
                if not container or not container["State"]["Running"]:
                    raise Blocked("Application stopped during health stabilization")
                health = container["State"].get("Health", {}).get("Status")
                if health and health != "healthy":
                    raise Blocked("Application health stabilization failed")
            if time.monotonic() >= deadline:
                break
            time.sleep(2)

    def compensate(self, journal):
        previous = journal.get("previous")
        if previous and (journal.get("activated") or journal.get("production_stopped")):
            self.activate(previous)
        elif journal.get("activated"):
            self.compose(journal["release"]["compose"], "stop")
        if journal.get("proxy_started"):
            self.proxy.restore(journal["routes"], journal["prior_routes"])
        # Preserve data written by a failed replacement, while vacating the intended
        # destination for a later retry. Never copy old data over those writes.
        for destination in journal.get("promoted_dirs", []):
            self.quiescent([{"source": destination, "destination": destination}])
            retained = Path(destination + ".failed-" + journal["id"])
            if Path(destination).exists():
                if retained.exists():
                    raise Blocked(
                        "Retained migration recovery directory already exists"
                    )
                Path(destination).rename(retained)
        for writer in journal.get("development_writers", []):
            self.run(["docker", "start", writer])
        journal["status"] = "restored"
        self.store.write("journal.json", journal)
        if previous:
            self.store.write("current.json", previous)

    def apply(self, plan_id, approvals):
        if not re.fullmatch(r"[a-z0-9-]+", plan_id):
            raise Blocked("Invalid plan identifier")
        plan = self.store.read(f"plans/{plan_id}.json")
        if not plan:
            raise Blocked("Plan does not exist")
        observed, selected, _ = self.observe()
        self.project.clean()
        check_plan(plan, approvals, observed)
        operation = plan["operation"]
        if plan.get("noop"):
            return {"status": "unchanged", "release": plan["previous"]["id"]}
        if operation == "recover":
            journal = plan["prior_journal"]
            self.compensate(journal)
            return {"status": "restored", "operation": journal["id"]}
        if operation == "accept":
            journal = plan["prior_journal"]
            journal["migration_accepted"] = True
            journal["status"] = "accepted"
            self.store.write("journal.json", journal)
            self.store.write(f"operations/{journal['id']}.json", journal)
            return {
                "status": "accepted",
                "retained_sources": [m["source"] for m in journal["migrations"]],
            }
        previous = plan["previous"] or self.baseline(selected, plan_id)
        journal = {
            "id": plan_id,
            "status": "preparing",
            "previous": previous,
            "routes": plan["routes"],
            "prior_routes": plan["prior_routes"],
            "approvals": approvals,
            "migrations": plan["migrations"],
            "development_writers": [],
            "snapshots": [],
            "promoted_dirs": [],
        }
        self.store.write("journal.json", journal)
        try:
            if operation in ("rollback", "recreate"):
                release = (
                    previous
                    if operation == "recreate"
                    else self.store.read(f"releases/{plan['target']}/release.json")
                )
                if operation == "recreate":
                    directory = self.store.root / "releases" / plan_id
                    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    model = read_json(previous["compose"])
                    for service, path in self.project.env_files.items():
                        if service in model["services"]:
                            model["services"][service]["env_file"] = [
                                {"path": path, "required": True}
                            ]
                    model, hashes = self.freeze_environment(model, directory)
                    path = directory / "compose.json"
                    write_json(path, model)
                    release = {
                        **previous,
                        "id": plan_id,
                        "compose": str(path),
                        "environment": hashes,
                        "identity": self.project.identity(),
                        "created": time.time(),
                    }
                    self.store.write(f"releases/{plan_id}/release.json", release)
            else:
                self.project.storage.ensure(self.project.database)
                images = self.publish(plan_id)
                self.project.clean()
                if self.project.identity() != plan["source_identity"]:
                    raise Blocked(
                        "Source or production environment changed during publication"
                    )
                now_selected, _ = self.containers()
                if {s: c["Id"] for s, c in now_selected.items()} != {
                    s: c["Id"] for s, c in selected.items()
                }:
                    raise Blocked("Production containers changed during publication")
                if self.proxy.observe(plan["routes"]) != plan["prior_routes"]:
                    raise Blocked("Proxy configuration changed during publication")
                directory = self.store.root / "releases" / plan_id
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                model, environment = self.freeze_environment(
                    self.project.desired(images), directory
                )
                path = directory / "compose.json"
                write_json(path, model)
                self.compose(path, "config", "--quiet")
                release = {
                    "id": plan_id,
                    "compose": str(path),
                    "images": images,
                    "revision": self.project.sha,
                    "created": time.time(),
                    "environment": environment,
                    "identity": self.project.identity(),
                }
                self.store.write(f"releases/{plan_id}/release.json", release)
                for move in plan["migrations"]:
                    self.project.storage.copy(
                        move["source"], move["destination"] + ".staging-" + plan_id
                    )
                journal["status"] = "stopping-writers"
                journal["development_writers"] = sorted(
                    {
                        w
                        for m in plan["migrations"]
                        if m["development"]
                        for w in m["writers"]
                    }
                )
                self.store.write("journal.json", journal)
                writers = sorted(
                    {c["Id"] for c in selected.values() if c["State"]["Running"]}
                    | {w for m in plan["migrations"] for w in m["writers"]}
                )
                if writers:
                    journal["production_stopped"] = True
                    self.store.write("journal.json", journal)
                    self.run(["docker", "stop", *writers])
                self.quiescent(plan["migrations"])
                journal["snapshots"] = self.project.storage.snapshot(
                    plan_id, self.project.database
                )
                self.store.write("journal.json", journal)
                for move in plan["migrations"]:
                    stage = move["destination"] + ".staging-" + plan_id
                    self.project.storage.copy(move["source"], stage)
                    self.project.storage.copy(move["source"], stage, verify=True)
                    destination = Path(move["destination"])
                    if destination.exists():
                        if any(destination.iterdir()):
                            raise Blocked(
                                "Migration destination became populated during staging"
                            )
                        destination.rmdir()
                    journal["promoted_dirs"].append(str(destination))
                    self.store.write("journal.json", journal)
                    Path(stage).rename(destination)
                self.quiescent(plan["migrations"])
                if plan["migrations"]:
                    journal["snapshots"] += self.project.storage.snapshot(
                        plan_id + "-ready", self.project.database
                    )
                    self.store.write("journal.json", journal)
            journal["release"] = release
            journal["activated"] = (
                True  # Journal before the potentially interrupted side effect.
            )
            journal["status"] = "activating"
            self.store.write("journal.json", journal)
            self.activate(release)
            journal["proxy_started"] = True
            self.store.write("journal.json", journal)
            self.proxy.ensure(plan["routes"])
            for writer in journal["development_writers"]:
                self.run(["docker", "start", writer])
            journal["status"] = "succeeded"
            self.store.write("current.json", release)
            self.store.write("journal.json", journal)
            self.store.write(f"operations/{plan_id}.json", journal)
            return {
                "status": "succeeded",
                "release": release["id"],
                "images": release["images"],
                "snapshots": journal["snapshots"],
                "migration_acceptance_required": bool(plan["migrations"]),
            }
        except Exception as error:
            journal["status"] = "recovery-required"
            self.store.write("journal.json", journal)
            try:
                self.compensate(journal)
            except Exception:
                self.store.write(f"operations/{plan_id}.json", journal)
                raise Blocked(
                    f"Operation {plan_id} requires manual recovery (exit 8)"
                ) from error
            self.store.write(f"operations/{plan_id}.json", journal)
            raise Blocked(
                f"Operation {plan_id} failed; prior application restored (exit 7)"
            ) from error

    def status(self):
        _, selected, routes = self.observe()
        current = self.store.read("current.json")
        findings = []
        if current:
            expected = read_json(current["compose"])
            for service, spec in expected["services"].items():
                actual = selected.get(service)
                if not actual:
                    findings.append(f"{service}: missing")
                    continue
                if (
                    not actual["State"]["Running"]
                    or actual["State"].get("Health", {}).get("Status", "healthy")
                    != "healthy"
                ):
                    findings.append(f"{service}: unhealthy")
                if (
                    actual["Config"]["Image"] != spec["image"]
                    and actual["Image"] != spec["image"]
                ):
                    findings.append(f"{service}: image drift")
                want_mounts = {
                    (v["source"], v["target"])
                    for v in spec.get("volumes", [])
                    if v["type"] == "bind"
                }
                actual_mounts = {
                    (v["Source"], v["Destination"])
                    for v in actual["Mounts"]
                    if v["Type"] == "bind"
                }
                if want_mounts != actual_mounts:
                    findings.append(f"{service}: storage drift")
            for path, value in current.get("environment", {}).items():
                if (
                    not Path(path).exists()
                    or fingerprint(Path(path).read_bytes().hex()) != value
                ):
                    findings.append("production environment changed")
        else:
            findings.append("application requires adoption")
        for route in self.project.routes():
            host = routes[route["domain"]]
            if (
                not host
                or host["forward_host"] != route["container"]
                or host["forward_port"] != route["port"]
            ):
                findings.append("proxy route drift: " + route["domain"])
        for dataset, props in self.project.storage.observe(
            self.project.database
        ).items():
            if not props or props["mounted"] != "yes" or props["canmount"] != "on":
                findings.append("storage mount drift: " + dataset)
        return {
            "app": self.project.name,
            "release": current["id"] if current else None,
            "findings": findings,
            "journal": (self.store.read("journal.json") or {}).get("status"),
            "releases": sorted(
                p.parent.name
                for p in (self.store.root / "releases").glob("*/release.json")
            ),
        }
