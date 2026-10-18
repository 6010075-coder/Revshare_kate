from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_MCP_URL = "https://mcp.altegio.dev/bi-data/mcp"
DEFAULT_AUTH_SERVER = "https://mcp.altegio.dev"
DEFAULT_RESOURCE = "https://mcp.altegio.dev/bi-data"
MCP_SCOPE = "mcp:bi-data:read"
MCP_PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


def mcp_url() -> str:
    return os.getenv("REVSHARE_ALTEGIO_MCP_URL", DEFAULT_MCP_URL).strip()


def mcp_resource() -> str:
    url = mcp_url().removesuffix("/mcp").rstrip("/")
    return os.getenv("REVSHARE_ALTEGIO_MCP_RESOURCE", url).strip()


def mcp_auth_server() -> str:
    return os.getenv("REVSHARE_ALTEGIO_MCP_AUTH_SERVER", DEFAULT_AUTH_SERVER).rstrip("/")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _decode_response(response) -> object:
    body = response.read().decode("utf-8")
    if not body:
        return None
    content_type = response.headers.get_content_type()
    if content_type == "application/json":
        return json.loads(body)
    events: list[object] = []
    data_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line.strip() and data_lines:
            try:
                events.append(json.loads("\n".join(data_lines)))
            except json.JSONDecodeError:
                pass
            data_lines = []
    if data_lines:
        try:
            events.append(json.loads("\n".join(data_lines)))
        except json.JSONDecodeError:
            pass
    if events:
        return events[-1]
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise MCPError("MCP returned an unsupported response") from error


def _http_json(
    url: str,
    *,
    method: str = "POST",
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[object, object]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            return _decode_response(response), response.headers
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise MCPError(f"MCP HTTP {error.code}: {detail[:500]}") from error
    except URLError as error:
        raise MCPError(f"MCP connection failed: {error.reason}") from error


def register_client(redirect_uri: str) -> str:
    payload, _ = _http_json(
        f"{mcp_auth_server()}/register",
        payload={
            "client_name": "Altegio Revshare",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": MCP_SCOPE,
        },
    )
    if not isinstance(payload, dict) or not payload.get("client_id"):
        raise MCPError("MCP did not return a client id")
    return str(payload["client_id"])


def authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": MCP_SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{mcp_auth_server()}/authorize?{query}"


def exchange_code(
    *, client_id: str, code: str, redirect_uri: str, verifier: str
) -> dict[str, object]:
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    ).encode("ascii")
    request = Request(
        f"{mcp_auth_server()}/token",
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = _decode_response(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise MCPError(f"MCP token exchange failed: {detail[:500]}") from error
    except URLError as error:
        raise MCPError(f"MCP token connection failed: {error.reason}") from error
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise MCPError("MCP token response did not contain an access token")
    return payload


def refresh_access_token(store) -> str:
    refresh_token = store.setting("mcp_refresh_token")
    client_id = store.setting("mcp_client_id")
    if not refresh_token or not client_id:
        raise MCPError("MCP is not connected")
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        }
    ).encode("ascii")
    request = Request(
        f"{mcp_auth_server()}/token",
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = _decode_response(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise MCPError(f"MCP refresh failed: {detail[:500]}") from error
    except URLError as error:
        raise MCPError(f"MCP refresh connection failed: {error.reason}") from error
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise MCPError("MCP refresh response did not contain an access token")
    store.set_setting("mcp_access_token", str(payload["access_token"]))
    store.set_setting("mcp_refresh_token", str(payload.get("refresh_token") or refresh_token))
    store.set_setting(
        "mcp_expires_at", str(time.time() + float(payload.get("expires_in") or 900))
    )
    return str(payload["access_token"])


def access_token(store) -> str:
    token = store.setting("mcp_access_token")
    try:
        expires_at = float(store.setting("mcp_expires_at", "0"))
    except ValueError:
        expires_at = 0
    if token and expires_at > time.time() + 90:
        return token
    return refresh_access_token(store)


class MCPClient:
    def __init__(self, token: str):
        self.token = token
        self.session_id = ""
        self.request_id = 0
        self.initialized = False

    def _post(self, payload: dict) -> object:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        result, response_headers = _http_json(mcp_url(), payload=payload, headers=headers)
        self.session_id = response_headers.get("Mcp-Session-Id", self.session_id)
        if isinstance(result, dict) and result.get("error"):
            error = result["error"]
            raise MCPError(str(error.get("message") or error))
        return result

    def initialize(self) -> dict:
        if self.initialized:
            return {}
        self.request_id += 1
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "altegio-revshare", "version": "0.3.0"},
                },
            }
        )
        if not isinstance(result, dict) or "result" not in result:
            raise MCPError("MCP initialize returned an invalid response")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialized = True
        return result["result"]

    def list_tools(self) -> list[dict]:
        self.initialize()
        self.request_id += 1
        result = self._post(
            {"jsonrpc": "2.0", "id": self.request_id, "method": "tools/list", "params": {}}
        )
        if not isinstance(result, dict):
            raise MCPError("MCP tools response is invalid")
        return list(result.get("result", {}).get("tools", []))

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        self.initialize()
        self.request_id += 1
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        if not isinstance(result, dict):
            raise MCPError("MCP tool response is invalid")
        error = result.get("error")
        if error:
            raise MCPError(str(error.get("message") or error))
        if result.get("isError"):
            message = "MCP tool failed"
            for item in result.get("content", []):
                if isinstance(item, dict) and item.get("type") == "text":
                    message = str(item.get("text") or message)
                    break
            raise MCPError(message)
        return dict(result.get("result") or {})

    def query_rows(
        self,
        sql: str,
        *,
        database: str = "salon",
        limit: int = 1000,
    ) -> list[dict[str, object]]:
        """Run a read-only SQL query and decode MCP compact rows."""
        result = self.call_tool(
            "query_mysql",
            {
                "sql": sql,
                "database": database,
                "format": "compact",
                "limit": min(max(int(limit), 1), 1000),
            },
        )
        raw = ""
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            raw = str(structured.get("result") or "")
        if not raw:
            for item in result.get("content", []):
                if isinstance(item, dict) and item.get("type") == "text":
                    raw = str(item.get("text") or "")
                    break
        if "\n\n[HINT:" in raw:
            raw = raw.split("\n\n[HINT:", 1)[0]
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise MCPError("MCP query returned invalid row data") from error
        columns = payload.get("columns") if isinstance(payload, dict) else None
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and data == [] and columns is None:
            return []
        if not isinstance(columns, list) or not isinstance(data, list):
            raise MCPError("MCP query returned an invalid row shape")
        return [
            {str(column): value for column, value in zip(columns, row)}
            for row in data
            if isinstance(row, list)
        ]


def store_token_payload(store, client_id: str, payload: dict[str, object]) -> None:
    store.set_setting("mcp_client_id", client_id)
    store.set_setting("mcp_access_token", str(payload["access_token"]))
    store.set_setting("mcp_refresh_token", str(payload.get("refresh_token") or ""))
    store.set_setting(
        "mcp_expires_at", str(time.time() + float(payload.get("expires_in") or 900))
    )
