# DevBox Workspace Storage

## Decision

Workspace data uses **one directory tree per workspace on a dedicated Linux XFS filesystem with project quotas enabled**. The worker uses xfs_quota to assign a project ID and enforce both a hard block limit and a hard inode limit.

This is the selected production backend. The ordinary Docker local named-volume driver is not used for workspace data because it does not provide a portable hard per-volume quota.

## Host requirements

- Linux host with an XFS filesystem dedicated to DevBox workspace data.
- The filesystem mounted with prjquota (or pquota).
- Project quota accounting and enforcement both enabled.
- xfsprogs and findmnt available in the worker image.
- The host storage path mounted into the worker at the **same absolute path** used by the Docker daemon.
- The worker runs as a trusted privileged service with CAP_SYS_ADMIN and access to the Docker socket.

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

The last command must show the exact mountpoint, xfs, and prjquota or pquota.

Configure:

```dotenv
WORKSPACE_STORAGE_ROOT=/srv/devbox/workspaces
WORKSPACE_DISK_QUOTA=10g
WORKSPACE_INODE_LIMIT=200000
```

The host directory must exist and already be mounted. Compose bind-mounts it into the worker using the same path. If a different path is selected, use that exact path in .env.

## Provisioning contract

For each workspace:

1. Validate the workspace ID and derive a stable storage directory name from its SHA-256 digest.
2. Verify the storage root is itself a mountpoint, is XFS, and has project quota enforcement enabled. A directory merely located somewhere on an XFS filesystem is rejected.
3. Allocate a deterministic 31-bit XFS project ID. A persistent SQLite registry enforces uniqueness and maps the workspace ID, project ID, and directory.
4. Initialize the XFS project tree with the xfs_quota project -s operation.
5. Apply bhard and ihard limits before the directory is mounted into the workspace container.
6. Set the directory owner to UID/GID 10001, then bind-mount only that directory at /workspace.

The registry is stored on the dedicated storage filesystem, outside individual workspace directories. It is required state: if it is missing while other storage data exists, provisioning fails closed. Back up the registry together with the workspace filesystem. Do not manually delete or edit it.

The API's storage_ref is an opaque backend reference, not a Docker volume name or a credential.

## Lifecycle

- **Stop / pause:** keep the directory, project ID, quota, and data.
- **Start / resume:** reuse the same directory and reapply its quota configuration before attaching it.
- **Delete:** remove the workspace container first, clear the XFS project tree and quota, remove the directory, and release its registry entry.
- **Legacy named-volume workspace:** the worker refuses to start an existing container whose /workspace mount does not match the XFS directory. Its old Docker volume is not automatically deleted or migrated.

## Limits and boundaries

- The block quota applies to data stored beneath /workspace.
- The inode quota limits file and directory count, reducing small-file exhaustion.
- /tmp and /run are separate bounded tmpfs mounts (64 MiB and 16 MiB respectively); they are not part of the workspace's XFS block quota.
- Container CPU, memory, PID, and network restrictions remain independent controls.
- Snapshot/restore, quota-aware backup/restore drills, and real Docker integration tests are still pending.
- This backend is not supported on Docker Desktop's ordinary VM filesystem. Use Docker Desktop for API development and mocked tests; use a Linux XFS host to validate actual workspace provisioning.

## Operational cautions

The worker has both Docker socket access and CAP_SYS_ADMIN; compromise of this service can compromise the host. Keep it private, authenticate all internal requests, restrict who can deploy or modify it, and monitor its logs. This is a trusted infrastructure component, not an untrusted workspace.

Do not call this container-level isolation a VM boundary. For hostile multi-tenant execution, evaluate microVM isolation before production exposure.
