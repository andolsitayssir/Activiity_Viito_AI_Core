"""Tiny requests-based HTTP client used by backend executors."""

from __future__ import annotations

from typing import Any

import requests

from server.execution.executors.base import ExecutorError

DEFAULT_TIMEOUT = 20


class HttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {"Accept": "application/json", **(headers or {})}
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> Any:
        """Perform a request; return the parsed JSON body on 2xx."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self._session.request(
                method,
                url,
                params=params,
                json=json,
                data=data,
                headers=headers,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise ExecutorError(f"network error on {method} {url}: {exc}") from exc

        if not response.ok:
            detail = response.text[:300].replace("\\n", " ")
            raise ExecutorError(
                f"{method} {url} -> HTTP {response.status_code}: {detail}"
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def close(self) -> None:
        self._session.close()