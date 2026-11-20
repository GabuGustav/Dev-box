# DevBox — Product Specification

## 1. Purpose

DevBox is a persistent, model-agnostic development and testing platform that gives AI agents controlled access to isolated software workspaces. An agent should be able to clone or connect a repository, inspect and edit files, install dependencies, execute builds and tests, inspect results, and prepare changes for review.

A website alone is not sufficient: DevBox must expose a documented tool interface (initially an MCP-compatible server, with a stable HTTP API underneath) so connected AI clients can perform operations.

## 2. Product principles

1. **Isolation by default:** every agent receives its own workspace and execution boundary.
2. **Explicit sharing:** access to another agent's workspace is denied unless the owner grants it.
3. **Persistence:** workspaces survive chat closure, agent disconnection, browser closure, and service restarts.
4. **Discoverability:** a repository URL can find registered workspaces; a stable workspace ID selects one exact workspace.
5. **Workspace is the source of truth:** current files, Git state, environment metadata, logs, and artifacts matter more than chat history.
6. **Controlled collaboration:** agents exchange work through explicit permissions and Git operations.
7. **Model independence:** core execution and storage do not depend on one AI vendor.
8. **CPU-first operation:** builds and ordinary tests must work without a dedicated GPU. GPU-backed runtime testing is optional and out of the initial MVP.
9. **Human control:** destructive actions, credential use, external publishing, and permission changes require appropriate authorization.

## 3. Actors

- **Workspace owner:** user or agent that creates and controls a workspace.
- **Agent:** AI client operating in its own workspace or using explicitly granted access.
- **User:** human who configures projects, grants permissions, and approves consequential actions.
- **Platform administrator:** operates DevBox infrastructure and enforces platform-wide policy.

An agent identity must be authenticated and distinct from a chat/session identifier.

## 4. Workspace identity and repository discovery

Every workspace receives an immutable, globally unique ID (for example, `dbx_<random-id>`). The ID identifies the workspace, not the repository or chat.

A repository may have many workspaces. A normalized Git remote URL is therefore a **repository lookup key**, not a unique workspace key. On receiving a repository URL, DevBox should:
- Resolve and normalize the repository identity.
- List workspaces the requesting user/agent is authorized to discover.
- Show relevant metadata (owner, branch, last activity, status, last commit).
- Let the agent reconnect to a selected workspace or create a new one.
- Never silently choose between ambiguous workspaces.

Workspace access must be checked on every operation. Knowing a workspace ID is not authorization.

## 5. Isolation and sharing

- A newly created workspace is private to its owner.
- Each agent's default workspace has separate filesystem, processes, environment variables, dependency state, resource limits, and execution identity.
- No shared writable filesystem between agents by default.
- Sharing is explicit, revocable, and scoped to one workspace.
- Initial permission levels: `read`, `write`, and `admin`. The owner retains control; an ordinary collaborator cannot transfer ownership or grant further access unless explicitly allowed.
- Permission revocation must block subsequent operations and terminate or invalidate active sessions where necessary.
- Shared work must still use Git branches/commits or another explicit change-integration mechanism. Shared filesystem access is not a substitute for change review.

## 6. Persistence

Persist at minimum:
- Workspace ID, owner, authorized principals, lifecycle state, creation and activity timestamps.
- Repository remote, selected branch, current commit, and Git working-tree state.
- Workspace filesystem and durable user data.
- Environment/image configuration and dependency lock information.
- Build/test jobs, exit codes, logs, timestamps, and artifact references.
- Audit events for access, commands, permission changes, and Git operations.

Persistence must survive application and worker restarts. Use durable volumes or snapshots for workspace data and database backups for metadata. Define retention and deletion policies before production.

## 7. Core capabilities

### Workspace lifecycle
- Create, list, discover, reconnect, pause, resume, snapshot, restore, and delete workspaces.
- Clone a repository or attach an existing checkout.
- Select a branch and report repository/working-tree state.

### Development operations
- List/read/write files within the workspace.
- Run allowlisted or policy-checked commands in the workspace.
- Install dependencies using project-native tooling.
- Run build, test, lint, and static-analysis jobs.
- Stream job output and retrieve complete logs.
- Store and retrieve build artifacts.

### Git operations
- Inspect status, diff, branches, and recent commits.
- Create a branch, commit changes, fetch, and push when authorized.
- Prefer pull requests for cross-agent integration.
- Detect conflicts and never silently overwrite another agent's work.

### Agent integration
- Stable, versioned API and MCP tool surface.
- Authentication and per-tool authorization.
- Clear structured results, job IDs, exit codes, and log references.
- No reliance on hidden chat state.

## 8. Build and test model

MVP modes:
1. **Build environment:** compile and package software without launching the target application.
2. **Automated testing:** run unit, integration, and static checks in isolated jobs.
3. **Runtime testing:** later optional capability for applications that need a GUI, operating system, game installation, or GPU.

For example, compiling a GTA V C# mod can be possible with the correct .NET toolchain and framework reference assemblies without launching GTA V. A successful compile does not prove in-game behaviour. Runtime validation is a separate capability.

## 9. Non-functional requirements

- Strong isolation between workspaces and tenants.
- Resource quotas for CPU, memory, storage, process count, and execution time.
- Reliable job cancellation and cleanup.
- Observable operations with structured logs and audit trails.
- API versioning and backward-compatible tool contracts.
- Secure secret handling; credentials must not be written into logs or source files.
- Graceful handling of worker crashes and interrupted jobs.
- Clear status for queued, running, succeeded, failed, cancelled, and timed-out jobs.

## 10. MVP boundaries

The first release should support one host operating system for workers, containerized CPU-first workspaces, persistent storage, Git operations, command execution, builds, tests, logs, and MCP/API access.

Out of scope for the first MVP:
- GPU passthrough or shared-vGPU scheduling.
- Full desktop/GUI streaming.
- Launching commercial games.
- Arbitrary privileged containers or unrestricted host access.
- Automatic merging of concurrent agent changes.
- Multi-region high availability.

## 11. Acceptance criteria

The MVP is not complete until:
- Two agents can create separate workspaces for the same repository.
- One agent cannot read or modify the other's workspace without a grant.
- A new chat/client can locate an authorized existing workspace using the repository URL and reconnect using its workspace ID.
- Workspace files and Git state survive service and worker restarts.
- A build/test job returns a durable job record, exit status, and logs.
- A failed job can be inspected and rerun.
- Revoking shared access prevents further access.
- Git changes can be reviewed and integrated without silent overwrites.
