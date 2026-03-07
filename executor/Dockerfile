# executor/Dockerfile
FROM python:3.12-slim

# Install system dependencies + common dev tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl jq make gcc g++ patch \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Go (optional — remove if not needed)
RUN curl -fsSL https://go.dev/dl/go1.22.0.linux-amd64.tar.gz \
    | tar -C /usr/local -xzf -
ENV PATH="/usr/local/go/bin:${PATH}"

# Rust (optional — remove if not needed)
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Workspace will be mounted at runtime
RUN mkdir -p /workspace

EXPOSE 9001

# Run as root — actual security boundary is compose-level:
# cap_drop: ALL, no-new-privileges, mem_limit, pids_limit
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9001"]