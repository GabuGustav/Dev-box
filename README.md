# DevBox

**Persistent, isolated development workspaces for AI agents.**

DevBox is a model-agnostic platform where AI agents can work with real repositories, edit code, install dependencies, run builds and tests, inspect logs, and prepare Git changes inside isolated execution environments.

## Current status

**Phase 2 implementation is in progress on `phase-2-isolated-workspaces`.** It adds a separate Docker executor service, one container and persistent named volume per workspace, lifecycle endpoints, container resource limits, and owner-scoped control. Repository cloning and command execution are not implemented yet.

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

## Stack

- Python 3.12+
- FastAPI and SQLAlchemy 2
- PostgreSQL 16 and Alembic
- Docker SDK in a separate trusted executor service
- Pytest and Ruff
- Docker Compose for local development

## Run locally

1. Install Docker Desktop with Linux containers enabled and Python 3.12+.
2. Copy `.env.example` to `.env`.
3. Replace `EXECUTOR_SERVICE_TOKEN` with a fresh random secret. For example:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

4. Start the complete stack:

   ```bash
   docker compose up --build
   ```

   Compose starts PostgreSQL, the trusted executor supervisor, and the API. The API applies database migrations before serving requests. The executor builds the workspace base image on first startup.

5. Create a principal using the API container:

   ```bash
   docker compose exec api python -m app.cli create-principal --type user --name local-owner
   ```

   The API key is printed once. Store it securely.
6. Open [http://localhost:8000/docs](http://localhost:8000/docs).

## Workspace API

All workspace endpoints require the `X-DevBox-Key` header.

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/v1/workspaces` | Register a workspace (metadata only) |
| GET | `/api/v1/workspaces` | List the authenticated principal's workspaces |
| GET | `/api/v1/workspaces/{workspace_id}` | Retrieve an owned workspace |
| POST | `/api/v1/workspaces/{workspace_id}/start` | Create/start its isolated container |
| POST | `/api/v1/workspaces/{workspace_id}/stop` | Stop its container, retaining its volume |
| POST | `/api/v1/workspaces/{workspace_id}/pause` | Pause a running container |
| POST | `/api/v1/workspaces/{workspace_id}/resume` | Resume a paused/stopped container |
| DELETE | `/api/v1/workspaces/{workspace_id}` | Remove its container and persistent volume |

Example start request:

```bash
curl -X POST http://localhost:8000/api/v1/workspaces/dbx_YOUR_ID/start \
  -H "X-DevBox-Key: YOUR_API_KEY"
```

Workspace creation remains separate from provisioning. A workspace receives a stable ID first; calling `start` provisions its container and named Docker volume. Stopping a container does not delete its volume. Deleting a workspace removes both and marks its metadata record as deleted.

## Isolation model and current limits

The API does not mount the Docker socket. A separate trusted worker service controls Docker and is the only service with access to the host Docker socket. Workspace containers receive only their own named volume.

Workspace containers currently use:
- A dedicated container and named volume per workspace.
- A non-root UID, read-only container root filesystem, and writable workspace mount.
- No Linux capabilities and `no-new-privileges`.
- CPU, memory, and PID limits.
- No network interface (`network_mode=none`) by default.
- Bounded writable tmpfs mounts for temporary runtime files.

This is container-level isolation, not a VM boundary. The worker supervisor is highly privileged and must remain a trusted internal service. Never expose its port publicly or mount the Docker socket into workspace containers.

**Known Phase 2 gaps:** Docker's default local named-volume driver does not enforce a portable hard disk quota. Snapshot/restore and automated cross-container filesystem attack tests are also not complete. The current network isolation prevents repository cloning and dependency downloads; controlled egress will be designed with the Git integration phase. Do not treat this branch as production-hardened multi-tenant infrastructure.

## Development checks

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

The API tests mock the executor service; they do not require a running Docker daemon. Container-level integration tests must run on a Docker-enabled host.

## Next

Complete disk quota enforcement, snapshot/restore, worker crash recovery, and Docker integration/isolation tests before considering Phase 2 complete. Phase 3 will add repository cloning, authorized repository discovery, and Git operations.
