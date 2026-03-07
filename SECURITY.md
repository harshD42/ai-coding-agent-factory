# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅ Yes    |

## Threat Model

This system is designed for **local, single-user deployment**. It is not designed to be exposed to the internet or run in a multi-tenant environment without additional hardening.

Known security boundaries:
- The executor container has `cap_drop: ALL`, `no-new-privileges`, `mem_limit`, `pids_limit`
- The executor has no internet access and no Docker socket
- The orchestrator reads the workspace read-only; only the executor writes
- Patch validation rejects binary patches, permission changes, and oversized diffs
- Context sanitization strips known prompt injection patterns

Known non-hardened areas (do not expose publicly without addressing):
- The orchestrator API has no authentication — add a reverse proxy with auth for multi-user
- Redis has no password by default — add `requirepass` in `redis.conf` for network exposure
- ChromaDB has no authentication by default

## Reporting a Vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Report vulnerabilities by emailing: **harsh.dwivedi42@gmail.com**

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive a response within 72 hours. We will coordinate disclosure after a fix is available.