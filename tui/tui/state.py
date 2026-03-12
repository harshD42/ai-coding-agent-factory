"""
state.py — Three-layer reactive application state.

AppState owns UIState, ProjectState, and SessionState as nested dataclasses.
Textual widgets watch specific sub-fields so only affected widgets re-render
when state changes (token updates don't repaint the DAG, task updates don't
repaint agent panes, etc.)

State is NEVER mutated by widgets directly — always via service methods or
app-level message handlers. This keeps the data flow unidirectional and
predictable.
"""

import time
from dataclasses import dataclass, field
from typing import Optional


# ── Sub-states ────────────────────────────────────────────────────────────────

@dataclass
class UIState:
    screen:             str  = "launcher"
    focus_widget:       str  = ""
    connection_status:  str  = "disconnected"  # connected|disconnected|reconnecting|failed
    dag_sidebar_open:   bool = True
    following_agent_id: str  = ""   # agent_id being "followed" with f key
    notifications:      list[str] = field(default_factory=list)


@dataclass
class ProjectState:
    project_id:   str       = ""
    name:         str       = ""
    workspace:    str       = ""
    session_ids:  list[str] = field(default_factory=list)
    git_branch:   str       = ""
    git_dirty:    bool      = False
    orchestrator_url: str   = "http://localhost:9000"


@dataclass
class AgentInfo:
    """Live agent state — updated from WSEvents and agent list polling."""
    agent_id:   str
    role:       str
    status:     str   = "idle"     # idle|running|done|failed|killed
    model:      str   = ""
    task:       str   = ""
    started_at: float = 0.0
    ended_at:   float = 0.0
    inbox_depth: int  = 0


@dataclass
class TaskInfo:
    id:     str
    role:   str
    desc:   str
    status: str         = "pending"   # pending|running|complete|failed|blocked
    deps:   list[str]   = field(default_factory=list)
    result_preview: str = ""


@dataclass
class PatchInfo:
    patch_id:    str
    agent_id:    str
    description: str
    status:      str       = "pending"
    files:       list[str] = field(default_factory=list)
    created_at:  float     = 0.0


@dataclass
class SessionState:
    session_id:   str  = ""
    status:       str  = ""    # active|paused|ended
    task:         str  = ""
    # Keyed dicts for O(1) lookup by ID
    agents:       dict[str, AgentInfo]  = field(default_factory=dict)
    tasks:        dict[str, TaskInfo]   = field(default_factory=dict)
    patches:      dict[str, PatchInfo]  = field(default_factory=dict)
    # Metrics
    token_count:  int   = 0
    patch_count:  int   = 0
    elapsed_start: float = field(default_factory=time.time)
    # Model assignments for this session (role → model_name)
    models:       dict[str, str] = field(default_factory=dict)
    # In-memory event log for log viewer
    event_log:    list[dict] = field(default_factory=list)

    def active_agents(self) -> list[AgentInfo]:
        """Agents that are running or recently active (not cleaned up)."""
        active_statuses = {"idle", "running", "done", "failed", "killed"}
        return [a for a in self.agents.values() if a.status in active_statuses]

    def running_agents(self) -> list[AgentInfo]:
        return [a for a in self.agents.values() if a.status == "running"]

    def tasks_list(self) -> list[TaskInfo]:
        """Tasks in original load order (dict preserves insertion order in 3.7+)."""
        return list(self.tasks.values())

    def patches_list(self) -> list[PatchInfo]:
        return sorted(self.patches.values(), key=lambda p: p.created_at, reverse=True)

    def elapsed_seconds(self) -> float:
        return time.time() - self.elapsed_start


# ── AppState ──────────────────────────────────────────────────────────────────

