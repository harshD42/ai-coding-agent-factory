# AICAF TUI

Terminal interface for the AI Coding Agent Factory. Requires the orchestrator running at `http://localhost:9000` (or a remote URL).

## Install

```bash
# From the AICAF repo root:
pipx install ./tui

# Verify:
aicaf --version
```

## Usage

```bash
aicaf                                    # launch project picker
aicaf --url http://gpu-server:9000       # connect to remote orchestrator
aicaf --project proj-a3f9b2             # open specific project
aicaf --session sess-abc123             # open specific session
aicaf --chat --role coder               # open chat mode directly
aicaf --dev                             # dev mode (CSS hot-reload)
```

## Keyboard shortcuts

### All screens
| Key | Action |
|-----|--------|
| `Ctrl+Q` | Quit |
| `?` | Help |
| `Esc` | Back / dismiss overlay |

### Launcher
| Key | Action |
|-----|--------|
| `↑↓` / `j k` | Navigate projects |
| `Enter` | Open project |
| `n` | New project |
| `c` | Quick chat |

### Session screen
| Key | Action |
|-----|--------|
| `Tab` | Cycle pane focus |
| `f` | Follow streaming agent |
| `d` | Toggle DAG sidebar |
| `l` | Open log viewer |
| `m` | Reconfigure models |
| `n` | New session |
| `p` | Go to project |
| `/` | Command palette |
| `@role text` | Route message to agent |
| `Ctrl+C` | Cancel focused agent |

### Chat mode
| Key | Action |
|-----|--------|
| `r` | Cycle agent role |
| `m` | Cycle available models |
| `p` | Go to project launcher |

## Config location

| OS | Path |
|----|------|
| Linux | `~/.config/aicaf/` |
| macOS | `~/Library/Application Support/aicaf/` |
| Windows | `%APPDATA%\aicaf\` |

Files: `projects.json`, `ui_state.json`, `logs/`

## Requirements

- Python 3.12+
- A terminal with 256-color or true-color support
- Windows Terminal, iTerm2, GNOME Terminal, or equivalent
- AICAF orchestrator running at v0.5.0+