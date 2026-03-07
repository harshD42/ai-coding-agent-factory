# Deployment Guide

## Prerequisites

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| Docker | 24.0+ | Latest |
| Docker Compose | v2.20+ | Latest |
| RAM | 8 GB | 16 GB |
| Disk | 10 GB | 50 GB (models) |
| OS | Windows 10, Ubuntu 20.04, macOS 12 | Ubuntu 22.04 |

For GPU profiles, additionally:
- NVIDIA GPU with CUDA 12.1+
- NVIDIA Container Toolkit installed

---

## Profile Selection

### Laptop Profile (CPU / any GPU)
Best for: development, testing, low-power machines.
- Model: `qwen2.5-coder:7b` via Ollama (~5 GB download)
- RAM: 8 GB minimum, 16 GB recommended
- GPU: optional (Ollama uses it automatically if available)

```bash
docker compose --profile laptop up -d
```

### Single GPU Profile
Best for: RTX 3090/4090, A6000 (24GB+ VRAM).
- Model: `Qwen3-Coder-Next-80B-A3B` via vLLM (~46 GB, requires quant)
- For 24 GB VRAM, set `SHARED_MODEL=Qwen/Qwen2.5-Coder-32B-Instruct` instead

```bash
docker compose --profile gpu-shared up -d
```

### Multi-GPU Profile
Best for: servers with 3+ GPUs (A100/H100/A6000).
- Coder: GPU 0, Architect: GPU 1, Reviewer: GPU 2
- Each role gets a dedicated model instance

```bash
docker compose --profile gpu up -d
```

---

## Configuration

Copy and edit the environment file:
```bash
cp .env.example .env
nano .env  # or notepad .env on Windows
```

**Required settings:**
```bash
PROFILE=laptop              # laptop | gpu-shared | gpu
PROJECT_PATH=/path/to/your/project   # your codebase
```

**For GPU profiles, also set:**
```bash
HF_TOKEN=hf_your_token     # HuggingFace token for gated models
CODER_GPU=0
ARCHITECT_GPU=1
REVIEWER_GPU=2
```

---

## First Run

```bash
# 1. Start the stack
docker compose --profile laptop up -d

# 2. Wait for models to download (first run only — several GB)
docker compose logs -f ollama-setup
# Wait for: "Models ready!"

# 3. Verify all containers are healthy
docker compose ps

# 4. Index your codebase
curl -X POST http://localhost:9000/v1/index

# 5. Test the API
curl http://localhost:9000/health
```

---

## Pointing at Your Project

Edit `.env`:
```bash
PROJECT_PATH=/absolute/path/to/your/project
```

Then restart the containers that mount the workspace:
```bash
docker compose restart executor orchestrator
```

Re-index after pointing at a new project:
```bash
curl -X POST http://localhost:9000/v1/index
# or
.\cli\agent.ps1 index
```

---

## Connecting Your IDE

### Roo Code (Recommended)
1. Install "Roo Code" from VS Code marketplace
2. Open Roo Code settings
3. Set API Provider: `OpenAI Compatible`
4. Set Base URL: `http://localhost:9000/v1`
5. Set API Key: `local` (any non-empty string)
6. Set Model ID: `orchestrator`
7. **Switch mode to "Chat"** (not Agent/Code)

### Open WebUI
```bash
docker compose --profile laptop --profile monitor up -d
```
Open `http://localhost:3000` in your browser.

### CLI
```powershell
.\cli\agent.ps1 status
.\cli\agent.ps1 architect "describe your task"
```

---

## Remote Server Setup

If running on a remote GPU server:

```bash
# On the server
docker compose --profile gpu up -d

# On your laptop — set the server IP
export ORCH_URL=http://YOUR_SERVER_IP:9000

# Test connection
curl http://YOUR_SERVER_IP:9000/health
```

In Roo Code, set Base URL to `http://YOUR_SERVER_IP:9000/v1`.

**Security note:** Do not expose port 9000 publicly without adding authentication (nginx reverse proxy + basic auth or OAuth).

---

## Updating

```bash
git pull
docker compose --profile laptop up -d --build
```

---

## Troubleshooting

**Containers not starting:**
```bash
docker compose ps          # check status
docker compose logs <name> # check logs
```

**Ollama model not loading:**
```bash
docker exec aicaf-ollama-1 ollama list
docker exec aicaf-ollama-1 ollama pull qwen2.5-coder:7b
```

**"corrupt patch" errors:**
Ensure your workspace files have LF line endings. The executor normalizes CRLF automatically, but if issues persist:
```bash
docker exec aicaf-executor-1 find /workspace -name "*.py" -exec sed -i 's/\r//' {} \;
```

**Out of memory:**
Reduce context window in `.env`:
```bash
MAX_CONTEXT_TOKENS=8000
CODER_CTX=8192
```

**ChromaDB telemetry error:**
The `capture() takes 1 positional argument but 3 were given` error in logs is a harmless bug in ChromaDB's telemetry client. It does not affect functionality.