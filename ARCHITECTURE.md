# DevBox — System Architecture

## 1. Architecture goals

Build a secure, persistent, model-agnostic execution platform. Keep the control plane separate from the execution plane. Start with a modular monolith and isolated workers rather than introducing microservices before they are needed.

## 2. High-level components

```text
AI clients (ChatGPT / other agents)       Human web application
                 |                                |
          MCP-compatible server              HTTPS API
                 |                                |
                 +----------- API gateway --------+
                                  |
                         Authentication / RBAC
                                  |
                    Control plane (application)
                    |          |          |
                Workspace   Job/queue   Git integration
                 manager    scheduler      service
                    |          |          |
                    +------ Metadata DB ---+
                               |
                         Durable storage
                               |
                 Worker supervisor / executor
                               |
                  Isolated workspace containers
                  |             |             |
               Agent A       Agent B       Agent C
              workspace     workspace     workspace
                  |             |             |
               CPU/memory/storage/network limits
```

## 3. Control plane

Responsibilities:
- Authenticate users and agents.
- Authorize every workspace and job operation.
- Create and manage workspace records.
- Resolve repository URLs to repository records and authorized workspace lists.
- Issue short-lived execution credentials.
- Queue jobs and track their lifecycle.
- Store metadata, audit events, logs references, and artifact references.
- Expose a versioned REST API and MCP tools.

The control plane must not execute arbitrary repository code inside the API process.

## 4. Execution plane

A worker supervisor provisions and controls isolated execution environments. The initial implementation should use rootless containers where practical, with hardened container settings and a dedicated non-root identity.

Each workspace has:
- A unique workspace ID and isolated filesystem/volume.
- A dedicated container or execution boundary.
- Separate process namespace and environment.
- CPU, memory, storage, process, and wall-clock limits.
- Network policy.
- A configured base image/toolchain.
- Durable workspace data mounted only into that workspace's executor.

Do not mount the Docker socket or host root filesystem into untrusted build containers. Do not run repository scripts as the platform service account.

For stronger multi-tenant isolation, evaluate microVMs (such as Firecracker) or dedicated VMs after the container MVP is validated. Containers alone are not a complete security boundary against a determined kernel-level escape.

### Current Phase 2 execution boundary

The current deployment separates Docker/XFS administration from general worker operations:

- **Privileged broker:** the only service with the Docker socket and `CAP_SYS_ADMIN`. It creates/removes containers, prepares the persistent state volumes, and manages XFS project quotas and restore swaps. Its private `broker_state` volume contains the project registries and a `broker.lock` advisory lock.
- **Rootless worker:** runs as UID/GID `10002:10001`, with all Linux capabilities dropped, no Docker socket, a read-only root filesystem, and an authenticated private connection to the broker. It holds `worker_state/worker.lock` before opening the snapshot registry. The broker prepares this volume with restricted ownership and permissions.
- **Workspace containers:** run as UID/GID `10001:10001`, receive only their own workspace directory at `/workspace`, use read-only image roots, drop all capabilities, set no-new-privileges, and have no network by default.
- **Git helper containers:** are short-lived and receive only the selected workspace bind mount. They restrict Git to HTTPS, disable repository-local executable configuration such as fsmonitor and credential helpers, and use the private allowlisted Squid proxy for network-enabled operations.

The broker and worker are single-instance per deployment. Both Uvicorn processes use one worker, and advisory locks prevent a second process/container sharing the same state volume from starting. Do not scale these services or run separate deployments against the same XFS roots. Workspace operation locks are process-local; multi-replica execution requires a distributed locking design.

Workspace and Git helper containers inherit Docker's default seccomp profile and the `docker-default` AppArmor profile when AppArmor is enabled. Integration tests verify the host-reported runtime protections and reject unconfined settings. Docker user-namespace remapping is not enabled: the subordinate UID/GID mapping and bind-mount ownership requirements must first be reconciled with the XFS storage contract.

For the detailed threat boundary and current residual risk, see [SECURITY.md](SECURITY.md), [STORAGE.md](STORAGE.md), and [Phase 2.3 Adversarial Hardening](docs/PHASE_2_3_ADVERSARIAL_HARDENING.md).

## 5. Persistence and data model

Use a relational database (PostgreSQL is a suitable initial choice) for metadata and object/durable volume storage for workspace data and artifacts.

Core records:
- `users`
- `agents`
- `repositories` — normalized remote identity and provider metadata.
- `workspaces` — immutable ID, owner, repository, branch, status, executor reference, timestamps.
- `workspace_grants` — principal, workspace, permission, grantor, expiry/revocation.
- `jobs` — workspace, requested operation, state, limits, timestamps, exit code.
- `job_logs` — chunk references or bounded log records.
- `artifacts` — job/workspace, storage key, digest, size, media type.
- `snapshots` — workspace version, storage reference, creation time.
- `audit_events` — actor, action, target, timestamp, result, request ID.

Repository URL resolution must not imply workspace identity. A repository can map to multiple workspace records, branches, owners, and agents.

## 6. Workspace lifecycle

Suggested states:
- `provisioning`
- `ready`
- `busy`
- `paused`
- `recovering`
- `failed`
- `deleting`
- `deleted`

Creation flow:
1. Authenticate requester.
2. Resolve repository and verify access/credentials.
3. Create immutable workspace ID and metadata record.
4. Provision isolated storage and execution boundary.
5. Clone repository into the workspace.
6. Record remote, branch, commit, and environment metadata.
7. Mark ready and return workspace ID.

