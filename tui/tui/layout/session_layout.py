"""
layout/session_layout.py — Dynamic agent pane grid recalculation.

SessionLayoutManager computes the correct CSS grid class and pane
assignments given the current set of active agents. Called on:
  - Agent spawn (new pane appears)
  - Agent done/cleanup (pane hibernates or disappears)
  - Terminal resize (Textual RESIZE event)

Coder always gets the largest pane because it produces the most tokens.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from state import AgentInfo

log = logging.getLogger("layout")

# Priority order for pane size assignment
# Roles listed first get larger panes when space is limited
ROLE_PRIORITY = ["coder", "architect", "reviewer", "tester", "documenter"]


@dataclass
class PaneAssignment:
    agent_id: str
    role:     str
    css_id:   str      # e.g. "agent-pane-coder-abc123"
    col:      int = 0  # grid column (0-indexed)
    row:      int = 0  # grid row (0-indexed)
    large:    bool = False  # True for the primary/coder pane


class SessionLayoutManager:
    """
    Computes layout CSS class and pane assignments for the session screen.
    """

    # CSS class names applied to #agent-area
    LAYOUT_CLASSES = {
        0: "layout-full",
        1: "layout-full",
        2: "layout-half-half",
        3: "layout-two-one",
        4: "layout-two-two",
    }
    SCROLL_LAYOUT = "layout-scroll"

    def get_layout_class(self, agent_count: int) -> str:
        if agent_count <= 0:
            return self.LAYOUT_CLASSES[0]
        if agent_count <= 4:
            return self.LAYOUT_CLASSES[agent_count]
        return self.SCROLL_LAYOUT

    def assign_panes(self, agents: list[AgentInfo]) -> list[PaneAssignment]:
        """
        Map active agents to grid positions.

        For the 2+1 layout (3 agents):
          - Coder gets the right column (large)
          - Architect gets top-left
          - Reviewer gets bottom-left

        For other layouts: sorted by ROLE_PRIORITY.
        """
        if not agents:
            return []

        # Sort by role priority — coder first
        def priority(a: AgentInfo) -> int:
            try:
                return ROLE_PRIORITY.index(a.role)
            except ValueError:
                return 99

        sorted_agents = sorted(agents, key=priority)
        n = len(sorted_agents)
        assignments = []

        if n == 1:
            a = sorted_agents[0]
            assignments.append(PaneAssignment(
                agent_id=a.agent_id, role=a.role,
                css_id=f"agent-pane-{a.agent_id}",
                col=0, row=0, large=True,
            ))

        elif n == 2:
            for i, a in enumerate(sorted_agents):
                assignments.append(PaneAssignment(
                    agent_id=a.agent_id, role=a.role,
                    css_id=f"agent-pane-{a.agent_id}",
                    col=i, row=0, large=(i == 0),
                ))

        elif n == 3:
            # 2+1: coder on right (col 1), others stacked left (col 0)
            coder_idx = next(
                (i for i, a in enumerate(sorted_agents) if a.role == "coder"),
                0
            )
            coder = sorted_agents[coder_idx]
            others = [a for a in sorted_agents if a.agent_id != coder.agent_id]

            assignments.append(PaneAssignment(
                agent_id=coder.agent_id, role=coder.role,
                css_id=f"agent-pane-{coder.agent_id}",
                col=1, row=0, large=True,
            ))
            for i, a in enumerate(others):
                assignments.append(PaneAssignment(
                    agent_id=a.agent_id, role=a.role,
                    css_id=f"agent-pane-{a.agent_id}",
                    col=0, row=i, large=False,
                ))

        else:
            # 2×2 grid or scrollable
            for i, a in enumerate(sorted_agents):
                assignments.append(PaneAssignment(
                    agent_id=a.agent_id, role=a.role,
                    css_id=f"agent-pane-{a.agent_id}",
                    col=i % 2, row=i // 2, large=(i == 0),
                ))

        return assignments

    def should_show_agent(self, agent: AgentInfo) -> bool:
        """
        Determine if an agent should have a visible pane.
        Active agents always show. Done/hibernating agents show until cleaned up.
        Agents that never ran (no started_at) are excluded.
        """
        if not agent.started_at:
            return False
        return agent.status not in ("", )

    def filter_display_agents(self, agents: list[AgentInfo]) -> list[AgentInfo]:
        """Return only agents that should currently have panes."""
        return [a for a in agents if self.should_show_agent(a)]