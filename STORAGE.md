# DevBox Workspace Storage

## Decision

Workspace data uses **one directory tree per workspace on a dedicated Linux XFS filesystem with project quotas enabled**. The privileged broker uses xfs_quota to assign a project ID and enforce both a hard block limit and a hard inode limit. The worker calls the broker through authenticated, workspace-scoped storage operations.

This is the selected production backend. The ordinary Docker local named-volume driver is not used for workspace data because it does not provide a portable hard per-volume quota.

## Host requirements

- Linux host with one dedicated XFS filesystem for workspace data and a second dedicated XFS filesystem for encrypted snapshots.
- The filesystem mounted with prjquota (or pquota).
- Project quota accounting and enforcement both enabled.
- xfsprogs and findmnt available in the privileged broker image; the worker image does not contain XFS administration tools.
- The broker has `CAP_SYS_ADMIN` and is the only service with Docker socket access.
- The worker runs as UID/GID `10002:10001` with all Linux capabilities dropped; workspace containers remain UID/GID `10001:10001`.
- The host storage paths mounted into both the broker and worker at the **same absolute paths** used by the Docker daemon.
- The broker validates the XFS mounts and sets mount roots to owner/group `0:10001`, mode `2750`; per-workspace directories are provisioned for UID/GID `10001:10001`, mode `2770`. The worker can traverse the mount roots but cannot create or rename entries there; group access is limited to per-workspace trees.

Do not place workspace data on the root filesystem or a directory shared with unrelated host data. Do not format a disk containing data. Provision and mount a dedicated data device using the host operating system's documented XFS procedure.

Example /etc/fstab entry after the filesystem has been created and its UUID identified:

```fstab
UUID=<dedicated-filesystem-uuid> /srv/devbox/workspaces xfs defaults,prjquota 0 0
```

Then create the mountpoint and mount it before starting Compose:

```bash
sudo mkdir -p /srv/devbox/workspaces
sudo mount /srv/devbox/workspaces
findmnt -no TARGET,FSTYPE,OPTIONS /srv/devbox/workspaces
```

The last command must show the exact mountpoint, xfs, and prjquota or pquota. Repeat the setup for the snapshot mount at `/srv/devbox/snapshots`; it must be a separate dedicated XFS filesystem with project quotas enabled.

Configure:

```dotenv
WORKSPACE_STORAGE_ROOT=/srv/devbox/workspaces
WORKSPACE_DISK_QUOTA=10g
WORKSPACE_INODE_LIMIT=200000

SNAPSHOT_STORAGE_ROOT=/srv/devbox/snapshots
SNAPSHOT_DISK_QUOTA=20g
SNAPSHOT_INODE_LIMIT=50000
SNAPSHOT_MAX_COUNT=10
SNAPSHOT_MAX_BYTES=10737418240
SNAPSHOT_ENCRYPTION_KEY=<32-byte-url-safe-base64-key>
```

Generate the encryption key once with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Store it in a secret manager or protected deployment environment, and keep a secure backup separate from the snapshot filesystem. Losing this key makes the snapshots permanently unrecoverable; changing it without re-encrypting existing snapshots has the same effect.

Both host directories must exist and already be mounted. Compose bind-mounts them into the broker and worker using the same absolute paths. The snapshot filesystem is separate so snapshot retention cannot consume the live workspace filesystem's capacity. Compose also mounts a persistent `worker_state` volume into the broker and worker; the broker initializes it as UID/GID `10002:10001`, mode `0700`. If custom paths are selected, use those exact paths in .env.

## Provisioning contract

For each workspace:

1. The worker validates the workspace ID and requests a storage operation using only the workspace ID and a bounded quota multiplier.
2. The broker verifies the storage root is itself a mountpoint, is XFS, and has project quota enforcement enabled. A directory merely located somewhere on an XFS filesystem is rejected.
3. The broker allocates a deterministic 31-bit XFS project ID. A persistent SQLite registry in the broker-only `broker_state` volume enforces uniqueness and maps the workspace ID, project ID, and directory.
4. The broker initializes the XFS project tree once with the `xfs_quota project -s` operation and records that initialization in the persistent registry.
5. The broker reapplies `bhard` and `ihard` limits before each start/resume, without recursively walking an already initialized tree.
6. The broker sets the directory owner to UID/GID `10001:10001` and mode `2770`; only the workspace's own directory is bind-mounted at `/workspace`.

