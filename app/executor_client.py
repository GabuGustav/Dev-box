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

    def _request(self, method: str, path: str, timeout: float = 30.0) -> dict:
        if not self.token:
            raise ExecutorUnavailable("Executor service authentication is not configured.")
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=timeout,
                trust_env=False,
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
