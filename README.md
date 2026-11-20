# DevBox

**Persistent, isolated development workspaces for AI agents.**

DevBox is a model-agnostic platform where AI agents can work with real repositories, edit code, install dependencies, run builds and tests, inspect logs, and prepare Git changes inside isolated execution environments.

## Core principles

- **One private workspace per agent by default.** Workspaces are isolated from one another.
- **Explicit sharing.** Owners can grant scoped read, write, or admin access and revoke it.
- **Persistent identity.** Every workspace has a unique, stable ID independent of any chat or browser session.
- **Cross-chat continuity.** A new AI session can locate authorized workspaces by repository URL, select a workspace, and continue from its actual files and Git state.
- **Controlled collaboration.** Agents exchange changes through explicit Git operations, branches, commits, and pull requests.
- **Model independence.** A versioned API and MCP-compatible interface allow different AI clients to use the same platform.
- **CPU-first execution.** Builds and ordinary automated tests do not require a dedicated GPU. GUI and GPU-backed runtime testing are later optional capabilities.

A repository can have multiple workspaces. The repository URL is a discovery key; the workspace ID identifies one exact environment. Knowing an ID does not grant access.

## Documentation

- [Product specification](PROJECT_SPEC.md)
- [System architecture](ARCHITECTURE.md)
- [Implementation roadmap](ROADMAP.md)
- [Security and isolation](SECURITY.md)
- [Build and test system](BUILD_SYSTEM.md)

## Initial implementation goal

Prove the core workflow:

1. Two agents create separate workspaces for the same repository.
2. Neither agent can access the other's workspace without permission.
3. Workspace data persists across service restarts and new chat sessions.
4. A new client discovers authorized workspaces from a GitHub URL and reconnects to one.
5. An agent runs a build or test, inspects errors, fixes code, and reruns the job.
6. Changes are integrated through controlled Git operations.

## Project status

Requirements and initial architecture are documented. Implementation has not yet started.
