# Hardware Requirements

AICAF scales from a MacBook to an 8×H100 cluster with no code changes — only a profile switch.

## Profiles

Start with the correct profile for your hardware:

```bash
# Laptop / no GPU
docker compose --profile laptop up -d

# Single GPU (≥20GB VRAM)
docker compose --profile gpu-shared up -d

# Multi-GPU (3+ GPUs, ≥80GB total VRAM)
docker compose --profile gpu up -d

# Auto-detect (picks laptop/gpu-shared/gpu based on nvidia-smi)
PROFILE=auto docker compose up -d
```

---

## Profile Details

### `laptop` — CPU or integrated GPU

| Requirement | Value |
|---|---|
| Min RAM | 16GB |
| GPU | Not required |
| VRAM | 0 (CPU inference) |
| Model server | Ollama |

**Models loaded automatically:**
- `qwen2.5-coder:7b` — all agent roles (7B parameter model, ~4GB on CPU)
- `nomic-embed-text` — embeddings

**Performance:** Slow (CPU inference). Expect 5–30 tokens/second depending on hardware. Suitable for development and testing.

---

### `gpu-shared` — Single GPU

| Requirement | Value |
|---|---|
| Min VRAM | 20GB |
| Recommended VRAM | 48GB+ |
| GPU | Any NVIDIA with CUDA 12+ |
| Model server | vLLM |

**Models:**
- `Qwen/Qwen3-Coder-Next-80B-A3B-Instruct` (default, requires ~46GB) — all roles share one instance
- Override via `SHARED_MODEL` env var to use a smaller model on less VRAM

**Smaller model options for `gpu-shared`:**

| Model | VRAM (approx) | Notes |
|---|---|---|
| `Qwen/Qwen3.5-35B-A3B` | ~20GB | Good architect/coder balance |
| `Qwen/QwQ-32B` | ~20GB | Strong reasoning, slower |
| `qwen2.5-coder:32b` via Ollama | ~20GB | Fall back to Ollama on same GPU |

**Performance:** Fast. Expect 30–80 tokens/second on an A100/H100.

---

### `gpu` — Multi-GPU

| Requirement | Value |
|---|---|
| Min GPUs | 3 |
| Min total VRAM | 80GB |
| Recommended | 3× A100 80GB or 3× H100 80GB |
| Model server | vLLM (3 instances) |

**Model assignment per service:**

| Service | Default model | VRAM (approx) | Roles served |
|---|---|---|---|
| `vllm-coder` (`:8001`) | Qwen3-Coder-Next-80B | 46GB | coder, tester |
| `vllm-architect` (`:8002`) | Qwen3.5-35B-A3B | 20GB | architect, documenter |
| `vllm-reviewer` (`:8003`) | QwQ-32B | 20GB | reviewer |

**GPU assignment** (configurable via env vars):

```bash
CODER_GPU=0      # GPU index for coder model
ARCHITECT_GPU=1  # GPU index for architect model
REVIEWER_GPU=2   # GPU index for reviewer model
```

For tensor parallelism across multiple GPUs per model:

```bash
CODER_TP=2       # tensor-parallel-size for coder (uses 2 GPUs)
ARCHITECT_TP=1
REVIEWER_TP=1
```

**Performance:** Fastest. All roles run in parallel. Expect 80–200 tokens/second per agent.

---

## PROFILE=auto Detection Logic

When `PROFILE=auto` is set, the orchestrator runs `nvidia-smi` at startup and selects:

| Condition | Selected profile |
|---|---|
| ≥3 GPUs AND ≥120GB total VRAM | `gpu` |
| ≥1 GPU AND ≥20GB total VRAM | `gpu-shared` |
| No GPU or nvidia-smi unavailable | `laptop` |

The selection is logged at WARNING level so it always appears in startup logs:

```
WARNING  config  PROFILE=auto detected 3 GPU(s), 245760MB total VRAM → selected profile: gpu
```

---

## Per-Session Model Override

Regardless of profile, individual sessions can use different models via the API:

```bash
curl -X POST http://localhost:9000/v1/session/configure \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "my-session",
    "models": {
      "architect": "Qwen/Qwen3.5-35B-A3B",
      "coder":     "qwen2.5-coder:7b",
      "reviewer":  "Qwen/QwQ-32B",
      "tester":    "qwen2.5-coder:7b",
      "documenter": "Qwen/Qwen3.5-35B-A3B"
    }
  }'
```

Session overrides are stored in Redis with a 24h TTL. Agents for that session use the configured models instead of profile defaults.

---

## Minimum System Requirements (all profiles)

| Component | Requirement |
|---|---|
| Docker | 24.0+ with Compose V2 |
| Docker RAM allocation | 16GB minimum |
| NVIDIA Container Toolkit | Required for gpu / gpu-shared profiles |
| CUDA | 12.0+ |
| Disk | 50GB+ for model weights cache |
| OS | Linux (recommended), macOS (laptop profile only), Windows WSL2 |

---

## Notes on VRAM Estimates

All `vram_approx_gb` values in the model catalog are indicative only. Actual VRAM usage depends on:

- **Quantization** — INT4/INT8 models use significantly less VRAM than FP16
- **Tensor parallelism** — splitting across GPUs changes per-GPU usage
- **KV cache size** — longer context windows require more KV cache memory
- **Batch size** — concurrent requests increase memory proportionally

Use `nvidia-smi` to monitor actual VRAM usage during inference.