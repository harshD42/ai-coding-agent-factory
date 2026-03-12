"""
services/project_service.py — Project business logic.

Combines local store operations with orchestrator client calls.
Screens call service methods; services never call screens.
"""

import logging
from typing import Optional

from tui.client import AicafClient, AicafConnectionError
from tui.state import AppState, ProjectState
from tui.store import Project, ProjectStore
from tui.utils.git import detect_repo

log = logging.getLogger("project_service")


class ProjectService:

    def __init__(
        self,
        client: AicafClient,
        store:  ProjectStore,
        state:  AppState,
    ) -> None:
        self._client = client
        self._store  = store
        self._state  = state

    # ── Create ────────────────────────────────────────────────────────────────

    async def create_project(
        self,
        name:      str,
        workspace: str,
        task:      str,
        models:    dict[str, str] | None = None,
    ) -> tuple[Project, str]:
        """
        Create a project in local store and start its first session.
        Returns (project, session_id).
        """
        project = self._store.create_project(
            name=name,
            workspace=workspace,
            orchestrator_url=self._client.base_url,
            default_models=models or {},
        )

        # Create first session on orchestrator
        sess = await self._client.create_session(
            task=task,
            models=models,
        )
        session_id = sess["session_id"]
        self._store.add_session_to_project(project.id, session_id)

        # If models provided, configure on orchestrator
        if models:
            try:
                await self._client.configure_models(session_id, models)
            except AicafConnectionError as e:
                log.warning("model configure failed: %s", e)

        log.info("created project %s, first session %s", project.id, session_id)
        return project, session_id

    # ── Load ──────────────────────────────────────────────────────────────────

    async def load_project(self, project_id: str) -> Optional[Project]:
        """Load project from store and populate ProjectState."""
        project = self._store.get_project(project_id)
        if project is None:
            return None

        git_info = detect_repo(project.workspace)

        self._state.project = ProjectState(
            project_id=project.id,
            name=project.name,
            workspace=project.workspace,
            session_ids=list(project.session_ids),
            git_branch=git_info.get("branch", ""),
            git_dirty=git_info.get("has_changes", False),
            orchestrator_url=project.orchestrator_url,
        )

        self._store.touch_project(project_id)
        return project

    # ── Session list ──────────────────────────────────────────────────────────

    async def get_project_sessions(self, project_id: str) -> list[dict]:
        """
        Return session dicts for all sessions in the project.
        Sessions that no longer exist on the orchestrator are marked expired.
        """
        project = self._store.get_project(project_id)
        if project is None:
            return []

        results = []
        for sid in reversed(project.session_ids):   # newest first
            try:
                sess = await self._client.get_session(sid)
                results.append({**sess, "expired": False})
            except AicafConnectionError:
                results.append({
                    "session_id": sid,
                    "status":     "expired",
                    "task":       "(expired)",
                    "expired":    True,
                    "created_at": 0,
                    "updated_at": 0,
                })
        return results

    # ── New session in existing project ───────────────────────────────────────

    async def new_session(
        self, project_id: str, task: str, models: dict | None = None
    ) -> str:
        """Create a new session under an existing project. Returns session_id."""
        project = self._store.get_project(project_id)
        if project is None:
            raise ValueError(f"Project {project_id!r} not found")

        effective_models = models or project.default_models or None
        sess = await self._client.create_session(task=task, models=effective_models)
        session_id = sess["session_id"]

        if effective_models:
            try:
                await self._client.configure_models(session_id, effective_models)
            except AicafConnectionError as e:
                log.warning("model configure failed: %s", e)

        self._store.add_session_to_project(project_id, session_id)
        return session_id

    # ── Orphan detection ──────────────────────────────────────────────────────

    async def find_orphan_sessions(self) -> list[dict]:
        """
        Return active sessions on the orchestrator that don't belong to any
        local project. Shown on the launcher as [reconnect] options.
        """
        try:
            active = await self._client.list_sessions(status="active")
        except AicafConnectionError:
            return []

        all_known: set[str] = set()
        for p in self._store.list_projects():
            all_known.update(p.session_ids)

        return [s for s in active if s["session_id"] not in all_known]

    # ── Index ─────────────────────────────────────────────────────────────────

    async def index_workspace(self) -> dict:
        return await self._client.index_codebase()