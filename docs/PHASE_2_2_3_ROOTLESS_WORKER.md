# Phase 2.2.3 — Rootless Worker and Broker-Owned Restore

**Branch:** `phase-2-rootless-worker-hardening`  
**Base commit:** `088364d3bffd7fb03bc9010bd1be9057824bffb1` on `phase-2-privileged-broker`  
**Base CI:** [45868184349](https://github.com/GabuGustav/Dev-box/actions/runs/45868184349) — passed.  
**Status:** Runtime changes, tests, and documentation committed; branch CI verification pending.

## Objective

Tighten the existing rootless worker configuration. The worker currently runs as UID/GID `10001:10001`, the same identity as workspace containers, and the XFS mount roots are group-writable. Move the worker to UID/GID `10002:10001`, keep workspace runtime ownership at `10001:10001`, and make XFS mount roots non-writable to the worker. Preserve group access only on per-workspace directories.

Move snapshot metadata off the XFS mount root into a dedicated worker-state volume. Since the worker cannot rename directories at the XFS mount root, move restore commit and interrupted-restore recovery into typed broker operations.

This is privilege reduction, not hostile-tenant isolation: the worker still has group-based access to every workspace and snapshot directory on the shared mounts.

## Investigation findings

1. The broker already owns Docker socket access and `CAP_SYS_ADMIN`; the worker already has no capabilities and a read-only root filesystem.
2. The current worker uses UID/GID `10001:10001`, matching the workspace runtime identity.
3. `XFSWorkspaceStorage.validate_backend` currently changes each shared mount root to mode `2770`, allowing the worker group to write directly to the mount root.
4. Workspace directories are created with shared group ownership, but the snapshot store subsequently changes snapshot directories to mode `0700`.
5. Snapshot metadata currently resides at `.devbox-snapshots.sqlite3` directly under the snapshot mount root. A non-root worker must not need write access to that root.
6. Snapshot restore currently performs `os.replace` and directory cleanup inside the worker. Those operations must be broker-owned once mount roots become non-writable.
7. The latest base commit passed Compose validation, image builds, Ruff, unit tests, and Linux XFS/Docker integration in run 45868184349.

## Implementation phases

### 2.2.3-A — Shared storage permissions
- [x] Keep broker-owned XFS mount roots as UID 0/GID 10001, mode `2750` (group read/execute, no group write).
- [x] Create workspace and snapshot directories as UID/GID `10001:10001`, mode `2770` (setgid and group read/write/execute).
- [x] Keep broker-owned XFS project registries exclusively in `broker_state`.
- [x] Verify fail-closed mountpoint, symlink, registry, project-ID, and device/inode identity checks remain intact.

### 2.2.3-B — Worker state and snapshot metadata
- [x] Add a persistent `worker_state` volume, separate from broker state and XFS data.
- [x] Have the broker initialize it as UID/GID `10002:10001`, mode `0700`.
- [x] Move the snapshot metadata SQLite registry to `/var/lib/devbox-worker/snapshots.sqlite3`.
- [x] Migrate an existing legacy registry through a read-only SQLite connection, atomically and without deleting the source file.
- [x] Reject registry and migration-path symlinks; restrict the new metadata database to mode `0600`.

### 2.2.3-C — Rootless worker identity
- [x] Run the worker as UID/GID `10002:10001`; retain workspace runtime UID/GID `10001:10001`.
- [x] Keep all Linux capabilities dropped, `no-new-privileges`, read-only root filesystem, resource limits, and private worker networks.
- [x] Provide only bounded temporary filesystems and the dedicated worker-state volume as additional writable paths.
- [ ] Verify Git, snapshots, deletion, and reconciliation with group access and without CAP_CHOWN in branch CI.

### 2.2.3-D — Broker-owned restore transitions
- [x] Add typed broker commit-restore and recover-restore endpoints.
- [x] Derive all workspace/staging/backup paths from validated workspace IDs and configured storage roots.
- [x] Perform atomic directory swaps, rollback/recovery, registry identity refresh, quota reset, and mount-root fsync in the broker.
- [x] Add worker client and remote-storage adapter methods; keep local fallback behavior for unit tests.

### 2.2.3-E — Verification and documentation
- [x] Add tests for rootless Compose identity, mount-root write denial, per-workspace group access, snapshot metadata migration, restore delegation, and recovery.
- [x] Extend Linux XFS/Docker integration to create files as UID 10002/GID 10001, verify mount-root write denial, perform broker restore swaps, and confirm workspace UID 10001 can modify restored files.
- [ ] Run Ruff, full unit tests, Compose validation, all image builds, and Linux XFS/Docker integration.
- [x] Update README, SECURITY, ARCHITECTURE, STORAGE, DEPLOYMENT, ROADMAP, and Phase 2.2 plans with verified results and remaining limitations.

## Security invariants

- The broker is the only service with Docker socket access and `CAP_SYS_ADMIN`.
- The worker has no Docker socket, no Linux capabilities, no `CAP_CHOWN`, and no write access to the XFS mount roots.
- The worker cannot modify the broker's XFS project registries.
- The worker may read/write workspace and snapshot directories through GID 10001. This means a compromised worker can still access multiple tenants' data; the architecture is not safe for hostile multi-tenancy.
- Workspace containers receive only their own workspace directory and never receive broker state, worker state, snapshot storage, Docker socket, or broker credentials.
- Restore paths are broker-derived and validated; callers cannot supply arbitrary host paths or command strings.

## Progress log

- [x] Confirmed the current base branch head and successful CI run.
- [x] Inspected existing shared-group storage, worker identity, snapshot metadata, restore logic, and Compose topology.
- [x] Created this branch from the latest successful base commit.
- [x] Recorded this implementation plan before changing runtime behavior.
- [x] Changed mount-root mode to 2750 and per-workspace directory mode to 2770.
- [x] Added dedicated worker-state volume and read-only legacy snapshot metadata migration.
- [x] Delegated restore commit/recovery and mount-root fsync to the broker.
- [x] Configured worker UID/GID 10002:10001, all capabilities dropped, no-new-privileges, read-only root, and bounded writable tmpfs.
- [x] Added unit and Linux integration coverage and updated implementation/security documentation.
- [ ] Run and inspect branch CI; quality and Linux XFS/Docker integration must both pass.

## References

- [Docker Engine security](https://docs.docker.com/engine/security/) — daemon access and least-privilege container configuration.
- [Docker Compose service reference](https://docs.docker.com/reference/compose-file/services/) — supported CPU, memory, PID, tmpfs, and security settings.
- [Linux path resolution](https://man7.org/linux/man-pages/man7/path_resolution.7.html) — directory traversal and permission semantics.
- [xfs_quota(8)](https://man7.org/linux/man-pages/man8/xfs_quota.8.html) — broker-owned XFS project quota administration.
