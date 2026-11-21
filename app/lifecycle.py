import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.executor_client import ExecutorClient, ExecutorRejected, ExecutorUnavailable
from app.models import AuditEvent, Workspace

logger = logging.getLogger("devbox.lifecycle")

_RECONCILED_STATUSES = ("running", "paused", "recovering")
_STATE_MAP = {
    "running": "running",
    "paused": "paused",
    "restarting": "recovering",
    "created": "stopped",
    "exited": "stopped",
    "absent": "stopped",
    "dead": "failed",
    "failed": "failed",
}


def reconcile_workspace_statuses(db: Session, executor: ExecutorClient) -> int:
    """Synchronize active database records with authoritative executor state."""
    workspaces = db.scalars(
        select(Workspace).where(Workspace.status.in_(_RECONCILED_STATUSES))
    ).all()
    changed = 0

    for workspace in workspaces:
        previous_values = (workspace.status, workspace.executor_id, workspace.storage_ref)
        try:
            result = executor.get_status(workspace.id)
        except ExecutorUnavailable:
            logger.warning(
                "Skipping workspace reconciliation because the executor is unavailable",
                extra={"workspace_id": workspace.id},
            )
            continue
        except ExecutorRejected:
            logger.exception(
                "Executor rejected a workspace status request",
                extra={"workspace_id": workspace.id},
            )
            continue

        if result.get("workspace_id") != workspace.id:
            logger.error(
                "Executor returned a mismatched workspace identity",
                extra={"workspace_id": workspace.id},
            )
            continue

        executor_state = result.get("state")
        new_status = _STATE_MAP.get(executor_state)
        if new_status is None:
            logger.error(
                "Executor returned an unknown workspace state",
                extra={"workspace_id": workspace.id, "executor_state": executor_state},
            )
            continue

        new_executor_id = result.get("container_id")
        new_storage_ref = result.get("storage_ref")
        db.refresh(workspace, with_for_update=True)
        current_values = (workspace.status, workspace.executor_id, workspace.storage_ref)
        if current_values != previous_values:
            # A user lifecycle request committed while the executor was being queried.
            # Do not let this older observation overwrite the newer API operation.
            continue
        new_values = (new_status, new_executor_id, new_storage_ref)
        if current_values == new_values:
            continue

        workspace.status = new_status
        workspace.executor_id = new_executor_id
        workspace.storage_ref = new_storage_ref
        db.add(
            AuditEvent(
                actor_id=None,
                action="workspace.reconciled",
                target_type="workspace",
                target_id=workspace.id,
                details={
                    "previous_status": old_values[0],
                    "status": new_status,
                    "executor_state": executor_state,
                },
            )
        )
        changed += 1

    if changed:
        db.commit()
    return changed


def reconcile_once() -> int:
    """Run one reconciliation cycle using the application's database session."""
    from app.database import SessionLocal

    with SessionLocal() as db:
        return reconcile_workspace_statuses(db, ExecutorClient())
