import httpx

from app.config import settings


class ExecutorUnavailable(RuntimeError):
    pass


class ExecutorClient:
    def __init__(self) -> None:
        self.base_url = settings.executor_service_url.rstrip("/")
        self.token = settings.executor_service_token

    def _request(self, method: str, path: str) -> dict:
        if not self.token:
            raise ExecutorUnavailable("Executor service authentication is not configured.")
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=30.0,
                trust_env=False,
            )
            response.raise_for_status()
            return response.json()
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
        return self._request("POST", f"/internal/workspaces/{workspace_id}/snapshots")

    def list_snapshots(self, workspace_id: str) -> dict:
        return self._request("GET", f"/internal/workspaces/{workspace_id}/snapshots")

    def restore_snapshot(self, workspace_id: str, snapshot_id: str) -> dict:
        return self._request(
            "POST",
            f"/internal/workspaces/{workspace_id}/snapshots/{snapshot_id}/restore",
        )

    def delete_snapshot(self, workspace_id: str, snapshot_id: str) -> dict:
        return self._request(
            "DELETE",
            f"/internal/workspaces/{workspace_id}/snapshots/{snapshot_id}",
        )
