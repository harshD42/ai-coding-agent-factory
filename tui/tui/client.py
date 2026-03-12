"""
client.py — Transport layer for orchestrator communication.

AicafClient: thin HTTP/SSE/WebSocket wrappers.
  No business logic. Every method raises AicafConnectionError on failure.
  Business logic lives in services/.

ConnectionSupervisor: background health monitor + reconnect loop.
  States: connected → disconnected → reconnecting → failed
  Notifies a callback so the app can update the header bar.
"""

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Callable, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("client")


class AicafConnectionError(Exception):
    """Raised when the orchestrator is unreachable or returns an unexpected error."""


# ── AicafClient ───────────────────────────────────────────────────────────────

class AicafClient:
    """
    Pure transport layer. Three sub-transports:
      - httpx.AsyncClient  — REST calls + SSE streaming
      - websockets         — WSEvent stream per session

    All methods are async. Callers catch AicafConnectionError.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._http: Optional[httpx.AsyncClient] = None

    async def connect(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    def _check(self) -> None:
        if self._http is None:
            raise AicafConnectionError("Client not connected — call connect() first")

    async def _get(self, path: str, **params) -> dict:
        self._check()
        try:
            r = await self._http.get(path, params={k: v for k, v in params.items() if v is not None})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise AicafConnectionError(f"HTTP {e.response.status_code}: {path}") from e
        except Exception as e:
            raise AicafConnectionError(str(e)) from e

    async def _post(self, path: str, body: dict) -> dict:
        self._check()
        try:
            r = await self._http.post(path, json=body)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            detail = ""
            try: detail = e.response.json().get("detail", "")
            except Exception: pass
            raise AicafConnectionError(f"HTTP {e.response.status_code}: {detail or path}") from e
        except Exception as e:
            raise AicafConnectionError(str(e)) from e

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> dict:
        return await self._get("/health")

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def list_sessions(self, status: str = None) -> list[dict]:
        data = await self._get("/v1/sessions", status=status)
        return data.get("sessions", [])

    async def get_session(self, session_id: str) -> dict:
        return await self._get(f"/v1/sessions/{session_id}")

    async def create_session(
        self, task: str, session_id: str = None, models: dict = None
    ) -> dict:
        body = {"task": task}
        if session_id: body["session_id"] = session_id
        if models:     body["models"]     = models
        return await self._post("/v1/sessions", body)

    async def end_session(self, session_id: str, summary: str = "") -> dict:
        return await self._post(f"/v1/sessions/{session_id}/end", {"summary": summary})

    async def pause_session(self, session_id: str) -> dict:
        return await self._post(f"/v1/sessions/{session_id}/pause", {})

    async def resume_session(self, session_id: str) -> dict:
        return await self._post(f"/v1/sessions/{session_id}/resume", {})

    async def configure_models(self, session_id: str, models: dict) -> dict:
        return await self._post("/v1/session/configure", {
            "session_id": session_id, "models": models
        })

    async def get_session_models(self, session_id: str) -> dict:
        return await self._get("/v1/session/models", session_id=session_id)

    # ── Agents ────────────────────────────────────────────────────────────────

    async def list_agents(self) -> list[dict]:
        data = await self._get("/v1/agents/list")
        return data.get("agents", [])

    async def spawn_agent(self, role: str, task: str, session_id: str) -> dict:
        return await self._post("/v1/agents/spawn", {
            "role": role, "task": task, "session_id": session_id
        })

    async def send_agent_message(
        self, agent_id: str, message: str, sender: str = "user"
    ) -> dict:
        return await self._post(f"/v1/agents/{agent_id}/message", {
            "message": message, "sender": sender
        })

    # ── Tasks ─────────────────────────────────────────────────────────────────

    async def get_task_status(self, session_id: str) -> dict:
        return await self._get("/v1/tasks/status", session_id=session_id)

    async def load_tasks(self, session_id: str, tasks: list[dict]) -> dict:
        return await self._post("/v1/tasks/load", {
            "session_id": session_id, "tasks": tasks
        })

    async def execute_tasks(self, session_id: str) -> dict:
        return await self._post("/v1/tasks/execute", {"session_id": session_id})

    # ── Patches ───────────────────────────────────────────────────────────────

    async def list_patches(self, session_id: str) -> list[dict]:
        data = await self._get("/v1/patches/list", session_id=session_id)
        return data.get("patches", [])

    async def process_patches(self) -> dict:
        return await self._post("/v1/patches/process", {})

    # ── Models ────────────────────────────────────────────────────────────────

    async def get_catalog(self) -> list[dict]:
        data = await self._get("/v1/models/catalog")
        return data.get("models", [])

    async def get_models_for_role(self, role: str) -> list[dict]:
        data = await self._get("/v1/models/for-role", role=role)
        return data.get("models", [])

    async def refresh_models(self) -> dict:
        return await self._post("/v1/models/refresh", {})

    # ── Memory / index ────────────────────────────────────────────────────────

    async def index_codebase(self) -> dict:
        return await self._post("/v1/index", {})

    async def recall(self, query: str) -> list[dict]:
        data = await self._get("/v1/memory/recall", q=query)
        return data.get("results", [])

    # ── Chat completions ──────────────────────────────────────────────────────

    async def chat_stream(
        self, messages: list[dict], model: str = "orchestrator"
    ) -> AsyncIterator[str]:
        """
        Stream tokens from POST /v1/chat/completions.
        Yields raw token strings. Caller responsible for [DONE] detection.
        """
        self._check()
        body = {"model": model, "messages": messages, "stream": True}
        try:
            async with self._http.stream("POST", "/v1/chat/completions", json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data_str)
                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            token = delta.get("content", "") or ""
                            if token:
                                yield token
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            raise AicafConnectionError(str(e)) from e

    # ── SSE: agent token stream ───────────────────────────────────────────────

    async def stream_agent_tokens(self, agent_id: str) -> AsyncIterator[str]:
        """
        Connect to GET /v1/agents/{agent_id}/stream (SSE).
        Yields token strings until [DONE] or connection closes.
        Uses a short-lived httpx client per stream to avoid timeout issues.
        """
        url = f"{self.base_url}/v1/agents/{agent_id}/stream"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0)) as c:
                async with c.stream("GET", url) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        token = line[5:].strip()
                        if token == "[DONE]":
                            return
                        if token:
                            yield token
        except Exception as e:
            log.debug("agent stream closed for %s: %s", agent_id, e)

    # ── WebSocket: session events ─────────────────────────────────────────────

    async def stream_session_events(
        self, session_id: str
    ) -> AsyncIterator[dict]:
        """
        Connect to ws://.../ws/session/{session_id}.
        Yields parsed WSEvent dicts until connection closes.
        Reconnection is handled by ConnectionSupervisor.
        """
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        uri    = f"{ws_url}/ws/session/{session_id}"
        try:
            async with websockets.connect(
                uri,
                ping_interval=20,
                ping_timeout=10,
                open_timeout=5,
            ) as ws:
                async for msg in ws:
                    try:
                        yield json.loads(msg)
                    except json.JSONDecodeError:
                        continue
        except ConnectionClosed:
            log.debug("ws session event stream closed for %s", session_id)
        except Exception as e:
            log.warning("ws stream error session=%s: %s", session_id, e)


# ── ConnectionSupervisor ──────────────────────────────────────────────────────

class ConnectionSupervisor:
    """
    Background health monitor. Pings /health every PING_INTERVAL seconds.
    On failure: sets status to reconnecting, retries with exponential backoff.
    On reconnect success: sets status to connected, calls on_reconnect.
    After MAX_RETRIES consecutive failures: sets status to failed.

    Designed to run as a background asyncio task in app.py.
    """

    STATES         = ("connected", "disconnected", "reconnecting", "failed")
    PING_INTERVAL  = 10       # seconds between pings when connected
    RETRY_DELAYS   = (1, 2, 4, 8, 16, 30)   # exponential backoff
    MAX_RETRIES    = 10

    def __init__(
        self,
        client: AicafClient,
        on_status_change: Callable[[str], None],
        on_reconnect:     Callable[[], None] | None = None,
    ) -> None:
        self._client   = client
        self._on_status = on_status_change
        self._on_reconnect = on_reconnect
        self._status   = "disconnected"
        self._task: Optional[asyncio.Task] = None

    @property
    def status(self) -> str:
        return self._status

    def _set_status(self, s: str) -> None:
        if s != self._status:
            self._status = s
            self._on_status(s)
            log.info("connection status → %s", s)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        retries = 0
        while True:
            try:
                await self._client.health()
                retries = 0
                self._set_status("connected")
                await asyncio.sleep(self.PING_INTERVAL)
            except AicafConnectionError:
                if self._status == "connected":
                    self._set_status("disconnected")

                if retries < self.MAX_RETRIES:
                    delay = self.RETRY_DELAYS[min(retries, len(self.RETRY_DELAYS) - 1)]
                    self._set_status("reconnecting")
                    log.info("reconnect attempt %d/%d in %ds", retries + 1, self.MAX_RETRIES, delay)
                    await asyncio.sleep(delay)
                    retries += 1
                else:
                    self._set_status("failed")
                    log.error("orchestrator unreachable after %d retries — giving up", retries)
                    return
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning("supervisor unexpected error: %s", e)
                await asyncio.sleep(5)