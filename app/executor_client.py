import httpx

from app.config import settings


class ExecutorUnavailable(RuntimeError):
    pass


class ExecutorRejected(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code


class ExecutorClient:
    def __init__(self) -> None:
        self.base_url = settings.executor_service_url.rstrip("/")
        self.token = settings.executor_service_token

    def _request(
        self,
        method: str,
        path: str,
        timeout: float = 30.0,
        json_body: dict | None = None,
    ) -> dict:
        if not self.token:
            raise ExecutorUnavailable("Executor service authentication is not configured.")
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=timeout,
                trust_env=False,
                json=json_body,
            )
            if 400 <= response.status_code < 500:
                try:
                    detail = response.json().get("detail", "Executor rejected the request.")
                except ValueError:
                    detail = "Executor rejected the request."
                raise ExecutorRejected(response.status_code, str(detail))
            response.raise_for_status()
            return response.json()
        except ExecutorRejected:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutorUnavailable("Workspace executor is unavailable.") from exc

    def start(self, workspace_id: str) -> dict:
        return self._request("POST", f"/internal/workspaces/{workspace_id}/start")

    def stop(self, workspace_id: str) -> dict:
        return self._request("POST", f"/internal/workspaces/{workspace_id}/stop")

    def pause(self, workspace_id: str) -> dict:
        return self._request("POST", f"/internal/workspaces/{workspace_id}/pause")

    def resume(self, workspace_id: str) -> dict:
        return self._request("POST", f"/internal/workspaces/{workspace_id}/resume")

    def delete(self, workspace_id: str) -> dict:
        return self._request("DELETE", f"/internal/workspaces/{workspace_id}")

    def create_snapshot(self, workspace_id: str) -> dict:
        return self._request(
            "POST", f"/internal/workspaces/{workspace_id}/snapshots", timeout=900.0
        )

    def list_snapshots(self, workspace_id: str) -> dict:
        return self._request("GET", f"/internal/workspaces/{workspace_id}/snapshots")

    def restore_snapshot(self, workspace_id: str, snapshot_id: str) -> dict:
        return self._request(
            "POST",
            f"/internal/workspaces/{workspace_id}/snapshots/{snapshot_id}/restore",
            timeout=900.0,
        )

    def delete_snapshot(self, workspace_id: str, snapshot_id: str) -> dict:
        return self._request(
            "DELETE",
            f"/internal/workspaces/{workspace_id}/snapshots/{snapshot_id}",
        )

    def get_status(self, workspace_id: str) -> dict:
        return self._request("GET", f"/internal/workspaces/{workspace_id}/status")

    def git_clone(self, workspace_id: str, repository_url: str, branch: str) -> dict:
        return self._request(
            "POST",
            f"/internal/workspaces/{workspace_id}/git/clone",
            timeout=600.0,
            json_body={"repository_url": repository_url, "branch": branch},
        )

    def git_status(self, workspace_id: str) -> dict:
        return self._request(
            "GET", f"/internal/workspaces/{workspace_id}/git/status", timeout=600.0
        )

    def git_diff(self, workspace_id: str) -> dict:
        return self._request(
            "GET", f"/internal/workspaces/{workspace_id}/git/diff", timeout=600.0
        )

    def git_create_branch(self, workspace_id: str, branch: str) -> dict:
        return self._request(
            "POST",
            f"/internal/workspaces/{workspace_id}/git/branches",
            timeout=600.0,
            json_body={"branch": branch},
        )

    def git_commit(
        self, workspace_id: str, message: str, paths: list[str] | None = None
    ) -> dict:
        return self._request(
            "POST",
            f"/internal/workspaces/{workspace_id}/git/commit",
            timeout=600.0,
            json_body={"message": message, "paths": paths or []},
        )

    def git_fetch(self, workspace_id: str, repository_url: str) -> dict:
        return self._request(
            "POST",
            f"/internal/workspaces/{workspace_id}/git/fetch",
            timeout=600.0,
            json_body={"repository_url": repository_url},
        )
