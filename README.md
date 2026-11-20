# DevBox

**Persistent, isolated development workspaces for AI agents.**

DevBox is a model-agnostic platform where AI agents can work with real repositories, edit code, install dependencies, run builds and tests, inspect logs, and prepare Git changes inside isolated execution environments.

## Current status

**Phase 1 — Core platform skeleton is implemented on the `phase-1-core-platform` branch.** It provides the first authenticated API, metadata models, database migration, health checks, audit foundation, and CI tests. It does not yet provision containers or clone repositories.

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
- FastAPI
- SQLAlchemy 2
- PostgreSQL 16
- Alembic migrations
- Pytest and Ruff
- Docker Compose for local development

## Run locally

1. Install Docker and Python 3.12+.
2. Copy `.env.example` to `.env`.
3. Start PostgreSQL: `docker compose up -d db`.
4. Create and activate a virtual environment, then install dependencies:

   ```bash
   python -m venv .venv
   # Windows: .venv\\Scripts\\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -e ".[dev]"
   ```

5. Apply the database migration: `alembic upgrade head`.
6. Create a local user or agent identity:

   ```bash
   python -m app.cli create-principal --type user --name local-owner
   ```

   The API key is printed once. Store it securely.
7. Start the API: `uvicorn app.main:app --reload`.
8. Open [http://localhost:8000/docs](http://localhost:8000/docs).

For a containerized API, after creating `.env`, run `docker compose up --build`. The API container applies migrations before starting.

## Initial API

All workspace endpoints require the `X-DevBox-Key` header.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health/live` | Process liveness |
| GET | `/health/ready` | Database readiness |
| POST | `/api/v1/workspaces` | Register a metadata-only workspace |
| GET | `/api/v1/workspaces` | List the authenticated principal's workspaces |
| GET | `/api/v1/workspaces/{workspace_id}` | Retrieve an owned workspace |

Example:

```bash
curl http://localhost:8000/api/v1/workspaces \
  -H "X-DevBox-Key: YOUR_API_KEY"
```

Workspace creation records a repository identity and creates a unique `dbx_...` ID. At this stage it does not clone the repository or start an execution container. Workspace listing is owner-scoped; another principal receives no visibility into the workspace.

## Development commands

- `make install` — install app and development dependencies.
- `make db-up` — start PostgreSQL.
- `make migrate` — apply migrations.
- `make test` — run tests.
- `make lint` — run Ruff.
- `make dev` — run the API with reload.

## Next

Phase 2 adds isolated persistent workspace containers, storage lifecycle, resource limits, and cross-workspace isolation tests. Phase 3 adds repository cloning and URL-based workspace discovery.
