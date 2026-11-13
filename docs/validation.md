# Validation performed on 2026-11-13

The replacement is independent of Murphy's executable, Go modules, state files, and configuration. Existing credentials were copied once into the replacement's own protected host configuration. Runtime compatibility recognizes existing container ownership labels during adoption.

| Area | Evidence |
|---|---|
| Planning and approval checks | Unit tests cover expiry, missing risk categories, source/environment/resource drift, and invalid saved plan identifiers |
| Secret handling | Unit tests reject inline credentials and unsafe file permissions; immutable release environments and host state are mode 0600 |
| Storage safety | Unit tests cover capacity/overhead, boot mounts, populated mountpoints, overlapping paths, checksum verification, and metadata-preserving copy arguments |
| Live ZFS lifecycle | An opt-in rehearsal created a unique file-backed pool with disposable datasets, verified canmount=on, performed verified migration, and retained held snapshots and original data |
| Live application lifecycle | Disposable containers exercised adoption, healthy replacement, exact-artifact rollback, an interrupted operation recovered by a fresh engine instance, a failed start, preservation of failed-release writes, retry, and migration acceptance |
| Image publication | The engine built/tested the dashboard source, pushed a unique validation image to Docker Hub, resolved an immutable digest, and pulled it successfully |
| NPM | Authentication and observation were validated against the actual server. Ownership, wildcard/expiry rules, conflict handling, and restoration are unit tested. Live host creation/certificate issuance was not exercised: there was no existing valid wildcard certificate suitable for a disposable hostname |
| GitHub Actions | The shared engine's tests passed on GitHub-hosted CI. The dashboard called the pinned reusable workflow on its own self-hosted runner and successfully generated a production reconciliation plan without applying it |

The rehearsal replaces registry publication and proxy calls with adapters, and never passes real HDD/SSD dataset names to ZFS. Its temporary pool, containers, networks, and vdev file are removed afterward. Registry publication was tested separately, without changing the running dashboard.

This evidence does not establish universal compatibility with every Compose application. Each project needs a reviewed manifest and host policy. Unsupported configurations fail closed. Public DNS/HTTPS checks, live certificate issuance, and every possible process-interruption boundary have not been validated. Persistent data is never automatically rolled back or destroyed; retained data and snapshots need deliberate operator cleanup.
