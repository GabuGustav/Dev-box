# DevBox

**Persistent, isolated development workspaces for AI agents.**

DevBox is a model-agnostic platform where AI agents can work with real repositories, edit code, install dependencies, run builds and tests, inspect logs, and prepare Git changes inside isolated execution environments.

## Current status

**Phase 2 implementation is in progress on `phase-2-isolated-workspaces`.** It adds a separate Docker executor service, one container and persistent XFS-backed directory per workspace, lifecycle endpoints, container resource limits, and owner-scoped control. Repository cloning and command execution are not implemented yet.

## Core principles

- One private workspace per agent by default.
- Explicit, revocable sharing with scoped permissions (planned).
- Stable workspace IDs independent of chats and browser sessions.
- Repository URL discovery, with explicit workspace selection.
- Controlled collaboration through Git branches and commits.
- Model-independent REST API and future MCP interface.
- CPU-first builds; GPU-backed runtime testing is optional and later.

A repository can have multiple workspaces. The repository URL is a discovery key; the workspace ID identifies one exact environment. Knowing an ID does not grant access.

## Documentation

- [Product specification](PROJECT_SPEC.md)
- [System architecture](ARCHITECTURE.md)
- [Implementation roadmap](ROADMAP.md)
- [Security and isolation](SECURITY.md)
- [Build and test system](BUILD_SYSTEM.md)
- [Workspace storage backend](STORAGE.md)

## Stack

- Python 3.12+
- FastAPI and SQLAlchemy 2
- PostgreSQL 16 and Alembic
- Docker SDK in a separate trusted executor service
- Pytest and Ruff
- Docker Compose for local development

## Run locally

1. For actual workspace provisioning, use a Linux host with Docker Engine. Docker Desktop on Windows/macOS can be used for API and mocked-test development, but it does not provide the required dedicated XFS host mount.
2. Prepare two dedicated Linux XFS filesystems: one for workspaces at `/srv/devbox/workspaces`, and one for encrypted snapshots at `/srv/devbox/snapshots`. Mount both with project quotas enabled (`prjquota` or `pquota`). Both mounts must exist before Compose starts and be visible at the same absolute paths inside the worker and to the Docker daemon. Do not format a disk that contains data; use dedicated data volumes and follow your host OS's XFS setup procedure.
3. Copy `.env.example` to `.env`.
4. Set `EXECUTOR_SERVICE_TOKEN` to a fresh random secret and `SNAPSHOT_ENCRYPTION_KEY` to a generated 32-byte URL-safe base64 key. For example:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   # For SNAPSHOT_ENCRYPTION_KEY:
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Keep the snapshot key backed up securely. Losing it makes existing snapshots unrecoverable.
5. Start the complete stack:

   ```bash
   docker compose up --build
   ```

   Compose starts PostgreSQL, the trusted executor supervisor, and the API. The API applies database migrations before serving requests. The executor builds the workspace base image on first startup.

6. Create a principal using the API container:

   ```bash
   docker compose exec api python -m app.cli create-principal --type user --name local-owner
   ```

   The API key is printed once. Store it securely.
7. Open [http://localhost:8000/docs](http://localhost:8000/docs).

## Workspace API

All workspace endpoints require the `X-DevBox-Key` header.

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/v1/workspaces` | Register a workspace (metadata only) |
| GET | `/api/v1/workspaces` | List the authenticated principal's workspaces |
| GET | `/api/v1/workspaces/{workspace_id}` | Retrieve an owned workspace |
| POST | `/api/v1/workspaces/{workspace_id}/start` | Create/start its isolated container and enforce its disk quota |
| POST | `/api/v1/workspaces/{workspace_id}/stop` | Stop its container, retaining its workspace data |
| POST | `/api/v1/workspaces/{workspace_id}/pause` | Pause a running container |
| POST | `/api/v1/workspaces/{workspace_id}/resume` | Resume a paused/stopped container |
| DELETE | `/api/v1/workspaces/{workspace_id}` | Remove its container, workspace data, and snapshots |
| POST | `/api/v1/workspaces/{workspace_id}/snapshots` | Create an encrypted snapshot |
| GET | `/api/v1/workspaces/{workspace_id}/snapshots` | List snapshots |
| POST | `/api/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}/restore` | Restore a snapshot while the workspace is stopped |
| DELETE | `/api/v1/workspaces/{workspace_id}/snapshots/{snapshot_id}` | Delete a snapshot |

Example start request:

```bash
curl -X POST http://localhost:8000/api/v1/workspaces/dbx_YOUR_ID/start \
  -H "X-DevBox-Key: YOUR_API_KEY"
```

Workspace creation remains separate from provisioning. A workspace receives a stable ID first; calling `start` provisions its container and an XFS project-quota directory. Stopping a container retains its directory. Deleting a workspace removes its container and directory, then releases its project quota.

## Isolation model and current limits

The API does not mount the Docker socket. A separate trusted worker service controls Docker and is the only service with access to the host Docker socket. Workspace containers receive only their own dedicated bind-mounted directory on the XFS workspace filesystem. The worker also receives `CAP_SYS_ADMIN` to manage XFS project quotas; this makes it a highly privileged component.

Workspace containers currently use:
- A dedicated container and directory-tree project quota per workspace.
- A non-root UID, read-only container root filesystem, and writable workspace mount.
- No Linux capabilities and `no-new-privileges`.
- CPU, memory, and PID limits.
- No network interface (`network_mode=none`) by default.
- Bounded writable tmpfs mounts for temporary runtime files.

This is container-level isolation, not a VM boundary. The worker supervisor is highly privileged and must remain a trusted internal service. Never expose its port publicly or mount the Docker socket into workspace containers.

**Storage requirements:** the worker refuses to provision a workspace unless the configured storage root is on XFS with project quota accounting and enforcement enabled. The worker uses `xfs_quota` to assign a unique project ID, a hard block limit, and a hard inode limit before mounting the directory into a workspace container. A SQLite registry prevents two workspaces from accidentally sharing a project ID. The workspace storage root must be mounted into the worker at the same absolute path used by the Docker daemon. Docker Desktop and non-XFS local filesystems are not supported for actual workspace provisioning; use them for API/unit-test development only.

**Known Phase 2 gaps:** quota-aware backup/restore drills, adversarial host-escape tests, and real-host integration verification remain incomplete. A periodic worker safety loop checks managed container mounts and stops containers whose storage no longer matches; it does not synchronize lifecycle status back into the API database. Encrypted snapshot create/list/restore/delete endpoints are implemented; they require a second dedicated XFS snapshot mount and a persistent encryption key. The current network isolation prevents repository cloning and dependency downloads; controlled egress will be designed with the Git integration phase. Do not treat this branch as production-hardened multi-tenant infrastructure.

## Development checks

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

The API tests mock the executor service, and executor unit tests mock Docker SDK resources; neither requires a running Docker daemon. Kernel quota tests require `DEVBOX_TEST_XFS_ROOT` to point at an empty dedicated XFS test mount. Docker persistence tests additionally require `DEVBOX_RUN_DOCKER_INTEGRATION=1` and a Linux Docker host.

## Next

Complete database lifecycle reconciliation, Docker integration/isolation tests, and backup/restore drills before considering Phase 2 complete. Phase 3 will add repository cloning, authorized repository discovery, and Git operations.
