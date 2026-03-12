"""
main.py — $ aicaf entry point.

Usage:
  aicaf                                  # launch project picker
  aicaf --url http://gpu-server:9000     # connect to remote orchestrator
  aicaf --project proj-a3f9b2            # open specific project
  aicaf --session sess-abc123            # open specific session
  aicaf --chat [--role coder]            # open chat mode directly
  aicaf --version                        # print version and exit
"""

import argparse
import sys
from pathlib import Path

# Ensure tui package is importable when run as `python -m tui.main`
sys.path.insert(0, str(Path(__file__).parent.parent))

from tui import __version__
from tui.store import ProjectStore, DEFAULT_ORCH_URL


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="aicaf",
        description="AI Coding Agent Factory — terminal interface",
    )
    parser.add_argument(
        "--url",
        metavar="URL",
        default=None,
        help="Orchestrator URL (default: last used or http://localhost:9000)",
    )
    parser.add_argument(
        "--project",
        metavar="PROJECT_ID",
        default=None,
        help="Open a specific project by ID",
    )
    parser.add_argument(
        "--session",
        metavar="SESSION_ID",
        default=None,
        help="Open a specific session (requires --project)",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        default=False,
        help="Open chat mode directly",
    )
    parser.add_argument(
        "--role",
        metavar="ROLE",
        default="coder",
        help="Agent role for chat mode (default: coder)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"aicaf {__version__}",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        default=False,
        help="Enable Textual dev mode (CSS hot-reload)",
    )

    args = parser.parse_args()

    # Resolve orchestrator URL: CLI arg > stored last URL > default
    store = ProjectStore()
    if args.url:
        orch_url = args.url.rstrip("/")
        store.set_orchestrator_url(orch_url)
    else:
        orch_url = store.last_orchestrator_url or DEFAULT_ORCH_URL

    # Determine project_id and session_id
    project_id  = args.project
    session_id  = args.session
    chat_role   = args.role if args.chat else None

    # If session given without project, try to find matching project
    if session_id and not project_id:
        for p in store.list_projects():
            if session_id in p.session_ids:
                project_id = p.id
                break

    # Import here so --version and --help are fast
    from tui.app import AicafApp

    app = AicafApp(
        orchestrator_url=orch_url,
        initial_project=project_id,
        initial_session=session_id,
        chat_role=chat_role,
    )

    if args.dev:
        # Textual dev mode: CSS is hot-reloaded on save
        import os
        os.environ["TEXTUAL_DEV"] = "1"

    app.run()


if __name__ == "__main__":
    main()