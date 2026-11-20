# DevBox — Security and Isolation

## 1. Security objective

DevBox executes code from repositories and commands requested by AI agents. Treat repository content, build scripts, dependencies, tool output, and agent-generated commands as untrusted. A compromised workspace must not compromise another workspace, the control plane, host infrastructure, or user credentials.

## 2. Trust boundaries

- User/browser to API.
- AI agent to MCP/API.
- Control plane to worker supervisor.
- Worker supervisor to execution container or VM.
- One workspace to another workspace.
- Workspace to host, internal network, and external network.
- Workspace to Git provider and credential store.

Every boundary requires authentication, authorization, and validation.

## 3. Workspace isolation

Default policy:
- One isolated execution boundary and filesystem per workspace.
- Separate process, mount, user, and network namespaces where supported.
- Run as non-root inside the workspace.
- Drop unnecessary Linux capabilities.
- Apply seccomp and mandatory access controls (AppArmor/SELinux) where available.
- Use read-only base images and a dedicated writable workspace directory with filesystem-enforced project quotas.
- Do not mount host root, sensitive host directories, or the container runtime socket.
- Do not share writable caches between tenants unless the cache is designed for safe, content-addressed sharing.
- Enforce CPU, memory, disk block and inode, process, file descriptor, and wall-clock quotas.
- Kill descendant processes and clean temporary resources on cancellation or deletion.

The trusted storage worker has Docker socket access and CAP_SYS_ADMIN to administer XFS quotas; compromise of that worker can compromise the host. Keep it private and treat it as a privileged control-plane component. Containers reduce risk but share the host kernel. For hostile multi-tenant workloads, evaluate microVMs or dedicated VMs as a stronger boundary.

## 4. Identity and authorization

- Authenticate humans and agents separately.
- Workspace IDs are identifiers, never bearer secrets.
- Authorize every read, write, command, job, log, artifact, Git, and sharing request.
- Enforce ownership and grants on the server; never trust client-provided owner or role fields.
- Private workspaces are private by default.
- Grants are scoped to one workspace and one principal.
- Initial roles: `read`, `write`, `admin`.
- Only the owner or an authorized administrator may grant access.
- Sharing is non-transitive by default.
- Revocation must block new operations immediately and invalidate active sessions/credentials as appropriate.
- Record grant, use, and revocation events.

## 5. Command execution

- Treat command strings and repository scripts as untrusted input.
- Execute only inside the assigned workspace boundary.
- Apply timeouts, output limits, resource quotas, and cancellation.
- Do not execute commands through a shell in the control-plane process.
- Prefer structured command arguments and configured build profiles over unrestricted shell access.
- If interactive shell access is enabled, it remains confined to the workspace and subject to the same quotas.
- Prevent path traversal and symlink escapes when reading/writing files.
- Validate artifact paths and prevent access outside approved workspace mounts.

## 6. Network policy

Default to restricted egress. Allow only the network access needed for configured dependency registries and source-control operations. Deny access to:
- Cloud instance metadata endpoints.
- Host and worker management interfaces.
- Private/internal control-plane networks.
- Other workspace endpoints.
- Localhost services outside the workspace boundary.

Apply DNS and IP-level controls to reduce DNS rebinding and SSRF risks. Network allowlists must be configurable per workload class.

## 7. Secrets and credentials

- Store provider tokens in an encrypted credential store or secret manager.
- Use least-privilege, repository-scoped, short-lived credentials.
- Inject secrets only for the operation that needs them.
- Never write credentials into repository files, images, command history, logs, artifacts, or environment snapshots.
- Redact secrets from stdout/stderr and error messages.
- Do not expose GitHub write credentials to read-only agents.
- Revoke credentials when access is revoked or a workspace is deleted.

## 8. Git safety

- A public repository URL grants no write access.
- Verify Git provider identity and repository permissions.
- Use branches for isolated agent work.
- Never force-push or overwrite remote history by default.
- Require explicit authorization for push, merge, release, or publication.
- Detect dirty working trees and conflicts before destructive operations.
- Keep an audit record of commits and remote changes.

## 9. Persistence, snapshots, and deletion

- Encrypt data in transit and at rest.
- Restrict snapshots and backups to authorized workspace owners and trusted services.
- Encrypt snapshot archives with authenticated encryption (AES-256-GCM) and store them on a separate quota-limited filesystem.
- Keep the snapshot encryption key outside the snapshot filesystem and back it up securely; key loss makes existing snapshots unrecoverable.
- Treat snapshot contents as sensitive: archives include workspace files as-is, including credentials accidentally written into the workspace. Encryption protects data at rest but does not remove secrets from the archive.
- Validate archive paths and symbolic links before restore; reject traversal and unsupported filesystem objects.
- Define retention and deletion behaviour for workspace data, logs, artifacts, and backups.
- Deletion should revoke credentials and grants, terminate executors, and schedule durable data removal according to the published retention policy.
- Test restore procedures and access controls.
- Delete a workspace's snapshots when its workspace data is permanently deleted, subject to the documented retention policy.

## 10. Audit and incident response

Record actor, workspace, operation, target, timestamp, request/job ID, result, and relevant policy decision. Avoid recording secrets or unnecessary source contents.

Provide:
- Centralized security and execution logs.
- Alerts for repeated authorization failures, resource abuse, and suspicious network activity.
- Ability to suspend an agent, workspace, or credential.
- Incident procedures for suspected container escape, credential exposure, and cross-workspace access.

## 11. Minimum security tests

- Agent A cannot list, read, write, or execute in Agent B's workspace.
- Guessing a valid workspace ID does not bypass authorization.
- A revoked grant no longer authorizes access.
- A workspace cannot read host files or another workspace's volume.
- A job cannot exceed configured resource or time limits.
- A job cannot access cloud metadata or internal management endpoints.
- Malicious filenames, symlinks, and archive entries cannot escape the workspace.
- Secrets do not appear in logs, artifacts, snapshots, or command history.
- Cancellation terminates child processes and releases resources.
