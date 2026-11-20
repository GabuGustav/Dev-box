import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import APIKey, Principal
from app.security import hash_api_key


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with testing_session() as db:
        first = Principal(name="test-agent", principal_type="agent")
        second = Principal(name="other-agent", principal_type="agent")
        db.add_all([first, second])
        db.flush()
        db.add_all(
            [
                APIKey(
                    principal_id=first.id,
                    key_hash=hash_api_key("test-key-one"),
                    key_prefix="dbx_test_one",
                ),
                APIKey(
                    principal_id=second.id,
                    key_hash=hash_api_key("test-key-two"),
                    key_prefix="dbx_test_two",
                ),
            ]
        )
        db.commit()

    with TestClient(app) as test_client:
        test_client.headers.update({"X-DevBox-Key": "test-key-one"})
        yield test_client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def auth_headers(key: str) -> dict[str, str]:
    return {"X-DevBox-Key": key}
