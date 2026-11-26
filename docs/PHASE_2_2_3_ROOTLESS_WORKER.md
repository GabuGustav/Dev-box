# Phase 2.2.3 — Remove Ambient Worker Privilege

**Branch:** `phase-2-xfs-quota-broker`  
**Base:** `phase-2-privileged-broker`  
**Base verification:** CI run [45864319247](https://github.com/GabuGustav/Dev-box/actions/runs/45864319247) completed successfully. Both the quality job and the dedicated Linux XFS/Docker integration job passed; the integration job ran all five tests.  
**Status:** Implementation committed; CI and Linux XFS/Docker verification pending.

## Objective

Finish the worker-side privilege reduction after the Docker and XFS RPC migrations. The broker remains the only component with Docker socket access and `CAP_SYS_ADMIN`. The worker must run as a dedicated non-root UID with all Linux capabilities dropped, no privilege escalation, a read-only container root, and only bounded writable temporary/state mounts.

This is a blast-radius reduction. The worker still needs shared access to workspace and snapshot data to perform Git and snapshot operations. Until storage operations are moved behind a per-workspace data service or a stronger VM boundary, a compromised worker can still access multiple tenants' data. This phase must not claim hostile-tenant isolation.

## Investigation findings

1. Docker lifecycle, image build, Git helper execution, and XFS project quota operations already route through the authenticated typed broker API.
2. The worker Compose service no longer mounts the Docker socket and no longer receives `CAP_SYS_ADMIN`.
3. The worker image still starts as root. The worker has shared bind mounts for both XFS roots, so root in the worker can access every workspace and snapshot directory.
4. `XFSWorkspaceStorage` currently creates workspace directories as UID/GID `10001:10001` with mode `0700`; restore staging directories use the same restrictive ownership/mode.
5. `SnapshotStore` currently changes snapshot directories to root-owned mode `0700` and keeps its SQLite metadata registry at the snapshot filesystem root. This conflicts with a non-root worker and must be redesigned without making the XFS mount root broadly writable.
6. The broker stores its XFS project registries under the broker-only `broker_state` volume. Those registries must remain inaccessible to the worker.
7. The worker and workspace runtime need a carefully documented shared group permission model for the transitional shared-mount design. This does not prevent a compromised worker from reading other workspace directories.

## Implementation phases

### 2.2.3-A — Persistent worker state and identity
- [x] Add a dedicated persistent worker-state volume, separate from broker state and XFS data.
- [x] Have the broker initialize that volume with owner UID `10002`, group GID `10001`, and restrictive permissions before the worker starts.
- [x] Move the snapshot metadata SQLite registry into worker state; migrate an existing legacy registry read-only and atomically.
- [x] Keep XFS project registries exclusively in broker state.

### 2.2.3-B — Shared XFS directory permissions
- [x] Broker validates and prepares each dedicated XFS mount root as root-owned/group `10001`, mode `2750`, with no group write on the mount root.
- [x] Broker creates per-workspace directories as `10001:10001`, mode `2770`; restore staging directories use the same group-access contract.
- [x] Remove worker-side `chown` of snapshot directories. Snapshot data directories remain group `10001` and group-writable.
- [x] Ensure restored files remain writable by the workspace runtime's group without granting worker root or `CAP_CHOWN`.
- [x] Preserve fail-closed symlink, mountpoint, registry, device/inode, and XFS quota checks.

### 2.2.3-C — Rootless worker runtime
- [x] Run the worker as UID/GID `10002:10001`.
- [x] Drop all Linux capabilities and set `no-new-privileges:true`.
- [x] Make the worker root filesystem read-only; provide only bounded `/tmp` and the dedicated worker-state volume as writable paths.
- [ ] Verify worker startup, health checks, Git operations, snapshots, deletion, and reconciliation with the reduced identity in CI and Linux integration tests.
- [x] Keep broker as the only service with Docker socket and `CAP_SYS_ADMIN`; broker network remains private and unpublished.

### 2.2.3-D — Verification and documentation
- [x] Add unit tests for broker-owned mount permissions, worker identity/capabilities, registry isolation, snapshot metadata migration, restore group access, and broker failure behavior.
- [ ] Extend Linux XFS/Docker integration coverage to exercise non-root worker file access and workspace-container write access after snapshot restore.
- [ ] Run Ruff, the full unit suite, Compose validation, image builds, and Linux XFS/Docker integration on this branch.
- [x] Update `README.md`, `SECURITY.md`, `ARCHITECTURE.md`, `STORAGE.md`, `DEPLOYMENT.md`, `ROADMAP.md`, and the Phase 2.2 broker plan.
- [ ] Record workflow run IDs and distinguish passing checks from remaining adversarial work.

## Security invariants

- The worker receives no Docker socket, no `CAP_SYS_ADMIN`, no `CAP_CHOWN`, and no Linux capabilities.
- The worker cannot modify the broker's XFS project registries or broker state.
- The worker cannot write arbitrary files directly into the XFS mount root.
- Every broker storage operation derives the storage path and project ID from a validated workspace ID and configured storage kind; no caller-supplied host paths or command strings are accepted.
- Workspace containers continue to receive only their own workspace directory, never snapshot storage, broker state, worker state, Docker socket, or broker credentials.
- Broker and worker APIs remain unpublished on host interfaces.
- The shared group model is explicitly documented as transitional and is not described as tenant isolation.

## Progress log

- [x] Confirmed the latest successful base branch and CI run.
- [x] Inspected broker storage RPC, remote worker storage adapter, snapshot metadata handling, Compose mounts, and current worker container identity.
- [x] Created this isolated branch from `phase-2-privileged-broker`.
- [x] Recorded the implementation plan before changing runtime behavior.
- [x] Added broker-owned XFS root permissions and group-based workspace directory modes.
- [x] Added persistent worker state, safe snapshot metadata migration, and private registry permissions.
- [x] Delegated restore commit/recovery and directory fsync to the broker.
- [x] Configured worker UID/GID `10002:10001`, all capabilities dropped, `no-new-privileges`, read-only root, and bounded `/tmp`.
- [x] Added unit tests for compose policy, state preparation, metadata migration, restore delegation, and group access.
- [x] Updated architecture, security, storage, deployment, roadmap, and broker implementation documents.
- [ ] CI and Linux XFS/Docker integration verification pending.

## Research references

- [Docker Engine security](https://docs.docker.com/engine/security/) — Docker daemon control remains a privileged host capability and should be isolated from application workers.
- [Docker security options](https://docs.docker.com/engine/security/) — use least privilege, capability reduction, and no-new-privileges for containers.
- [Linux filesystem permissions](https://man7.org/linux/man-pages/man7/path_resolution.7.html) — directory traversal and group permissions must be considered separately from file permissions.
- [XFS project quota documentation](https://man7.org/linux/man-pages/man8/xfs_quota.8.html) — quota project and hard-limit operations remain broker-owned.