Reconnect flow:
1. Authenticate new client/agent.
2. Resolve normalized repository URL.
3. List only workspaces discoverable by that principal.
4. User/agent selects a workspace or supplies its ID.
5. Authorize requested permission.
6. Start or attach to executor; inspect Git and workspace state.
7. Return a compact state report and available job history.

## 7. Job execution lifecycle

1. Validate workspace access and operation permissions.
2. Validate command against policy and workspace configuration.
3. Reserve resource quota.
4. Create durable job record.
5. Dispatch to worker.
6. Stream bounded logs and update heartbeat.
7. Enforce timeout, cancellation, output-size, and resource limits.
8. Capture exit code, logs, artifacts, and final Git status.
9. Release resources and mark terminal state.

Jobs must be idempotent where possible. A worker crash should leave a recoverable job state rather than a false success.

## 8. Agent API and MCP surface

Initial tool groups:
- Workspace: `create_workspace`, `list_workspaces`, `find_workspaces_by_repository`, `get_workspace`, `reconnect_workspace`, `pause_workspace`, `resume_workspace`, `snapshot_workspace`, `delete_workspace`.
- Files: `list_files`, `read_file`, `write_file`, `search_files`.
- Execution: `run_command`, `cancel_job`, `get_job`, `get_job_logs`.
- Build/test: `run_build`, `run_tests`, `run_lint`, `list_artifacts`, `get_artifact`.
- Git: `git_status`, `git_diff`, `git_branch`, `git_commit`, `git_push`, `git_pull`, `create_pull_request`.
- Sharing: `grant_workspace_access`, `list_workspace_access`, `revoke_workspace_access`.

Every tool call must carry an authenticated principal and be authorized server-side. Never trust a client-supplied owner ID or agent ID.

## 9. GitHub integration

Use least-privilege OAuth or GitHub App installation permissions. Prefer short-lived tokens and repository-scoped access. Keep tokens in a secret manager or encrypted credential store, not in workspace files or logs.

Support private repositories only when the user has explicitly connected an authorized GitHub identity. A public repository URL alone must not grant write permission.

## 10. Deployment topology

MVP:
- Web/API service.
- MCP service (may share the same application deployment initially).
- PostgreSQL.
- Durable volume/object storage.
- Job queue.
- Worker supervisor and one or more CPU-first worker hosts.
- Reverse proxy/TLS termination.
- Centralized logs and metrics.

The user's laptop should be only an AI client/browser. Builds run on the DevBox worker host, so the laptop's CPU/GPU is not required for compilation.

## 11. Scaling path

1. Single-node development deployment.
2. Separate API and worker processes.
3. Multiple worker hosts with scheduler and quotas.
4. Dedicated VM/microVM execution for higher-risk workloads.
5. Optional Windows workers and GPU-capable runtime workers for projects that require them.

## 12. Key design constraints

- Never use a repository URL as a secret or authorization token.
- Never infer workspace selection when multiple matches exist.
- Never expose host filesystem paths or infrastructure credentials to agents.
- Never allow a workspace to access another workspace's volume by default.
- Never treat a successful build as proof of runtime correctness.


## 13. Privileged execution boundary

The execution plane separates the API, an orchestration worker, a privileged host broker, and untrusted workspace containers.

1. **API/control plane:** authenticates principals, enforces workspace ownership, and calls the worker over its authenticated service API.
2. **Orchestration worker:** runs as UID/GID `10002:10001`, drops all Linux capabilities, enables `no-new-privileges`, and uses a read-only root filesystem. It has no Docker socket, Docker SDK, or XFS quota command access.
3. **Privileged host broker:** the only service with Docker socket access and `CAP_SYS_ADMIN`. It exposes typed workspace, Git-helper, and XFS storage operations over a private authenticated network. It derives paths, project IDs, images, mounts, and container configuration internally; it does not proxy arbitrary Docker API requests.
4. **Workspace runtime:** remains untrusted and receives only its own workspace directory. It never receives the Docker socket, broker credentials, worker state, broker state, or snapshot storage.

The broker owns the XFS project registries in its private `broker_state` volume. It prepares XFS mount roots as root-owned/group `10001`, mode `2750`, and per-workspace directories as `10001:10001`, mode `2770`. The worker's persistent snapshot metadata is stored separately in `worker_state`; legacy snapshot metadata is migrated read-only from the snapshot mount.

Snapshot archive reads/writes still occur in the worker. The worker therefore retains group-based read/write access to all workspace and snapshot directories on the shared XFS mounts. This reduces the consequences of worker compromise compared with a root/Docker-capable worker, but it does **not** provide hostile-tenant data isolation. A future per-workspace storage service or VM/microVM boundary is required to prevent a compromised worker from reading other tenants' data.

Snapshot restore directory swaps and recovery are broker operations because the worker is not allowed to write to the XFS mount root. The broker validates the derived workspace/staging/backup paths, performs the atomic rename sequence, refreshes the registered directory identity, resets quotas, and fsyncs the mount root.

The Compose deployment keeps PostgreSQL internal and binds the API to host loopback by default. Production deployment must terminate TLS at a trusted reverse proxy before exposing the API externally. See [DEPLOYMENT.md](DEPLOYMENT.md) and [docs/PHASE_2_2_3_ROOTLESS_WORKER.md](docs/PHASE_2_2_3_ROOTLESS_WORKER.md).

**PID namespace note:** the workspace runtime relies on Docker's default private PID namespace. The Docker API does not accept `pid_mode="private"`; omitting the setting selects the private default. The Linux integration test verifies that one workspace cannot see another workspace's process marker.
