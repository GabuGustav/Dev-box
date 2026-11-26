# Phase 2.2 — Privileged Worker Boundary

**Branch:** `phase-2-privileged-broker`  
**Base:** `phase-2-security-hardening`  
**Status:** Source inventory complete; broker implementation not started  
**Parent verification:** CI run [45860830556](https://github.com/GabuGustav/Dev-box/actions/runs/45860830556) passed on the base branch.

## Objective

Remove direct Docker daemon access and `CAP_SYS_ADMIN` from the general worker. Move the smallest possible set of Docker and XFS administration capabilities into a separate, private, typed host broker. Preserve the existing API contract and workspace behavior.

This is a blast-radius reduction, not a claim that the worker becomes tenant-isolated: it will initially retain shared workspace/snapshot filesystem access. Cross-tenant data exposure after worker compromise remains a separate threat-model item.

## Source inventory

### Docker operations currently in `worker/main.py`

- Image presence/build: `docker_client.images.get` and `images.build` during worker startup.
- Workspace container lifecycle: get/create/start/unpause/stop/pause/remove/inspect.
- Git helper execution: create/start/wait/logs/stop/remove a short-lived container with a fixed image, fixed workspace bind mount, and optionally restricted egress.
- Managed workspace reconciliation: list labeled containers, inspect labels/mounts/state, stop unsafe containers.
- Orphaned Git helper reconciliation: list labeled helpers and remove completed helpers.
- Health endpoint: Docker daemon ping.

### XFS and filesystem operations currently in `worker/storage.py`

- `findmnt` verifies the configured mount is a dedicated XFS mount with project quotas enabled.
- `xfs_quota` verifies project quota state, initializes project trees, sets block/inode limits, scales limits during restore, and clears project assignments.
- Worker creates workspace/snapshot directories, maintains the SQLite project registry, validates directory device/inode identity, changes directory ownership, and removes workspace trees.
- `worker/snapshots.py` performs authenticated encryption/decryption, archive creation/extraction, staging, rollback, and recovery directly against the shared workspace/snapshot roots.

### Existing boundaries to preserve

- The worker API already requires a bearer service token.
- Workspace IDs are strictly validated and deterministic container/project identifiers are derived from the workspace ID.
- Workspace containers run with a fixed image, non-root UID, no network, private IPC/PID defaults, dropped capabilities, read-only root filesystem, no-new-privileges, and resource limits.
- Git helper containers use a fixed image, per-workspace mount, bounded time/output, no capabilities, read-only root filesystem, and the restricted egress network only when needed.
- Docker socket access is currently mounted only into the worker; workspace containers do not receive it.

## Target topology

```text
API
 |
 | existing authenticated worker API
 v
Unprivileged worker (no Docker socket, no CAP_SYS_ADMIN)
 |   |   \ shared workspace/snapshot mounts remain temporarily
 |    \ (known cross-tenant data-plane risk)
 |
 | private authenticated typed RPC
 v
Privileged broker (Docker socket + minimum XFS authority)
 |
 +-- fixed image management
 +-- workspace container lifecycle and inspection
 +-- fixed Git helper lifecycle and bounded output
 +-- managed-container reconciliation
 +-- XFS mount/project/quota operations
```

The broker must not be reachable from workspace containers, the egress proxy, or public host interfaces. Do not expose Docker's TCP API or forward arbitrary Docker API paths.

## Phased implementation

### 2.2.1 — Broker contract and validation
- [x] Inventory Docker and XFS operations in the current worker.
- [ ] Define typed Pydantic request/response models for each operation.
- [ ] Validate workspace IDs, operation enums, bounded resource values, and request sizes at the broker independently of worker validation.
- [ ] Derive all container names, labels, paths, project IDs, images, mount targets, security options, and networks inside the broker.
- [ ] Define error mapping, idempotency, timeout, reconciliation, and audit-log behavior.
- [ ] Add threat-model note that shared filesystem access still permits worker-to-worker tenant data compromise.

**Exit:** broker protocol has no arbitrary Docker API payloads, host paths, shell commands, image references, devices, capabilities, or mount definitions.

### 2.2.2 — Broker service and private transport
- [x] Add a dedicated broker service/image and internal-only Compose network.
- [x] Add a broker-only service token distinct from the worker/API token; compare secrets in constant time.
- [ ] Enforce request body limits and strict schemas; do not log tokens, Git output, or file contents.
- [x] Add broker health/readiness check and startup token validation.
- [ ] Ensure no port is published to the host.

**Exit:** only the worker can reach the broker on its private network; unauthorized, malformed, oversized, and unknown requests fail closed.

### 2.2.3 — XFS administration migration
- [ ] Move `findmnt`/XFS state inspection and `xfs_quota` mutations behind typed broker methods.
- [ ] Replace raw command runner injection with a broker quota client in production while preserving a deterministic fake for unit tests.
- [ ] Broker derives mount roots and project paths from trusted configured roots and workspace IDs.
- [ ] Broker accepts only positive bounded block/inode limits and a small restore multiplier range.
- [ ] Add negative tests for traversal, alternate mount roots, unknown IDs, invalid limits, and arbitrary command attempts.

**Exit:** worker has no `CAP_SYS_ADMIN`; it cannot invoke XFS commands or choose arbitrary host mount paths.

### 2.2.4 — Docker lifecycle migration
- [ ] Move fixed workspace container create/start/stop/pause/unpause/remove/inspect to broker methods.
- [ ] Move image ensure/build to broker startup with a fixed build context and fixed image tag.
- [ ] Move managed-container reconciliation and orphan helper cleanup into the broker.
- [ ] Replace worker Docker SDK use with a typed HTTP broker client.
- [ ] Preserve the current workspace container hardening profile and verify actual Docker HostConfig after creation.
- [ ] Add idempotency and interrupted-operation tests.

**Exit:** worker imports no Docker SDK and cannot issue Docker daemon requests directly.

### 2.2.5 — Git helper migration
- [ ] Move ephemeral Git helper container lifecycle into broker.
- [ ] Send typed Git operation requests, not arbitrary Docker command/configuration.
- [ ] Keep repository URL/branch/path validation in a shared module and repeat critical validation in the broker.
- [ ] Keep fixed image, workspace mount, resource limits, bounded time/output, and restricted egress proxy.
- [ ] Add hostile Git config, timeout, output truncation, and helper cleanup tests.

**Exit:** Git operations work without giving worker Docker access or weakening helper isolation.

### 2.2.6 — Remove ambient worker privilege
- [ ] Remove `/var/run/docker.sock` from worker mounts.
- [ ] Remove `CAP_SYS_ADMIN` from worker.
- [ ] Run worker under a dedicated non-root UID/GID where compatible with shared storage access; document any remaining group-based access.
- [ ] Keep Docker socket and XFS authority only in the broker.
- [ ] Verify workspace containers cannot resolve or connect to broker service/network.

**Exit:** runtime inspection confirms worker has no Docker socket, no `CAP_SYS_ADMIN`, and broker is not reachable from untrusted workspace containers.

### 2.2.7 — CI and documentation
- [ ] Add broker unit/API tests and a Docker-backed integration job.
- [ ] Add negative broker capability tests.
- [ ] Verify worker health, workspace lifecycle, snapshots, Git operations, reconciliation, and restart recovery end-to-end.
- [ ] Update `SECURITY.md`, `ARCHITECTURE.md`, `STORAGE.md`, `DEPLOYMENT.md`, and `ROADMAP.md`.
- [ ] Record CI run IDs and remaining limitations.

**Exit:** all broker and regression tests pass; security docs accurately describe the remaining shared-storage trust boundary.

## Implementation constraints

- No generic Docker socket proxy and no Docker-over-TCP endpoint.
- No arbitrary shell command or raw `xfs_quota` string in broker requests.
- No user-controlled Docker image, host path, mount, device, capability, entrypoint, privileged flag, or network mode.
- No removal of existing workspace security settings.
- Do not enable multiple worker replicas until the process-local locking issue is separately resolved.
- Do not claim hostile multi-tenant isolation while the worker retains access to every workspace/snapshot directory.

## Progress log

- [x] Source inventory: `worker/main.py`, `worker/storage.py`, `worker/snapshots.py`, `docker-compose.yml`, `worker.Dockerfile`, and related tests inspected.
- [ ] Broker API design reviewed and fully integrated. Initial broker endpoints, Git command allowlist, service-token validation, Docker lifecycle methods, and unit tests are implemented; the worker still uses Docker directly and the XFS operations are not migrated.
- [x] Added broker package, FastAPI service, dedicated Docker image, internal Compose network, separate broker token, and unit tests for authorization, path validation, fixed workspace container configuration, Git command allowlisting, and idempotent stop behavior.
- [ ] Broker service has not yet been connected to worker routes; worker still mounts Docker socket and has `CAP_SYS_ADMIN`.
- [ ] XFS quota operations are still executed directly by worker; privilege removal is not complete.


## Detailed implementation architecture

### Storage adapter

Keep `XFSWorkspaceStorage` as the privileged implementation used only inside the broker. Add a worker-side `RemoteXFSWorkspaceStorage` adapter that preserves the methods consumed by `SnapshotStore`:

- `validate_backend()`
- `ensure_workspace(workspace_id)`
- `set_project_quota(workspace_id, multiplier)`
- `prepare_project_directory(workspace_id, directory, project_id, quota_multiplier)`
- `mark_workspace_initialized(workspace_id, directory)`
- `delete_workspace(workspace_id)`
- Pure local path/project-ID derivation for the configured storage root.

The worker must not receive raw `findmnt` or `xfs_quota` command access. Broker methods take a storage-kind enum (`workspace` or `snapshot`) plus a validated workspace ID and bounded quota multiplier. The broker independently derives paths and project IDs, maintains the trusted XFS project registry, and refuses paths that are not the expected direct child of its configured mount.

### Shared filesystem permissions during this transition

The worker still performs snapshot archive reads/writes against the shared storage mounts. To remove root and `CAP_CHOWN` from the worker without breaking that flow:

- Broker owns the mount roots and prepares them as root-owned, group `10001`, mode `2750`; the broker-owned XFS registry remains inaccessible for worker writes.
- Broker creates per-workspace directories owned by `10001:10001`, mode `0770`.
- Worker runs as UID `10002`, GID `10001`, with no added capabilities. It can operate on workspace trees through the shared group but cannot chown, alter the broker registry, or administer XFS.
- Workspace/Git helper containers continue to run as `10001:10001` and receive only their own workspace bind mount.
- Snapshot storage is not mounted into workspace containers.

This is a transitional trust model: the worker can still read/write all workspace trees through group membership. It is not hostile-tenant isolation. A later per-workspace storage-operation boundary or microVM architecture is needed to prevent a compromised worker from reading another tenant's files.

### Docker adapter

Move Docker SDK usage entirely to the broker. The worker-side client exposes named operations only; it does not implement or tunnel the Docker Engine API. Broker methods own:
- fixed workspace container lifecycle and inspect/identity validation;
- fixed Git helper create/start/wait/logs/remove with strict Git operation allowlisting;
- fixed image build/ensure using the broker's read-only build context;
- managed container and orphan-helper reconciliation.

The broker derives the container name and workspace mount path itself. Worker-supplied values must never select an image, mount, device, capability, network, entrypoint, or Docker host configuration.
