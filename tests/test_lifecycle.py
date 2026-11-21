from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.executor_client import ExecutorUnavailable
from app.lifecycle import reconcile_workspace_statuses
from app.models import AuditEvent, Principal, Repository, Workspace


class FakeExecutor:
    def __init__(self, states):
        self.states = states

    def get_status(self, workspace_id):
        result = self.states[workspace_id]
        if isinstance(result, Exception):
            raise result
        return result


def make_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def add_workspace(db: Session, status: str) -> Workspace:
    principal = Principal(name="owner", principal_type="agent")
    repository = Repository(normalized_url="https://github.com/example/project")
    db.add_all([principal, repository])
    db.flush()
    workspace = Workspace(
        id="dbx_abcdefghijklmnopqrstuv",
        name="test workspace",
        owner_id=principal.id,
        repository_id=repository.id,
        status=status,
        executor_id="old-container",
        storage_ref="old-storage",
    )
    db.add(workspace)
    db.commit()
    return workspace


def test_reconciliation_updates_stale_workspace_and_audits_change():
    engine, db = make_session()
    try:
        workspace = add_workspace(db, "running")
        executor = FakeExecutor(
            {
                workspace.id: {
                    "workspace_id": workspace.id,
                    "container_id": None,
                    "storage_ref": "xfs-current",
                    "state": "absent",
                }
            }
        )

        changed = reconcile_workspace_statuses(db, executor)

        assert changed == 1
        assert workspace.status == "stopped"
        assert workspace.executor_id is None
        assert workspace.storage_ref == "xfs-current"
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "workspace.reconciled")
        ).all()
        assert len(events) == 1
        assert events[0].details == {
            "previous_status": "running",
            "status": "stopped",
            "executor_state": "absent",
        }
    finally:
        db.close()
        engine.dispose()


def test_reconciliation_does_not_change_state_when_executor_is_unavailable():
    engine, db = make_session()
    try:
        workspace = add_workspace(db, "paused")
        executor = FakeExecutor({workspace.id: ExecutorUnavailable("offline")})

        changed = reconcile_workspace_statuses(db, executor)

        assert changed == 0
        assert workspace.status == "paused"
        assert workspace.executor_id == "old-container"
        assert db.scalars(select(AuditEvent)).all() == []
    finally:
        db.close()
        engine.dispose()


def test_reconciliation_maps_docker_restarting_to_recovering():
    engine, db = make_session()
    try:
        workspace = add_workspace(db, "running")
        executor = FakeExecutor(
            {
                workspace.id: {
                    "workspace_id": workspace.id,
                    "container_id": "container-123",
                    "storage_ref": "xfs-current",
                    "state": "restarting",
                }
            }
        )

        changed = reconcile_workspace_statuses(db, executor)

        assert changed == 1
        assert workspace.status == "recovering"
        assert workspace.executor_id == "container-123"
    finally:
        db.close()
        engine.dispose()
