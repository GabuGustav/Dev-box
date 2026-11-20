import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.executor_client import ExecutorClient, ExecutorUnavailable
from app.models import AuditEvent, Principal, Workspace
from app.repositories import get_or_create_repository, normalize_repository_url
from app.schemas import WorkspaceCreate, WorkspaceRead
from app.security import get_current_principal

router = APIRouter(prefix="/workspaces", tags=["workspaces"])
logger = logging.getLogger("devbox.audit")
executor_client = ExecutorClient()


def new_workspace_id() -> str:
    return f"dbx_{secrets.token_urlsafe(18)}"


def serialize_workspace(row: Workspace) -> WorkspaceRead:
    return WorkspaceRead(
        id=row.id,
        name=row.name,
        repository_url=row.repository.normalized_url,
        branch=row.branch,
        status=row.status,
        owner_id=row.owner_id,
        executor_id=row.executor_id,
        storage_ref=row.storage_ref,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def owned_workspace(db: Session, principal: Principal, workspace_id: str) -> Workspace:
    row = db.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.owner_id == principal.id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    return row


def execute_lifecycle(
    action: str,
    workspace_id: str,
) -> dict:
    try:
        return getattr(executor_client, action)(workspace_id)
    except ExecutorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def record_lifecycle(
    db: Session,
    request: Request,
    principal: Principal,
    workspace: Workspace,
    action: str,
    result: dict,
    new_status: str,
) -> WorkspaceRead:
    workspace.status = new_status
    workspace.executor_id = result.get("container_id")
    workspace.storage_ref = result.get("storage_ref")
    db.add(
        AuditEvent(
            actor_id=principal.id,
            action=f"workspace.{action}",
            target_type="workspace",
            target_id=workspace.id,
            request_id=getattr(request.state, "request_id", None),
            details={"status": new_status},
        )
    )
    db.commit()
    db.refresh(workspace)
    return serialize_workspace(workspace)


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: WorkspaceCreate,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> WorkspaceRead:
    try:
        normalized_url = normalize_repository_url(payload.repository_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    name = payload.name.strip()
    branch = payload.branch.strip()
    if not name or not branch:
        raise HTTPException(status_code=422, detail="Workspace name and branch cannot be blank.")

    repository = get_or_create_repository(db, normalized_url)
    workspace = Workspace(
        id=new_workspace_id(),
        name=name,
        owner_id=principal.id,
        repository_id=repository.id,
        branch=branch,
        status="registered",
    )
    db.add(workspace)
    db.flush()
    db.add(
        AuditEvent(
            actor_id=principal.id,
            action="workspace.created",
            target_type="workspace",
            target_id=workspace.id,
            request_id=getattr(request.state, "request_id", None),
            details={"repository_url": normalized_url, "branch": branch},
        )
    )
    db.commit()
    db.refresh(workspace)
    logger.info(
        "Workspace created",
        extra={"request_id": getattr(request.state, "request_id", None)},
    )
    return serialize_workspace(workspace)


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces(
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> list[WorkspaceRead]:
    rows = db.scalars(
        select(Workspace)
        .where(Workspace.owner_id == principal.id)
        .order_by(Workspace.created_at.desc())
    ).all()
    return [serialize_workspace(row) for row in rows]


@router.get("/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> WorkspaceRead:
    return serialize_workspace(owned_workspace(db, principal, workspace_id))


@router.post("/{workspace_id}/start", response_model=WorkspaceRead)
def start_workspace(
    workspace_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> WorkspaceRead:
    workspace = owned_workspace(db, principal, workspace_id)
    if workspace.status == "deleted":
        raise HTTPException(status_code=409, detail="Deleted workspaces cannot be started.")
    result = execute_lifecycle("start", workspace.id)
    return record_lifecycle(db, request, principal, workspace, "started", result, "running")


@router.post("/{workspace_id}/stop", response_model=WorkspaceRead)
def stop_workspace(
    workspace_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> WorkspaceRead:
    workspace = owned_workspace(db, principal, workspace_id)
    if workspace.status == "deleted":
        raise HTTPException(status_code=409, detail="Workspace is deleted.")
    result = execute_lifecycle("stop", workspace.id)
    return record_lifecycle(db, request, principal, workspace, "stopped", result, "stopped")


@router.post("/{workspace_id}/pause", response_model=WorkspaceRead)
def pause_workspace(
    workspace_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> WorkspaceRead:
    workspace = owned_workspace(db, principal, workspace_id)
    if workspace.status != "running":
        raise HTTPException(status_code=409, detail="Only running workspaces can be paused.")
    result = execute_lifecycle("pause", workspace.id)
    return record_lifecycle(db, request, principal, workspace, "paused", result, "paused")


@router.post("/{workspace_id}/resume", response_model=WorkspaceRead)
def resume_workspace(
    workspace_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> WorkspaceRead:
    workspace = owned_workspace(db, principal, workspace_id)
    if workspace.status == "deleted":
        raise HTTPException(status_code=409, detail="Deleted workspaces cannot be resumed.")
    if workspace.status not in {"paused", "stopped"}:
        raise HTTPException(status_code=409, detail="Only paused or stopped workspaces can resume.")
    result = execute_lifecycle("resume", workspace.id)
    return record_lifecycle(db, request, principal, workspace, "resumed", result, "running")


@router.delete("/{workspace_id}", response_model=WorkspaceRead)
def delete_workspace(
    workspace_id: str,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> WorkspaceRead:
    workspace = owned_workspace(db, principal, workspace_id)
    if workspace.status != "deleted":
        result = execute_lifecycle("delete", workspace.id)
        return record_lifecycle(db, request, principal, workspace, "deleted", result, "deleted")
    return serialize_workspace(workspace)
