# DevBox — Implementation Roadmap

This roadmap sequences the project so each phase produces a testable result. Estimates are intentionally omitted until the implementation stack and hosting budget are chosen.

## Phase 0 — Foundations

- [x] Define product principles and workspace isolation policy.
- [x] Define persistent workspace identity and repository discovery.
- [x] Define exclusive-by-default access with explicit sharing.
- [ ] Confirm initial deployment target and budget.
- [ ] Select implementation language/framework and supported worker OS.
- [ ] Define threat model and data retention policy.

**Exit criteria:** architecture and security decisions are sufficiently specific to implement.

## Phase 1 — Core platform skeleton

- [ ] Create repository structure, backend application, configuration, and development environment.
- [ ] Add database schema and migrations.
- [ ] Add user and agent authentication.
- [ ] Add workspace and repository metadata models.
- [ ] Implement health checks, structured logging, and audit event foundation.
- [ ] Add automated tests and CI.

**Exit criteria:** authenticated clients can create and list metadata-only workspaces.

## Phase 2 — Isolated persistent workspaces

- [ ] Implement worker supervisor and container provisioning.
- [ ] Enforce per-workspace filesystem and process isolation.
- [ ] Add CPU, memory, disk, process, and timeout limits.
- [ ] Attach durable volumes and verify restart recovery.
- [ ] Implement pause, resume, snapshot, and deletion lifecycle.
- [ ] Test that one workspace cannot access another workspace's files or processes.

**Exit criteria:** two agents can operate concurrently in separate persistent workspaces without cross-access.

## Phase 3 — Git and repository discovery

- [ ] Normalize repository URLs and store repository identities.
- [ ] Implement authorized repository lookup.
- [ ] Clone repositories into workspaces.
- [ ] Track remote, branch, commit, and dirty working-tree state.
- [ ] Add branch, diff, commit, fetch, and push operations.
- [ ] Add safe conflict handling and pull-request workflow.
- [ ] Test reconnect from a completely new client session.

**Exit criteria:** a new chat/client can provide a repository URL, discover authorized workspaces, select one, and continue from its actual state.

## Phase 4 — Build and test engine

- [ ] Implement queued jobs and worker execution.
- [ ] Stream and persist logs.
- [ ] Capture exit codes, duration, and resource usage.
- [ ] Add cancellation, timeout, retry policy, and crash recovery.
- [ ] Add project build/test profiles.
- [ ] Store artifacts and associate them with jobs.
- [ ] Add static analysis and test result summaries.

**Exit criteria:** an agent can build and test a repository, inspect a failure, edit code, rerun, and compare results.

## Phase 5 — Agent interfaces

- [ ] Publish versioned REST API.
- [ ] Implement MCP server and documented tool schemas.
- [ ] Add API authentication and per-tool authorization.
- [ ] Add human web interface for workspace listing, logs, jobs, and permissions.
- [ ] Test at least two different agent clients against the same API.

**Exit criteria:** model-independent agents can operate DevBox without manual file copying.

## Phase 6 — Sharing and collaboration

- [ ] Implement read, write, and admin grants.
- [ ] Add grant expiry and revocation.
- [ ] Enforce permissions on every API and executor operation.
- [ ] Implement owner-controlled sharing and audit history.
- [ ] Add explicit Git-based change exchange and conflict review.
- [ ] Test revoked access and concurrent writes.

**Exit criteria:** workspace owners can safely grant and revoke scoped access without weakening isolation.

## Phase 7 — Hardening and production readiness

- [ ] Threat model review and security testing.
- [ ] Evaluate rootless containers versus microVM isolation.
- [ ] Add backup/restore drills and disaster recovery.
- [ ] Add quotas, billing/usage accounting if required, and abuse controls.
- [ ] Add metrics, alerting, retention, and operational runbooks.
- [ ] Load-test concurrent jobs and workspace recovery.

**Exit criteria:** documented operational and security requirements are met.

## Later capabilities

- Windows build workers for Windows-specific toolchains.
- GUI/desktop sessions and remote interaction.
- Optional GPU-backed runtime testing.
- Specialized build images for game modding, mobile, embedded, and other ecosystems.
- Multi-region scheduling and team administration.

## First implementation target

Do not begin with GPU virtualization, game launching, or a complex microservice fleet. Prove the core promise first:

**Two isolated agents → persistent workspaces → repository URL discovery → new-session reconnection → build/test → inspect and fix.**
