import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from . import VERSION
from .core import Blocked, protected, read_json, write_json
from .engine import Engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["inspect", "status", "plan", "show-plan", "apply", "version"],
    )
    parser.add_argument("--config", default="/etc/server-deploy/config.json")
    parser.add_argument("--app")
    parser.add_argument("--source")
    parser.add_argument("--manifest", default="deploy/app.json")
    parser.add_argument("--operation")
    parser.add_argument("--target")
    parser.add_argument("--plan")
    parser.add_argument("--approve", default="")
    parser.add_argument("--seed-development", action="store_true")
    args = parser.parse_args()
    if args.command == "version":
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "schema": 1,
                    "engine_revision": Path(__file__).resolve().parents[1].name,
                }
            )
        )
        return 0
    try:
        if not args.app or not args.source:
            raise Blocked("An application and source checkout are required")
        if os.geteuid() != 0:
            raise Blocked("Run through the installed privileged deployment entry point")
        if args.config != "/etc/server-deploy/config.json" and os.environ.get(
            "SUDO_USER"
        ):
            raise Blocked(
                "Workflow invocations must use the root-managed host configuration"
            )
        if Path(args.config).stat().st_uid != 0:
            raise Blocked("Host configuration must be owned by root")
        config = read_json(protected(args.config))
        if config.get("schema") != 1:
            raise Blocked("Unsupported host configuration schema")
        os.environ["DOCKER_CONFIG"] = config["docker_config"]
        engine = Engine(config, args.app, args.source, args.manifest)
        with engine.store.lock():
            if args.command == "plan":
                result = engine.plan(
                    args.operation or "deploy", args.target, args.seed_development
                )
            elif args.command == "show-plan":
                result = engine.public_plan(engine.saved_plan(args.plan))
            elif args.command == "apply":
                if (
                    args.operation
                    and engine.saved_plan(args.plan)["operation"] != args.operation
                ):
                    raise Blocked(
                        "Saved plan operation differs from the requested operation"
                    )
                result = engine.apply(
                    args.plan, [s.strip() for s in args.approve.split(",") if s.strip()]
                )
            else:
                result = engine.status()
        print(json.dumps(result, indent=2))
        return 0
    except Blocked as error:
        message = str(error)
        print(json.dumps({"error": message}), file=sys.stderr)
        return 8 if "exit 8" in message else 7 if "exit 7" in message else 3
    except Exception:
        write_json(
            "/var/lib/server-deploy/last-error.json",
            {"traceback": traceback.format_exc()},
        )
        # Do not dump exception representations that might include secrets.
        print(
            json.dumps(
                {"error": "Deployment engine error; inspect protected host state"}
            ),
            file=sys.stderr,
        )
        return 8


if __name__ == "__main__":
    sys.exit(main())
