"""
store.py — Local project store backed by ~/.config/aicaf/ (platformdirs).

Owns two files:
  projects.json  — projects, session groupings, model defaults, chat history
  ui_state.json  — ephemeral UI state (last screen, last project, layout prefs)

Both are versioned with a migration path so future schema changes don't
corrupt existing installs. Corruption in ui_state.json never prevents boot —
it is silently reset to defaults.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from platformdirs import user_config_dir

log = logging.getLogger("store")

# ── Config directory ──────────────────────────────────────────────────────────
CONFIG_DIR = Path(user_config_dir("aicaf"))
PROJECTS_FILE  = CONFIG_DIR / "projects.json"
UI_STATE_FILE  = CONFIG_DIR / "ui_state.json"
LOGS_DIR       = CONFIG_DIR / "logs"

PROJECTS_VERSION  = 1
UI_STATE_VERSION  = 1
DEFAULT_ORCH_URL  = "http://localhost:9000"


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class Project:
    id:              str
    name:            str
    workspace:       str
    orchestrator_url: str = DEFAULT_ORCH_URL
    created_at:      float = field(default_factory=time.time)
    last_active:     float = field(default_factory=time.time)
    session_ids:     list[str] = field(default_factory=list)
    default_models:  dict[str, str] = field(default_factory=dict)
    metadata:        dict = field(default_factory=dict)


@dataclass
class ChatRecord:
    id:              str
    role:            str
    model:           str
    orchestrator_url: str = DEFAULT_ORCH_URL
    started_at:      float = field(default_factory=time.time)


@dataclass
class UIState:
    version:             int   = UI_STATE_VERSION
    last_project_id:     str   = ""
    last_session_id:     str   = ""
    last_screen:         str   = "launcher"
    dag_sidebar_open:    bool  = True
    last_orchestrator_url: str = DEFAULT_ORCH_URL


# ── Migration helpers ─────────────────────────────────────────────────────────

def _migrate_projects(data: dict) -> dict:
    v = data.get("version", 0)
    if v < 1:
        data.setdefault("chat_history", [])
        data.setdefault("last_orchestrator_url", DEFAULT_ORCH_URL)
        data["version"] = 1
    return data


def _migrate_ui_state(data: dict) -> dict:
    v = data.get("version", 0)
    if v < 1:
        data.setdefault("dag_sidebar_open", True)
        data.setdefault("last_orchestrator_url", DEFAULT_ORCH_URL)
        data["version"] = 1
    return data


# ── ProjectStore ──────────────────────────────────────────────────────────────

class ProjectStore:
    """
    Thread-safe (single-process) local store for projects and UI state.
    All methods are synchronous — called from TUI event handlers which
    are already on the main thread.
    """

    def __init__(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._projects:     list[Project]  = []
        self._chat_history: list[ChatRecord] = []
        self._last_orch_url: str           = DEFAULT_ORCH_URL
        self._ui_state:     UIState        = UIState()
        self._load()

    # ── Internal I/O ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load both files. Corruption in ui_state resets silently."""
        # projects.json
        if PROJECTS_FILE.exists():
            try:
                raw  = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
                data = _migrate_projects(raw)
                self._projects = [
                    Project(**p) for p in data.get("projects", [])
                ]
                self._chat_history = [
                    ChatRecord(**c) for c in data.get("chat_history", [])
                ]
                self._last_orch_url = data.get(
                    "last_orchestrator_url", DEFAULT_ORCH_URL
                )
            except Exception as e:
                log.warning("projects.json load failed (%s) — starting fresh", e)
                self._projects = []

        # ui_state.json — silently reset on any error
        if UI_STATE_FILE.exists():
            try:
                raw  = json.loads(UI_STATE_FILE.read_text(encoding="utf-8"))
                data = _migrate_ui_state(raw)
                self._ui_state = UIState(
                    version=data.get("version", 1),
                    last_project_id=data.get("last_project_id", ""),
                    last_session_id=data.get("last_session_id", ""),
                    last_screen=data.get("last_screen", "launcher"),
                    dag_sidebar_open=data.get("dag_sidebar_open", True),
                    last_orchestrator_url=data.get(
                        "last_orchestrator_url", DEFAULT_ORCH_URL
                    ),
                )
            except Exception as e:
                log.warning("ui_state.json load failed (%s) — using defaults", e)
                self._ui_state = UIState()

    def _save_projects(self) -> None:
        data = {
            "version": PROJECTS_VERSION,
            "projects": [asdict(p) for p in self._projects],
            "chat_history": [asdict(c) for c in self._chat_history],
            "last_orchestrator_url": self._last_orch_url,
        }
        PROJECTS_FILE.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _save_ui_state(self) -> None:
        UI_STATE_FILE.write_text(
            json.dumps(asdict(self._ui_state), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ── Project CRUD ──────────────────────────────────────────────────────────

    def create_project(
        self,
        name:            str,
        workspace:       str,
        orchestrator_url: str = DEFAULT_ORCH_URL,
        default_models:  dict[str, str] | None = None,
    ) -> Project:
        p = Project(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name=name,
            workspace=workspace,
            orchestrator_url=orchestrator_url,
            default_models=default_models or {},
        )
        self._projects.insert(0, p)
        self._save_projects()
        log.info("created project %s (%s)", p.id, p.name)
        return p

    def get_project(self, project_id: str) -> Optional[Project]:
        return next((p for p in self._projects if p.id == project_id), None)

    def list_projects(self) -> list[Project]:
        """Return projects sorted by last_active descending."""
        return sorted(self._projects, key=lambda p: p.last_active, reverse=True)

    def update_project(self, project_id: str, **kwargs) -> Optional[Project]:
        p = self.get_project(project_id)
        if p is None:
            return None
        for k, v in kwargs.items():
            if hasattr(p, k):
                setattr(p, k, v)
        self._save_projects()
        return p

    def delete_project(self, project_id: str) -> bool:
        before = len(self._projects)
        self._projects = [p for p in self._projects if p.id != project_id]
        if len(self._projects) < before:
            self._save_projects()
            return True
        return False

    def add_session_to_project(self, project_id: str, session_id: str) -> None:
        p = self.get_project(project_id)
        if p and session_id not in p.session_ids:
            p.session_ids.append(session_id)
            p.last_active = time.time()
            self._save_projects()

    def touch_project(self, project_id: str) -> None:
        """Update last_active timestamp."""
        p = self.get_project(project_id)
        if p:
            p.last_active = time.time()
            self._save_projects()

    # ── Chat history ──────────────────────────────────────────────────────────

    def add_chat_record(
        self, role: str, model: str, orchestrator_url: str = DEFAULT_ORCH_URL
    ) -> ChatRecord:
        c = ChatRecord(
            id=f"chat-{uuid.uuid4().hex[:8]}",
            role=role,
            model=model,
            orchestrator_url=orchestrator_url,
        )
        self._chat_history.insert(0, c)
        # Keep only last 20 chat records
        self._chat_history = self._chat_history[:20]
        self._save_projects()
        return c

    def list_chats(self) -> list[ChatRecord]:
        return list(self._chat_history)

    # ── Orchestrator URL ──────────────────────────────────────────────────────

    @property
    def last_orchestrator_url(self) -> str:
        return self._last_orch_url

    def set_orchestrator_url(self, url: str) -> None:
        self._last_orch_url = url.rstrip("/")
        self._ui_state.last_orchestrator_url = self._last_orch_url
        self._save_projects()
        self._save_ui_state()

    # ── UI state ──────────────────────────────────────────────────────────────

    @property
    def ui_state(self) -> UIState:
        return self._ui_state

    def save_ui_state(
        self,
        last_project_id:  str  | None = None,
        last_session_id:  str  | None = None,
        last_screen:      str  | None = None,
        dag_sidebar_open: bool | None = None,
    ) -> None:
        if last_project_id  is not None: self._ui_state.last_project_id  = last_project_id
        if last_session_id  is not None: self._ui_state.last_session_id  = last_session_id
        if last_screen      is not None: self._ui_state.last_screen      = last_screen
        if dag_sidebar_open is not None: self._ui_state.dag_sidebar_open = dag_sidebar_open
        self._save_ui_state()