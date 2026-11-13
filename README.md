# Server Deploy Actions

An independent, host-installed deployment engine and reusable GitHub Actions workflow for Docker Compose projects on a Linux Docker/OpenZFS server with Nginx Proxy Manager. It neither imports nor invokes Murphy Deploy. Host policy and credentials stay on the server; projects provide a small deployment manifest.

This is a new implementation. Its automated tests cover planning, failure recovery, secret handling, storage safety, and proxy ownership. An opt-in rehearsal also exercises a real file-backed ZFS pool, disposable containers, migration, retained original data, boot mounts, held snapshots, health, and exact-artifact rollback. Docker Hub publication and live NPM changes need separate integration validation; the rehearsal stubs publication and proxy calls.

## Operations

| Operation | Behavior |
|---|---|
| `inspect` / `status` | Check source identity, protected environment files, container ownership, images, mounts, health, ZFS boot mounts, and proxy routing; show release history |
| `deploy` | Plan the tagged source, verify approvals and drift, build/test, publish unique Docker Hub tags, resolve/pull digests, prepare storage, migrate declared stores if needed, snapshot, activate, stabilize, configure TLS/proxy routes, and record the release |
| `reconcile` | Adopt the existing application after ownership checks and move stores into their declared destinations; requires explicit adoption/migration approvals |
| `recreate` | Recreate the current digest-pinned release with a new protected snapshot of production environment files |
| `rollback` | Activate a saved release's exact Compose artifact and environment snapshot, then verify health |
| `recover` | Compensate an interrupted journal using its saved previous artifact and proxy configuration |
| `accept` | Mark a verified migration accepted while retaining original data and held snapshots |

Plans expire after 15 minutes and include source, configuration, environment, container, storage, and proxy fingerprints. A new plan is required after drift. Operations are serialized per application. Persistent data is never automatically rolled back or destroyed. A failure restored to the previous application exits 7; a failure needing manual recovery exits 8. Build/test failures leave the running release in place.

Storage uses `HDD/app-APP` at `/HDD/docker-data/apps/APP`, or `SSD/app-APP` at `/SSD/apps/APP`; declared database stores use `SSD/database-APP` at `/SSD/databases/APP`. Existing data moves require a vacant destination, no unrelated writers, migration overhead and a 10% pool free-space floor. Writers are stopped for the final checksum/metadata sync. Original sources remain. Held ZFS snapshots have no automatic deletion policy; retention cleanup requires an operator decision.

The engine uses explicit manifests instead of trying to guess application roles or database paths. Unsupported or ambiguous settings fail closed. Privileged containers, host network modes, and Compose secret/config mounts are not currently supported by the materializer. Application health is Compose health plus a stabilization interval; proxy configuration is verified through NPM. External DNS and public HTTPS availability are separate infrastructure checks.

## Host installation

Run `bash scripts/install.sh` from a clean, committed, reviewed checkout. This installs a root-owned, commit-addressed engine under `/opt/server-deploy/releases` and atomically switches its active symlink. Install Docker/Compose, ZFS, rsync, Git, and Python 3.11+ on the host. Configure `/etc/server-deploy/config.json`, owned by root with mode 0600:

```json
{
  "schema": 1,
  "state_root": "/var/lib/server-deploy",
  "registry_namespace": "YOUR_DOCKERHUB_NAMESPACE",
  "docker_config": "/etc/server-deploy/docker",
  "source_roots": ["/var/lib/server-deploy-runners", "/HDD/development"],
  "stabilization_seconds": 15,
  "npm": {
    "api_url": "http://127.0.0.1:81/api",
    "login_file": "/etc/server-deploy/npm.env"
  },
  "apps": {
    "example": {
      "repository": "OWNER/example",
      "storage": "hdd",
      "env_files": {"web": "/etc/server-deploy/apps/example.env"},
      "proxy": {"production": "example.your-domain.com", "development": "example-dev.your-domain.com"}
    }
  }
}
```

Store Docker login credentials in `/etc/server-deploy/docker/config.json`. `/etc/server-deploy/npm.env` contains `NPM_IDENTITY` and `NPM_SECRET`; both it and production environment files must be mode 0600. Never commit credentials. The engine captures immutable protected environment snapshots per release, so rollback restores the release's environment rather than reading a subsequently edited file.

Run `python3 scripts/register-runner.py OWNER/REPOSITORY APP` using an authenticated GitHub CLI with repository admin access. Each personal-account repository needs its own runner registration; organization runners can instead be assigned through runner groups. The registration script creates a service account and a systemd service. Its account receives access only to the root-owned engine through sudo, rather than direct Docker or ZFS access. Register the app and source root in host policy before running it.

Workflow code and Docker build inputs still need to be trusted. Public pull requests must never run jobs on this deployment runner. Lock down tag creation, repository write access, and workflow changes.

## Project manifest

Commit `deploy/app.json` beside the project's existing Compose file:

```json
{
  "schema": 1,
  "app": "example",
  "compose": "docker-compose.yml",
  "primary": "web",
  "port": 8080,
  "stores": [
    {"name": "uploads", "service": "web", "target": "/uploads", "kind": "app"},
    {"name": "postgres", "service": "db", "target": "/var/lib/postgresql/data", "kind": "database"}
  ],
  "test": ["python", "-m", "unittest", "discover", "-s", "/workspace/tests", "-v"]
}
```

The primary service needs a Compose healthcheck. Every persistent mount must be declared. All database mounts belong on SSD; all other writable application stores follow the application's storage class. The optional test runs in the primary built image with the checkout mounted read-only at `/workspace`, no network, and a temporary writable `/tmp`. Environment interpolation should live in protected env files, not inline secret values. Development and production use distinct storage and container identities.

## Calling the reusable workflow

Use a reviewed commit SHA in place of `ENGINE_SHA`:

```yaml
name: Deploy production
on:
  push:
    tags: ['**']
permissions:
  contents: read
jobs:
  deploy:
    uses: ftechhelp/server-deploy-actions/.github/workflows/deploy.yml@ENGINE_SHA
    with:
      app: example
```

New tags deploy their exact source commit. Ordinary branch pushes do not deploy. Initial adoption or migration blocks until its required categories are explicitly approved. A maintenance workflow may call the same workflow with `operation: reconcile`, `approvals: deployment,downtime,adoption,data-migration`, and `plan-only: true` to review the plan, then run again with `plan-only: false`. Never approve categories without reading the plan. Maintenance operations must originate from `workflow_dispatch`; an ordinary pushed tag cannot silently approve migration or adoption.

Use the generated `id` as `plan-id` to apply the exact reviewed plan with `plan-only: false`; plans expire after 15 minutes and drift requires a fresh plan. Without `plan-id`, a run deliberately creates a fresh plan. Use `operation: rollback` with `target: RELEASE_ID` and `approvals: downtime` for application rollback; use `recover` for interrupted operations. `seed-development: true` copies an existing development primary container's data only into vacant production destinations, stops/resumes its writers, and requires migration approval. Original sources and snapshots stay in place after acceptance. To accept a prior verified migration after a later deployment, supply that migration's operation ID as `target`.

## Validation

`python3 -m unittest discover -s tests -v` uses temporary directories and mocked command/API adapters. It never touches host ZFS pools, production containers, or NPM routes. The host installation runs this suite before replacing engine files. A production migration rehearsal requires a disposable privileged VM and file-backed ZFS pools; never aim destructive test fixtures at the real HDD/SSD pools.
