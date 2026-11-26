from __future__ import annotations

import os
from typing import Any

import httpx


class BrokerError(RuntimeError):
    pass


class BrokerUnavailable(BrokerError):
    pass


class BrokerNotFound(BrokerError):
    pass


class BrokerRejected(BrokerError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class BrokerClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout_seconds: float = 30,
        git_timeout_seconds: float = 330,
    ) -> None:
        self.base_url = (base_url or os.getenv("DEVBOX_BROKER_URL", "http://broker:9100")).rstrip("/")
        self.token = token if token is not None else os.getenv("DEVBOX_BROKER_TOKEN", "")
        self.timeout_seconds = timeout_seconds
        self.git_timeout_seconds = git_timeout_seconds
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )

    def validate_config(self) -> None:
        if len(self.token) < 32 or "replace-this-with" in self.token.lower():
            raise RuntimeError(
                "DEVBOX_BROKER_TOKEN must be a unique non-placeholder secret of at least 32 characters."
            )
        parsed = httpx.URL(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise RuntimeError("DEVBOX_BROKER_URL must be an absolute HTTP(S) URL.")

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(
                method,
                path,
                json=json,
                timeout=timeout or self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise BrokerUnavailable("Privileged broker is unavailable.") from exc

        if response.status_code == 404:
            raise BrokerNotFound("Broker resource does not exist.")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            if response.status_code >= 500:
                raise BrokerUnavailable(str(detail))
            raise BrokerRejected(response.status_code, str(detail))
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrokerUnavailable("Broker returned an invalid response.") from exc
        if not isinstance(payload, dict):
            raise BrokerUnavailable("Broker returned an invalid response.")
        return payload

    def ping(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def start_workspace(self, workspace_id: str) -> BrokerContainer:
        payload = self._request("POST", f"/internal/workspaces/{workspace_id}/start")
        return BrokerContainer(self, workspace_id, payload)

    def get_workspace(self, workspace_id: str) -> BrokerContainer:
        payload = self._request("GET", f"/internal/workspaces/{workspace_id}/status")
        if payload.get("state") == "absent":
            raise BrokerNotFound("Workspace executor does not exist.")
        if payload.get("state") == "failed":
            raise BrokerRejected(409, "Workspace executor failed broker identity validation.")
        return BrokerContainer(self, workspace_id, payload)

    def stop_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request("POST", f"/internal/workspaces/{workspace_id}/stop")

    def pause_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request("POST", f"/internal/workspaces/{workspace_id}/pause")

    def resume_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request("POST", f"/internal/workspaces/{workspace_id}/resume")

    def delete_workspace(self, workspace_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/internal/workspaces/{workspace_id}")

    def list_managed_workspace_ids(self) -> list[str]:
        payload = self._request("GET", "/internal/managed-workspaces")
        workspace_ids = payload.get("workspace_ids")
        if not isinstance(workspace_ids, list) or not all(
            isinstance(workspace_id, str) for workspace_id in workspace_ids
        ):
            raise BrokerUnavailable("Broker returned an invalid workspace list.")
        return workspace_ids

    def reconcile_git_helpers(self) -> int:
        payload = self._request("POST", "/internal/reconcile/git-helpers")
        removed = payload.get("removed")
        if not isinstance(removed, int):
            raise BrokerUnavailable("Broker returned an invalid reconciliation result.")
        return removed

    def run_git_helper(
        self,
        workspace_id: str,
        arguments: list[str],
        *,
        network_enabled: bool = False,
    ) -> str:
        payload = self._request(
            "POST",
            f"/internal/workspaces/{workspace_id}/git",
            json={"arguments": arguments, "network_enabled": network_enabled},
            timeout=self.git_timeout_seconds,
        )
        output = payload.get("output")
        if not isinstance(output, str):
            raise BrokerUnavailable("Broker returned invalid Git output.")
        return output


class BrokerContainer:
    """Small compatibility wrapper for the workspace-container operations used by the worker."""

    def __init__(
        self,
        client: BrokerClient,
        workspace_id: str,
        payload: dict[str, Any],
    ) -> None:
        self.client = client
        self.workspace_id = workspace_id
        self._apply(payload)

    def _apply(self, payload: dict[str, Any]) -> None:
        self.id = payload.get("container_id")
        self.name = payload.get("container_name")
        self.status = payload.get("state", "absent")
        self.attrs = payload.get("attrs") or {}

    def reload(self) -> None:
        payload = self.client._request(
            "GET", f"/internal/workspaces/{self.workspace_id}/status"
        )
        self._apply(payload)

    def start(self) -> None:
        self._apply(self.client._request(
            "POST", f"/internal/workspaces/{self.workspace_id}/start"
        ))

    def stop(self, timeout: int = 10) -> None:
        self._apply(self.client.stop_workspace(self.workspace_id))

    def pause(self) -> None:
        self._apply(self.client.pause_workspace(self.workspace_id))

    def unpause(self) -> None:
        self._apply(self.client.resume_workspace(self.workspace_id))

    def remove(self, force: bool = False, v: bool = False) -> None:
        self._apply(self.client.delete_workspace(self.workspace_id))
