# DevBox

**Persistent, isolated development workspaces for AI agents.**

DevBox is a model-agnostic platform where AI agents can work with real repositories, edit code, install dependencies, run builds and tests, inspect logs, and prepare Git changes inside isolated execution environments.

## Current status

**Phase 2 security hardening is continuing on `phase-2-rootless-worker-hardening`, based on successful base commit `088364d` (CI run 45868184349).** Docker lifecycle and XFS quota administration are broker-owned; the worker has no Docker socket, Docker SDK, or `CAP_SYS_ADMIN`. This branch separates the worker identity (UID/GID `10002:10001`) from the workspace runtime (UID/GID `10001:10001`), makes XFS mount roots non-writable to the worker, moves snapshot metadata into a dedicated state volume, and delegates restore directory swaps to the broker. The branch changes are awaiting CI verification. DevBox supports public HTTPS repository cloning through an ephemeral Git helper, owner-scoped Git status/diff/branch/commit/fetch APIs, and a restricted egress proxy. General command/job execution, private-repository credentials, and Git push are not implemented yet.

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
- [Secure deployment](DEPLOYMENT.md)
- [Phase 2 security hardening plan](docs/PHASE_2_SECURITY_HARDENING.md)
- [Rootless worker implementation plan](docs/PHASE_2_2_3_ROOTLESS_WORKER.md)

## Stack

- Python 3.12+
- FastAPI and SQLAlchemy 2
- PostgreSQL 16 and Alembic
- Docker SDK in a separate privileged broker; worker uses authenticated HTTP RPC
- Pytest and Ruff
- Docker Compose for local development

## Run locally

1. For actual workspace provisioning, use a Linux host with Docker Engine. Docker Desktop on Windows/macOS can be used for API and mocked-test development, but it does not provide the required dedicated XFS host mount.
2. Prepare two dedicated Linux XFS filesystems: one for workspaces at `/srv/devbox/workspaces`, and one for encrypted snapshots at `/srv/devbox/snapshots`. Mount both with project quotas enabled (`prjquota` or `pquota`). Both mounts must exist before Compose starts and be visible at the same absolute paths inside the broker and worker and to the Docker daemon. Do not format a disk that contains data; use dedicated data volumes and follow your host OS's XFS setup procedure.
3. Copy `.env.example` to `.env`.
4. Set `DB_PASSWORD` to a unique URL-safe random secret, `EXECUTOR_SERVICE_TOKEN` and `BROKER_SERVICE_TOKEN` to separate fresh random secrets, and `SNAPSHOT_ENCRYPTION_KEY` to a generated 32-byte URL-safe base64 key. For example:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   # For EXECUTOR_SERVICE_TOKEN:
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   # For BROKER_SERVICE_TOKEN:
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   # For SNAPSHOT_ENCRYPTION_KEY:
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Keep the snapshot key backed up securely. Losing it makes existing snapshots unrecoverable.
5. Start the complete stack:

   ```bash
   docker compose up --build
   ```

   Compose starts PostgreSQL, the privileged broker, the worker, and the API. The API applies database migrations before serving requests. The broker builds the workspace base image on first startup.

6. Create a principal using the API container:

   ```bash
   docker compose exec api python -m app.cli create-principal --type user --name local-owner
   ```

   The API key is printed once. Store it securely.
