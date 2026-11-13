#!/usr/bin/env python3
"""Unprivileged bridge from a reusable workflow to the installed host engine."""

import json
import os
import subprocess
import sys


def call(*args):
    result = subprocess.run(
        ["sudo", "-n", "/opt/server-deploy/bin/server-deploy", *args],
        text=True,
        capture_output=True,
    )
    if result.returncode:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    return json.loads(result.stdout)


app = os.environ["DEPLOY_APP"]
operation = os.environ.get("DEPLOY_OPERATION", "deploy")
if operation == "deploy" and (
    os.environ.get("GITHUB_REF_TYPE") != "tag"
    or os.environ.get("GITHUB_EVENT_NAME") != "push"
):
    sys.exit("Production deployment requires a pushed tag")
base = [
    "--app",
    app,
    "--source",
    os.environ["GITHUB_WORKSPACE"],
    "--manifest",
    os.environ.get("DEPLOY_MANIFEST", "deploy/app.json"),
]
if operation in ("inspect", "status"):
    print(json.dumps(call(operation, *base), indent=2))
    sys.exit(0)
plan_args = ["plan", *base, "--operation", operation]
if os.environ.get("DEPLOY_TARGET"):
    plan_args += ["--target", os.environ["DEPLOY_TARGET"]]
if os.environ.get("DEPLOY_SEED") == "true":
    plan_args.append("--seed-development")
plan = call(*plan_args)
print(json.dumps(plan, indent=2))
summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a") as output:
        output.write(
            "Deployment plan\n\n```json\n" + json.dumps(plan, indent=2) + "\n```\n"
        )
if os.environ.get("DEPLOY_PLAN_ONLY") != "true":
    result = call(
        "apply",
        *base,
        "--plan",
        plan["id"],
        "--approve",
        os.environ.get("DEPLOY_APPROVALS", "deployment,downtime"),
    )
    print(json.dumps(result, indent=2))
    if summary:
        with open(summary, "a") as output:
            output.write(
                "\nDeployment result\n\n```json\n"
                + json.dumps(result, indent=2)
                + "\n```\n"
            )
