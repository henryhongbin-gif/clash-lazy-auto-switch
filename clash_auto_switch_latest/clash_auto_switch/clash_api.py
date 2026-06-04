"""
Clash / Mihomo REST API client.

Supports both HTTP (http://host:port) and Unix-domain-socket transports.
All secret handling stays in-memory – never passed via CLI arguments.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


SPECIAL_NAMES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}


def _quote(name: str) -> str:
    return urllib.parse.quote(name, safe="")


@dataclass
class Api:
    """Thin wrapper around the Clash external-controller REST API."""

    base: str
    secret: str | None
    timeout: float
    unix_socket: str | None = None

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        data: bytes | None = None
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        if self.unix_socket:
            payload = self._request_unix(method, path, headers, data)
        else:
            url = self.base.rstrip("/") + path
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"{method} {path} failed: HTTP {exc.code} {detail}"
                ) from exc

        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))

    def _request_unix(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        data: bytes | None,
    ) -> bytes:
        class _UnixConnection(http.client.HTTPConnection):
            def __init__(self, sock_path: str, timeout: float) -> None:
                super().__init__("localhost", timeout=timeout)
                self._sock_path = sock_path

            def connect(self) -> None:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                sock.connect(self._sock_path)
                self.sock = sock

        conn = _UnixConnection(self.unix_socket, self.timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            if resp.status >= 400:
                detail = payload.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"{method} {path} failed: HTTP {resp.status} {detail}"
                )
            return payload
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # public helpers
    # ------------------------------------------------------------------

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def put(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("PUT", path, body)

    def proxy_delay(
        self,
        name: str,
        test_url: str,
        timeout_ms: int,
    ) -> int | None:
        """Measure delay of a single proxy node."""
        params = urllib.parse.urlencode({"url": test_url, "timeout": timeout_ms})
        try:
            data = self.get(f"/proxies/{_quote(name)}/delay?{params}")
        except Exception:
            return None
        delay = data.get("delay") if isinstance(data, dict) else None
        if isinstance(delay, int) and delay >= 0:
            return delay
        return None

    # ------------------------------------------------------------------
    # higher-level operations
    # ------------------------------------------------------------------

    def load_proxies(self) -> dict[str, Any]:
        data = self.get("/proxies")
        proxies = data.get("proxies") if isinstance(data, dict) else None
        if not isinstance(proxies, dict):
            raise RuntimeError("unexpected /proxies response")
        return proxies

    def load_provider_members(self) -> dict[str, set[str]]:
        try:
            data = self.get("/providers/proxies")
        except Exception:
            return {}
        providers = data.get("providers") if isinstance(data, dict) else None
        if not isinstance(providers, dict):
            return {}

        members: dict[str, set[str]] = {}
        for pname, pinfo in providers.items():
            if not isinstance(pname, str) or not isinstance(pinfo, dict):
                continue
            proxy_items = pinfo.get("proxies") or []
            names: set[str] = set()
            for item in proxy_items:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    names.add(item["name"])
                elif isinstance(item, str):
                    names.add(item)
            members[pname] = names
        return members

    def refresh_providers(self) -> None:
        try:
            data = self.get("/providers/proxies")
        except Exception:
            return
        providers = data.get("providers") if isinstance(data, dict) else None
        if not isinstance(providers, dict):
            return
        for name in providers:
            try:
                self.put(f"/providers/proxies/{_quote(name)}")
            except Exception:
                pass

    def switch_proxy(self, group_name: str, node_name: str) -> None:
        """Tell Clash to switch *group_name* to *node_name*."""
        self.put(f"/proxies/{_quote(group_name)}", {"name": node_name})