class AppState:
    """
    Single source of truth for the TUI.

    Not a Textual reactive — instead the app passes this to screens and
    widgets which call app.refresh_specific_widget() after mutations.
    This gives us fine-grained control over what repaints.

    Usage pattern:
        state.session.token_count += delta
        app.query_one(HeaderBar).refresh()   # only header updates
    """

    def __init__(self) -> None:
        self.ui      = UIState()
        self.project: Optional[ProjectState] = None
        self.session: Optional[SessionState] = None

    # ── UI helpers ────────────────────────────────────────────────────────────

    def set_connection(self, status: str) -> None:
        """connected | disconnected | reconnecting | failed"""
        self.ui.connection_status = status

    def set_following(self, agent_id: str) -> None:
        self.ui.following_agent_id = agent_id

    def toggle_dag(self) -> bool:
        self.ui.dag_sidebar_open = not self.ui.dag_sidebar_open
        return self.ui.dag_sidebar_open

    def push_notification(self, msg: str) -> None:
        self.ui.notifications.append(msg)
        if len(self.ui.notifications) > 10:
            self.ui.notifications.pop(0)

    # ── Session helpers ───────────────────────────────────────────────────────

    def init_session(self, session_id: str, task: str, models: dict = None) -> None:
        self.session = SessionState(
            session_id=session_id,
            task=task,
            models=models or {},
            elapsed_start=time.time(),
        )

    def clear_session(self) -> None:
        self.session = None

    def upsert_agent(self, agent_dict: dict) -> AgentInfo:
        """Insert or update an agent from orchestrator agent dict."""
        if self.session is None:
            return
        aid = agent_dict["agent_id"]
        if aid in self.session.agents:
            a = self.session.agents[aid]
            a.status     = agent_dict.get("status", a.status)
            a.model      = agent_dict.get("model", a.model) or a.model
            a.task       = agent_dict.get("task", a.task) or a.task
            a.ended_at   = agent_dict.get("ended_at") or a.ended_at
        else:
            a = AgentInfo(
                agent_id=aid,
                role=agent_dict.get("role", ""),
                status=agent_dict.get("status", "idle"),
                model=agent_dict.get("model", ""),
                task=agent_dict.get("task", ""),
                started_at=agent_dict.get("started_at", time.time()),
            )
            self.session.agents[aid] = a
        return a

    def upsert_task(self, task_dict: dict) -> TaskInfo:
        if self.session is None:
            return
        tid = task_dict["id"]
        if tid in self.session.tasks:
            t = self.session.tasks[tid]
            t.status = task_dict.get("status", t.status)
            t.result_preview = task_dict.get("result", "")[:100]
        else:
            t = TaskInfo(
                id=tid,
                role=task_dict.get("role", ""),
                desc=task_dict.get("desc", ""),
                status=task_dict.get("status", "pending"),
                deps=task_dict.get("deps", []),
            )
            self.session.tasks[tid] = t
        return t

    def upsert_patch(self, patch_dict: dict) -> PatchInfo:
        if self.session is None:
            return
        pid = patch_dict["patch_id"]
        if pid not in self.session.patches:
            p = PatchInfo(
                patch_id=pid,
                agent_id=patch_dict.get("agent_id", ""),
                description=patch_dict.get("description", ""),
                status=patch_dict.get("status", "pending"),
                files=patch_dict.get("files", []),
                created_at=patch_dict.get("created_at", time.time()),
            )
            self.session.patches[pid] = p
        else:
            self.session.patches[pid].status = patch_dict.get("status", "pending")
        self.session.patch_count = sum(
            1 for p in self.session.patches.values() if p.status == "applied"
        )
        return self.session.patches[pid]

    def add_tokens(self, count: int) -> None:
        if self.session:
            self.session.token_count += count

    def log_event(self, source: str, event_type: str, content: str) -> None:
        if self.session:
            self.session.event_log.append({
                "ts":      time.time(),
                "source":  source,
                "type":    event_type,
                "content": content,
            })
            # Keep last 500 events in memory
            if len(self.session.event_log) > 500:
                self.session.event_log = self.session.event_log[-500:]