7. Open [http://localhost:8000/docs](http://localhost:8000/docs). The API binds to localhost by default. PostgreSQL is not published to the host. Never expose the plain-HTTP API directly to the internet; configure TLS termination as described in [DEPLOYMENT.md](DEPLOYMENT.md).

## Workspace API

All workspace endpoints require the `X-DevBox-Key` header.

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/v1/workspaces` | Register a workspace (metadata only) |
| GET | `/api/v1/workspaces` | List the authenticated principal's workspaces |
| GET | `/api/v1/workspaces/discover?repository_url=...` | Find only the authenticated principal's workspaces for a normalized repository URL |
| GET | `/api/v1/workspaces/{workspace_id}` | Retrieve an owned workspace |
| POST | `/api/v1/workspaces/{workspace_id}/clone` | Clone its registered public repository and selected branch through the restricted proxy |
| GET | `/api/v1/workspaces/{workspace_id}/git/status` | Read branch and working-tree status |
| GET | `/api/v1/workspaces/{workspace_id}/git/diff` | Read staged and unstaged changes |
| POST | `/api/v1/workspaces/{workspace_id}/git/branches` | Create and select a branch |
| POST | `/api/v1/workspaces/{workspace_id}/git/commit` | Commit already staged files, optionally staging explicit relative paths |
| POST | `/api/v1/workspaces/{workspace_id}/git/fetch` | Fetch remote updates without merging |
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

The API and worker do not mount the Docker socket. A separate privileged broker controls Docker and XFS quota administration and is the only service with access to the host Docker socket and `CAP_SYS_ADMIN`. The worker communicates with it over a private authenticated network. The worker runs as UID/GID `10002:10001` with all capabilities dropped, `no-new-privileges`, and a read-only root filesystem. XFS mount roots are root-owned/group `10001`, mode `2750`; per-workspace directories are `10001:10001`, mode `2770`. Snapshot metadata is stored in a separate `worker_state` volume. The worker still has group-based read/write access to every workspace and snapshot directory, so this reduces host privilege but does not provide hostile-tenant data isolation.

Workspace containers currently use:
- A dedicated container and directory-tree project quota per workspace.
- A non-root UID, read-only container root filesystem, and writable workspace mount.
- No Linux capabilities and `no-new-privileges`.
- CPU, memory, and PID limits.
- No network interface (`network_mode=none`) by default.
- Bounded writable tmpfs mounts for temporary runtime files.

This is container-level isolation, not a VM boundary. The broker is host-privileged and must remain a trusted internal service. The worker remains trusted for cross-workspace storage access until storage operations are scoped per workspace. Never expose broker or worker ports publicly, and never mount the Docker socket into workspace containers.

**Storage requirements:** the broker refuses to provision a workspace unless the configured storage root is on XFS with project quota accounting and enforcement enabled. The broker uses `xfs_quota` to assign a unique project ID, a hard block limit, and a hard inode limit before mounting the directory into a workspace container. Broker-owned SQLite registries prevent project-ID collisions. The broker and worker receive the XFS mounts at the same absolute paths used by the Docker daemon; the worker has file-I/O access only and cannot administer quotas or write to mount roots. Docker Desktop and non-XFS local filesystems are not supported for actual workspace provisioning; use them for API/unit-test development only.

**Known Phase 2 gaps:** extended quota-aware backup/restore drills, adversarial host-escape and cross-workspace tests, proxy bypass/DNS rebinding tests, and distributed concurrency remain incomplete. The five-test Linux XFS/Docker integration baseline passed in CI runs 45864319247 and 45868184349. A periodic worker safety loop checks managed container mounts and stops containers whose storage no longer matches. The API also polls the trusted executor every `LIFECYCLE_RECONCILE_INTERVAL_SECONDS` (default 60) for running, paused, or recovering workspaces, synchronizes authoritative container state into PostgreSQL, and records changes in the audit log. If the executor is unavailable, reconciliation leaves existing database state unchanged. Encrypted snapshot create/list/restore/delete endpoints are implemented; they require a second dedicated XFS snapshot mount and a persistent encryption key. Workspace containers remain offline by default. A separate, non-published Squid proxy and internal-only helper network provide allowlisted egress for temporary Git helper containers. Git clone and fetch run through that proxy; local status, diff, branch, and commit helpers use `network_mode=none`. Git helper containers use the same unprivileged workspace UID, read-only root filesystem, CPU/memory/PID limits, and only the selected workspace bind mount. Git operations currently support public HTTPS repositories on GitHub, GitLab, and Bitbucket. Private repository credentials and Git push are deliberately withheld until a short-lived credential broker is implemented. The commit API only commits already-staged files unless explicit relative paths are supplied; it never stages the entire workspace implicitly. Do not treat this branch as production-hardened multi-tenant infrastructure.

## Development checks

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

The API tests mock the executor service, and executor unit tests mock Docker SDK resources; neither requires a running Docker daemon. Kernel quota tests require `DEVBOX_TEST_XFS_ROOT` to point at an empty dedicated XFS test mount. Docker persistence tests additionally require `DEVBOX_RUN_DOCKER_INTEGRATION=1` and a Linux Docker host.

## Git workflow

After creating a workspace, call `POST /api/v1/workspaces/{workspace_id}/clone` to clone the repository and branch registered on that workspace. Then use the Git status, diff, branch, commit, and fetch endpoints listed above. Clone is safe to retry when the same repository and branch are already present. Fetch updates remote-tracking references only; it does not merge or rebase.

Commit accepts a JSON body such as:

```json
{"message": "Implement workspace feature", "paths": ["src/file.py"]}
```

Omit `paths` to commit only files already staged inside the workspace. Private-repository authentication and push are not available yet; they require a short-lived credential broker.

## Repository discovery

Use `GET /api/v1/workspaces/discover?repository_url=https://github.com/owner/repository` to reconnect to existing workspaces after a new chat or client session. The endpoint normalizes the repository URL and returns only workspaces owned by the authenticated principal; matching a repository never grants access to another agent's workspace.

## Next

Next: implement a short-lived credential broker for private repositories and Git push, then add merge/conflict handling and PR workflows. Phase 2 still requires real Docker/XFS integration runs, backup/restore drills, and adversarial isolation testing on a dedicated Linux host.
