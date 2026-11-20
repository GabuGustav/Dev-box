from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Repository


def normalize_repository_url(raw_url: str) -> str:
    value = raw_url.strip()
    if not value or any(char in value for char in ("\n", "\r", "\t")):
        raise ValueError("Repository URL is invalid.")
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        value = f"https://{host}/{path}"
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Use a valid HTTPS Git repository URL.")
    if parsed.username or parsed.password:
        raise ValueError("Repository URLs must not contain embedded credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("Repository URLs must not contain query strings or fragments.")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2 or any(part in {".", ".."} for part in parts):
        raise ValueError("Repository URL must identify an owner and repository.")
    path = "/".join(parts[:2])
    if path.endswith(".git"):
        path = path[:-4]
    return urlunsplit(("https", parsed.hostname.lower(), f"/{path}", "", ""))


def get_or_create_repository(db: Session, normalized_url: str) -> Repository:
    repository = db.scalar(select(Repository).where(Repository.normalized_url == normalized_url))
    if repository is None:
        repository = Repository(normalized_url=normalized_url)
        db.add(repository)
        db.flush()
    return repository
