import hashlib
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import APIKey, Principal, utc_now

api_key_header = APIKeyHeader(name="X-DevBox-Key", auto_error=False)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return f"dbx_live_{secrets.token_urlsafe(32)}"


def get_current_principal(
    raw_key: str | None = Depends(api_key_header),
    db: Session = Depends(get_db),
) -> Principal:
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A DevBox API key is required.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    key = db.scalar(
        select(APIKey).where(APIKey.key_hash == hash_api_key(raw_key), APIKey.revoked_at.is_(None))
    )
    if key is None or not key.principal.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked DevBox API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    key.last_used_at = utc_now()
    db.commit()
    return key.principal