The project registry is stored in the broker-only `broker_state` Docker volume, outside both XFS filesystems and inaccessible to the worker. It maps workspace IDs to project IDs and paths and records each directory's device/inode identity. A one-time migration copies the legacy `.devbox-storage.sqlite3` registry into broker-owned state. Missing or replaced registered directories fail closed instead of being silently recreated; restore them from a snapshot. The registry is required state: if it is missing while other storage data exists, provisioning fails closed. Back up the `broker_state` volume together with both XFS filesystems and the snapshot encryption key. Do not manually delete or edit it.

The API's storage_ref is an opaque backend reference, not a Docker volume name or a credential.

## Lifecycle

- **Stop / pause:** keep the directory, project ID, quota, and data.
- **Start / resume:** reuse the same directory and reapply its quota configuration before attaching it.
- **Restore:** require a stopped workspace, decrypt and validate into a quota-bound staging tree, swap it into place, and refresh the registered directory identity.
- **Delete:** remove the workspace container first, clear the XFS project tree and quota, remove the directory, and release its registry entry.
- **Legacy named-volume workspace:** the worker refuses to start an existing container whose /workspace mount does not match the XFS directory. Its old Docker volume is not automatically deleted or migrated.

## Encrypted snapshots

Snapshots are stored on the separate XFS snapshot filesystem, in a separate per-workspace project-quota directory. Each archive is streamed through AES-256-GCM encryption; the workspace ID and snapshot ID are authenticated as additional data. The metadata registry records the encrypted file's SHA-256 digest, timestamp, and size. Default policy is at most 10 snapshots per workspace and a 10 GiB maximum plaintext archive per snapshot, subject to the separate 20 GiB per-workspace snapshot quota.

- `POST /api/v1/workspaces/{workspace_id}/snapshots` creates a snapshot. If its container is running, the executor pauses it while archiving and then unpauses it.
- `GET /api/v1/workspaces/{workspace_id}/snapshots` lists snapshots owned by the authenticated principal.
- `POST /api/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}/restore` restores a snapshot. The workspace must be stopped; a running or paused workspace is rejected.
- `DELETE /api/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}` deletes one snapshot.
- Deleting a workspace also deletes its stored snapshots.

Restore decrypts into a separate staging directory assigned the same XFS project ID as the workspace. During the operation, the worker asks the broker to temporarily raise that project's block and inode limits to three times their configured values so the old and staged trees can coexist. The worker validates every archive path, rejects absolute paths, parent traversal, special files, hard links, and escaping symlinks, verifies the AES-GCM authentication tag, and writes group-accessible files into the staging tree. It then asks the broker to atomically swap the staging tree into place, refresh the registered directory identity, remove the old tree, fsync the mount root, and reapply the normal limits. The worker cannot rename directories at the XFS mount root. The host filesystem must have enough free space for both trees; the temporary quota increase does not create physical capacity. Interrupted swaps are recovered through a typed broker operation.

The broker's `broker_state` volume contains the XFS project registries. The `worker_state` volume contains the snapshot metadata database at `/var/lib/devbox-worker/snapshots.sqlite3`. Back up both named volumes together with the workspace and snapshot filesystems. On upgrade, the broker grants read-only group access to a legacy `.devbox-snapshots.sqlite3` file so the worker can migrate it into `worker_state`; the legacy file is retained until an operator verifies the migration and backup. Keep the encryption key separately. Snapshots protect confidentiality at rest but do not guarantee that repository files contain no credentials: any credentials already written inside the workspace will be included in its encrypted snapshot. The worker necessarily handles plaintext while creating/restoring archives; its host and snapshot mount must be trusted.

## Limits and boundaries

- The block quota applies to data stored beneath /workspace.
- The inode quota limits file and directory count, reducing small-file exhaustion.
- /tmp and /run are separate bounded tmpfs mounts (64 MiB and 16 MiB respectively); they are not part of the workspace's XFS block quota.
- Container CPU, memory, PID, and network restrictions remain independent controls.
- Snapshot create/list/restore/delete and periodic unsafe-mount reconciliation are implemented. Backup/restore drills and real-host integration verification are still pending. Opt-in XFS quota and Docker persistence/isolation tests exist but have not yet been verified on a dedicated Linux host.
- This backend is not supported on Docker Desktop's ordinary VM filesystem. Use Docker Desktop for API development and mocked tests; use a Linux XFS host to validate actual workspace provisioning.

## Operational cautions

The worker has both Docker socket access and CAP_SYS_ADMIN; compromise of this service can compromise the host. Keep it private, authenticate all internal requests, restrict who can deploy or modify it, and monitor its logs. This is a trusted infrastructure component, not an untrusted workspace.

