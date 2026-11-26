# Phase 2.2 — Privileged Worker Boundary

**Broker branch:** `phase-2-privileged-broker`  
**Rootless hardening branch:** `phase-2-rootless-worker-hardening`  
**Rootless base:** `088364d3bffd7fb03bc9010bd1be9057824bffb1`  
**Base verification:** CI run [45868184349](https://github.com/GabuGustav/Dev-box/actions/runs/45868184349) passed.  
**Status:** Docker/XFS broker migration verified; rootless-worker storage hardening is implemented on the current branch and awaits CI verification.

## Objective

Remove direct Docker daemon access and XFS administration from the general worker. Keep the smallest possible set of Docker and XFS administration capabilities in a separate, private, typed host broker. Preserve the existing worker API and workspace behavior.

This is a blast-radius reduction, not a claim that the worker becomes tenant-isolated. The worker still performs Git and snapshot file operations and retains group-based access to all workspace and snapshot directories on shared XFS mounts. Cross-tenant data exposure after worker compromise remains a known trust-boundary limitation.

## Source inventory

### Docker operations

The worker's former Docker responsibilities have moved behind the broker:
- fixed workspace container create/start/stop/pause/unpause/remove/inspect;
- fixed image ensure/build using broker-owned build context;
- ephemeral Git helper lifecycle and bounded output;
- managed-container and orphan-helper reconciliation;
- Docker daemon health checks.

The worker no longer mounts `/var/run/docker.sock`, imports the Docker SDK, or accepts arbitrary Docker API payloads.

### XFS and filesystem operations

- `XFSWorkspaceStorage` is the privileged implementation used by the broker.
- Broker validates the dedicated XFS mount, owns the project registries, derives project IDs and workspace paths, and performs `xfs_quota` operations.
- Worker-side `RemoteWorkspaceStorage` exposes the limited methods used by `SnapshotStore`; it does not run `findmnt` or `xfs_quota`.
- Snapshot encryption, archive validation, and archive file I/O remain in the worker.
- Restore directory swaps and interrupted-restore recovery are being moved to broker RPC because the worker must not write to the XFS mount root.

### Existing boundaries to preserve

- Worker API requires a bearer service token; broker uses a separate service token.
- Workspace IDs are strictly validated and deterministic container/project identifiers are derived from them.
- Workspace containers use fixed images, UID/GID `10001:10001`, no network by default, private IPC/PID defaults, dropped capabilities, read-only root, no-new-privileges, and resource limits.
- Git helper containers use a fixed image, per-workspace mount, bounded time/output, no capabilities, read-only root, and restricted egress only when needed.
- Workspace containers never receive the Docker socket, broker credentials, worker state, broker state, or snapshot storage.

## Target topology

```text
API
 |
 | authenticated worker API
 v
Worker (UID 10002:GID 10001; no capabilities; read-only root)
 |   |  shared workspace/snapshot mounts for file I/O
 |   |  known cross-tenant data-plane risk remains
 |
 | private authenticated typed RPC
 v
Privileged broker (Docker socket + CAP_SYS_ADMIN)
 |
 +-- fixed image management
 +-- workspace container lifecycle and inspection
 +-- fixed Git helper lifecycle and bounded output
 +-- managed-container reconciliation
 +-- XFS mount/project/quota operations
 +-- snapshot restore commit/recovery
```

The broker must not be reachable from workspace containers, the egress proxy, or public host interfaces. Do not expose Docker's TCP API or forward arbitrary Docker API paths.

## Phased implementation

### 2.2.1 — Broker contract and validation
- [x] Inventory Docker and XFS operations.
- [x] Define typed request models for storage quotas, workspace configuration, and Git helper operations.
- [x] Validate workspace IDs, operation enums, and bounded resource values.
- [x] Derive container names, labels, paths, project IDs, images, mounts, security options, and networks inside the broker.
- [ ] Add strict broker request-body limits and audit-event coverage for all privileged operations.
- [x] Document that shared filesystem access still permits worker-to-worker tenant data compromise.

### 2.2.2 — Broker service and private transport
- [x] Add dedicated broker service/image and internal-only Compose network.
- [x] Add broker-only service token distinct from the worker/API token; compare secrets in constant time.
- [x] Add broker health/readiness check and startup token validation.
- [x] Keep broker ports unpublished and broker reachable only from the worker's private network.
- [ ] Add explicit request-body size limits and complete negative malformed/oversized request tests.

### 2.2.3 — XFS administration migration
- [x] Move `findmnt`/XFS state inspection and `xfs_quota` mutations behind typed broker methods.
- [x] Replace worker-side command execution with `RemoteWorkspaceStorage`.
- [x] Broker derives mount roots and project paths from configured roots and validated workspace IDs.
- [x] Broker accepts only bounded quota multipliers and broker-owned block/inode limits.
- [x] Add broker storage RPC unit tests and Linux XFS/Docker integration coverage.

**Exit:** worker has no `CAP_SYS_ADMIN`; it cannot invoke XFS commands or choose arbitrary host mount paths.

### 2.2.4 — Docker lifecycle migration
- [x] Move fixed workspace container lifecycle and inspection to broker methods.
- [x] Move image ensure/build to broker startup with a fixed build context and image tag.
- [x] Move managed-container reconciliation and orphan helper cleanup into broker.
- [x] Replace worker Docker SDK use with authenticated HTTP broker client.
- [x] Preserve workspace container hardening and verify Docker HostConfig in tests.
- [ ] Expand interrupted-operation and broker restart idempotency tests.

**Exit:** worker imports no Docker SDK and cannot issue Docker daemon requests directly.

### 2.2.5 — Git helper migration
- [x] Move ephemeral Git helper lifecycle into broker.
- [x] Send typed Git operation requests, not arbitrary Docker configuration.
- [x] Keep URL/branch/path validation in worker routes and repeat critical validation in broker.
- [x] Keep fixed image, workspace mount, resource limits, bounded time/output, and restricted egress proxy.
- [ ] Add hostile Git config, timeout, output truncation, and helper cleanup adversarial tests.

### 2.2.6 — Remove ambient worker privilege
- [x] Remove Docker socket from worker mounts.
- [x] Remove `CAP_SYS_ADMIN` from worker.
- [x] Configure dedicated worker UID/GID `10002:10001`; workspace runtime remains `10001:10001`.
- [x] Drop all worker capabilities, enable `no-new-privileges`, and make the worker root filesystem read-only.
- [x] Keep Docker socket and XFS authority only in broker.
- [x] Keep broker inaccessible from untrusted workspace containers.
- [ ] Verify rootless worker and storage permissions in current-branch CI.

### 2.2.7 — CI and documentation
- [x] Add broker image/configuration to CI and dedicated Linux XFS/Docker integration coverage.
- [x] Add rootless Compose identity, worker-state, snapshot migration, and broker restore tests.
- [x] Update security, architecture, storage, deployment, and roadmap documentation.
- [ ] Run and inspect current-branch quality and Linux XFS/Docker integration workflows.
- [ ] Complete adversarial archive, egress, cross-workspace, and concurrency tests.

## Rootless shared-storage contract

- Broker owns mount roots as UID/GID `0:10001`, mode `2750`; the worker group can traverse/read but cannot create or rename mount-root entries.
- Broker creates workspace and snapshot directories as UID/GID `10001:10001`, mode `2770` (setgid with group read/write/execute).
- Worker runs as UID/GID `10002:10001`, with all capabilities dropped. It can operate on per-workspace trees through the shared group but cannot chown, alter broker registries, or administer XFS.
- Workspace/Git helper containers remain UID/GID `10001:10001` and receive only their own workspace bind mount.
- Snapshot metadata is stored in the dedicated `worker_state` volume at `/var/lib/devbox-worker/snapshots.sqlite3`. Legacy metadata is migrated through a read-only SQLite connection.
- Broker-owned XFS project registries remain in `broker_state`, inaccessible to the worker.
- Snapshot restore staging is created by the broker. The worker extracts group-accessible files into it, then asks the broker to atomically swap the stage into place, update the registered directory identity, reset quota, and fsync the mount root.

This is a transitional trust model: the worker can still read/write all workspace and snapshot trees through group membership. It is not hostile-tenant isolation. A later per-workspace storage-operation boundary or microVM architecture is needed to prevent a compromised worker from reading another tenant's files.

## Implementation constraints

- No generic Docker socket proxy and no Docker-over-TCP endpoint.
- No arbitrary shell command or raw `xfs_quota` string in broker requests.
- No user-controlled Docker image, host path, mount, device, capability, entrypoint, privileged flag, or network mode.
- No removal of existing workspace security settings.
- Do not enable multiple worker replicas until distributed locking replaces process-local locks.
- Do not claim hostile multi-tenant isolation while the worker retains access to every workspace/snapshot directory.

## Verification evidence

- CI run [45864319247](https://github.com/GabuGustav/Dev-box/actions/runs/45864319247) passed the Docker/XFS broker migration: 146 tests passed, five were skipped in the quality job, Ruff and image builds passed, and all five Linux XFS/Docker integration tests passed.
- CI run [45868184349](https://github.com/GabuGustav/Dev-box/actions/runs/45868184349) passed the latest base Compose validation, image builds, Ruff, unit tests, and Linux XFS/Docker integration.
- Rootless worker UID/GID separation, non-writable mount roots, snapshot metadata migration, and broker restore swaps are implemented on `phase-2-rootless-worker-hardening`; verification is pending.

See [docs/PHASE_2_2_3_ROOTLESS_WORKER.md](PHASE_2_2_3_ROOTLESS_WORKER.md) for the current implementation checklist.
