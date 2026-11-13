import copy
import json
import re
from pathlib import Path

from .core import (
    Blocked,
    env_hash,
    fingerprint,
    inside,
    name,
    no_secrets,
    read_json,
    run,
)
from .storage import Storage


class Project:
    def __init__(self, config, app, source, manifest="deploy/app.json", runner=run):
        self.run = runner
        self.name = name(app)
        self.config = config
        self.policy = config["apps"].get(app)
        if not self.policy:
            raise Blocked("Application is not registered in host policy")
        self.source = Path(source).resolve()
        roots = config.get("source_roots", [])
        if not roots or not any(inside(self.source, root) for root in roots):
            raise Blocked(
                "Source checkout is outside the allowed runner/development roots"
            )
        manifest_path = self.source / manifest
        if not inside(manifest_path, self.source):
            raise Blocked("Manifest escapes the checked-out source")
        self.manifest = read_json(manifest_path)
        if self.manifest.get("schema") != 1 or self.manifest.get("app") != app:
            raise Blocked("Manifest application or schema is invalid")
        self.sha = self.git("rev-parse", "HEAD").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", self.sha):
            raise Blocked("Invalid source commit")
        origin = self.git("remote", "get-url", "origin").strip().removesuffix(".git")
        if origin != "https://github.com/" + self.policy["repository"]:
            raise Blocked("Repository does not match the host application policy")
        storage_class = self.policy.get("storage", "hdd")
        if storage_class not in ("hdd", "ssd"):
            raise Blocked("Storage class must be hdd or ssd")
        self.storage = Storage(app, storage_class, runner)
        self.compose_path = self.source / self.manifest.get(
            "compose", "docker-compose.yml"
        )
        if not inside(self.compose_path, self.source):
            raise Blocked("Compose file escapes project source")
        result = runner(
            [
                "docker",
                "compose",
                "-f",
                str(self.compose_path),
                "config",
                "--format",
                "json",
                "--no-interpolate",
                "--no-env-resolution",
            ],
            cwd=self.source,
        )
        self.model = json.loads(result.stdout)
        no_secrets(self.model)
        self.primary = self.manifest["primary"]
        if self.primary not in self.model["services"]:
            raise Blocked("Primary service is missing from Compose")
        if not self.model["services"][self.primary].get("healthcheck", {}).get("test"):
            raise Blocked("Primary service requires an explicit Compose healthcheck")
        self.stores = self.manifest.get("stores", [])
        seen = set()
        for store in self.stores:
            name(store["name"])
            service, target = store["service"], store["target"]
            if service not in self.model["services"] or not target.startswith("/"):
                raise Blocked(
                    "Store references an unknown service or invalid container path"
                )
            if store["kind"] not in ("database", "app") or (service, target) in seen:
                raise Blocked("Invalid or duplicate store declaration")
            seen.add((service, target))
        self.env_files = self.policy.get("env_files", {})
        self.environment_hash = env_hash(self.env_files.values())

    def git(self, *args):
        return self.run(
            [
                "git",
                "-c",
                f"safe.directory={self.source}",
                "-C",
                str(self.source),
                *args,
            ]
        ).stdout

    def clean(self):
        if self.git("rev-parse", "HEAD").strip() != self.sha:
            raise Blocked("Source revision changed during the operation")
        if self.git("status", "--porcelain", "--untracked-files=all").strip():
            raise Blocked("Source worktree is dirty")

    @property
    def database(self):
        return any(s["kind"] == "database" for s in self.stores)

    def routes(self):
        routes = []
        for kind, container in (
            ("production", self.name),
            ("development", self.name + "-dev"),
        ):
            domain = self.policy.get("proxy", {}).get(kind)
            if domain:
                routes.append(
                    {
                        "domain": domain,
                        "container": container,
                        "port": int(self.manifest["port"]),
                    }
                )
        return routes

    def container_name(self, service):
        return self.name if service == self.primary else f"{self.name}-{service}"

    def desired(self, images):
        result = copy.deepcopy(self.model)
        result["name"] = self.name
        result["networks"] = {
            "private": {"external": True, "name": "private"},
            "app-local": {"name": f"{self.name}-local"},
        }
        result.pop("volumes", None)
        for service, spec in result["services"].items():
            if any(
                spec.get(k)
                for k in (
                    "privileged",
                    "pid",
                    "ipc",
                    "devices",
                    "cap_add",
                    "volumes_from",
                )
            ):
                raise Blocked(
                    "Privileged runtime options require a separately reviewed host policy"
                )
            spec.pop("build", None)
            spec["image"] = images[service]
            spec["container_name"] = self.container_name(service)
            spec["restart"] = "unless-stopped"
            spec["networks"] = (
                ["app-local", "private"] if service == self.primary else ["app-local"]
            )
            spec.pop("ports", None)
            spec.pop("develop", None)
            spec.pop("profiles", None)
            spec.pop("pull_policy", None)
            spec["labels"] = {
                k: v
                for k, v in (spec.get("labels") or {}).items()
                if not k.startswith("io.murphy-deploy.")
            }
            spec["labels"]["io.server-deploy.app"] = self.name
            env = spec.get("environment") or {}
            # Resolve environment on the host via a protected production env file.
            if any(isinstance(v, str) and "${" in v for v in env.values()):
                raise Blocked(
                    "Use a production env_file for variable environment values"
                )
            if spec.get("env_file"):
                if service not in self.env_files:
                    raise Blocked(
                        f"No production env_file mapped for service {service}"
                    )
                spec["env_file"] = [{"path": self.env_files[service], "required": True}]
            mounts = []
            for mount in spec.get("volumes", []):
                if not isinstance(mount, dict):
                    raise Blocked("Compose did not normalize a volume declaration")
                store = next(
                    (
                        s
                        for s in self.stores
                        if s["service"] == service and s["target"] == mount["target"]
                    ),
                    None,
                )
                if not store:
                    raise Blocked(f"Declare a store for {service}:{mount['target']}")
                mounts.append(
                    {
                        "type": "bind",
                        "source": self.storage.destination(store),
                        "target": mount["target"],
                        "read_only": bool(mount.get("read_only")),
                    }
                )
            spec["volumes"] = mounts
        # Unresolved build/volume/image substitutions must not enter production artifacts.
        if "${" in json.dumps(result):
            raise Blocked(
                "Unresolved Compose interpolation in production configuration"
            )
        if result.get("secrets") or result.get("configs"):
            raise Blocked(
                "Compose secret/config file sources require explicit host mappings"
            )
        return result

    def identity(self):
        return fingerprint(
            {
                "sha": self.sha,
                "manifest": self.manifest,
                "compose": self.model,
                "policy": self.policy,
                "environment": env_hash(self.env_files.values()),
            }
        )