Do not call this container-level isolation a VM boundary. For hostile multi-tenant execution, evaluate microVM isolation before production exposure.


## Recovery and integration verification

The worker service is configured to restart unless explicitly stopped. It runs as UID/GID `10002:10001` with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, bounded tmpfs mounts, and the dedicated `worker_state` volume. Workspace containers are independently managed by Docker and use a deterministic name plus a restart policy. A periodic worker reconciliation loop (60 seconds by default) scans managed containers, verifies their labels, deterministic names, assigned XFS directory, and /workspace bind mount, and stops running containers whose storage no longer matches. It also recovers interrupted snapshot directory swaps before validating mounts. It never auto-starts or deletes containers.

The API runs a separate periodic lifecycle reconciler (60 seconds by default) for workspaces recorded as running, paused, or recovering. It queries the authenticated executor status endpoint, validates the workspace identity and storage binding, and synchronizes PostgreSQL state with the observed container state. Changes are written to the audit log. The API reconciler uses a row lock and compares the state observed before the network request so a delayed poll cannot overwrite a newer user-initiated lifecycle operation. If the executor is unavailable or rejects the request, the API leaves the database state unchanged. This control-plane reconciler is separate from the worker's host safety loop.

The worker serializes lifecycle, snapshot, deletion, and reconciliation operations with an in-process lock per workspace. The Compose worker runs one Uvicorn process. Do not scale the worker to multiple processes or replicas until these locks are replaced with a shared distributed lock.

Optional integration tests:

```bash
# Use an empty, dedicated XFS test mount; run with permissions to manage XFS quotas.
DEVBOX_TEST_XFS_ROOT=/mnt/devbox-xfs-test pytest tests/integration/test_xfs_quota.py

# Verify encrypted snapshots across two dedicated XFS mounts.
DEVBOX_TEST_XFS_ROOT=/mnt/devbox-workspace-test \
DEVBOX_TEST_SNAPSHOT_XFS_ROOT=/mnt/devbox-snapshot-test \
DEVBOX_TEST_SNAPSHOT_ENCRYPTION_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
pytest tests/integration/test_snapshot_storage.py

# Also verify real Docker bind-mount isolation and persistence.
DEVBOX_TEST_XFS_ROOT=/mnt/devbox-xfs-test \
DEVBOX_RUN_DOCKER_INTEGRATION=1 \
pytest tests/integration/
```

These tests are opt-in because they modify filesystem quota state and create Docker containers. Never point them at a filesystem containing valuable data or live workspaces.


## Security audit follow-up

The current worker requires Docker socket access and `CAP_SYS_ADMIN` to manage containers and XFS project quotas. This is a host-privileged trust boundary: compromise of the worker may compromise the host and every workspace. Keep this service private and tightly controlled until the planned narrow host broker is implemented and independently tested.

The broker must accept only workspace-scoped typed operations and derive storage paths/project IDs from trusted registry state. It must not accept arbitrary host paths, arbitrary Docker API payloads, or shell commands. This is a remediation target; it is not implemented by this document update.

Real XFS/Docker integration, adversarial snapshot archive tests, interrupted-restore drills, and registry/key recovery exercises remain required. See [docs/PHASE_2_SECURITY_HARDENING.md](docs/PHASE_2_SECURITY_HARDENING.md).


**PID namespace note:** Docker uses a private PID namespace by default. Do not pass `pid_mode="private"` to the Docker API; that value is invalid. DevBox now relies on the default (unset) PID mode and verifies process isolation in the Docker integration test.


## Privilege boundary

- **Broker:** runs with Docker socket access and `CAP_SYS_ADMIN`; it owns the XFS project registries and is the only service that runs `findmnt`/`xfs_quota` operations. Its API exposes fixed workspace-scoped operations and bounded quota multipliers, not arbitrary host paths or Docker API passthrough.
- **Worker:** runs as UID/GID 10001, has no Docker SDK/socket, no XFS tools, no `CAP_SYS_ADMIN`, and no Linux capabilities. Its root filesystem is read-only. It can access the shared workspace and snapshot mounts as UID 10001 so it can read, write, and atomically restore workspace files.
- **Residual trust:** the worker can access every workspace directory on the shared mounts. The current design reduces host-level privilege but does not protect tenant data from a compromised worker. Per-workspace host identities or microVMs remain necessary for hostile multi-tenant guarantees.
