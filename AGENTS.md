# AGENTS.md - AI Assistant Context for credit-keeper

## Project Overview

**credit-keeper** is a shared-credit proxy that enables multiple users to pool their API credentials. When one user's credits are exhausted, the proxy automatically rotates to the credential with the most remaining credits. This allows a group of users sharing the same proxy to collectively benefit from all pooled credentials without manual intervention.

The core use case: several people each have their own API subscription (e.g., Amazon Q Developer). They all route traffic through this proxy. The proxy captures their credentials, monitors usage via API responses, and when any one credential runs out, seamlessly switches to the one with the most headroom.

## Architecture

```
                     ┌─────────────────────────────────────┐
                     │            credit-keeper             │
User traffic ──────► │                                     │
                     │  ┌──────────────┐  ┌─────────────┐  │
                     │  │ HeaderCustom │  │ CredPool    │  │ ──────► Upstream API
                     │  │ izer (addon) │  │ Addon       │  │
                     │  └──────────────┘  └──────┬──────┘  │
                     │                           │         │
                     │                    ┌──────▼──────┐  │
                     │                    │  SQLite DB  │  │
                     │                    └──────┬──────┘  │
                     │                           │         │
                     │                    ┌──────▼──────┐  │
                     │                    │  FastAPI    │  │ ──────► Web UI (browser)
                     │                    │  Web UI     │  │
                     │                    └─────────────┘  │
                     └─────────────────────────────────────┘
```

- **mitmproxy** (v12, Python library mode) handles TCP/TLS, HTTP parsing, and flow lifecycle
- **HeaderCustomizer addon** applies YAML-configured header rules (set/add/remove/replace)
- **CredentialPoolAddon** intercepts credentials, tracks usage, performs smart rotation
- **SQLite** stores credentials, usage snapshots, and request logs (WAL mode, busy_timeout for concurrent access)
- **FastAPI Web UI** provides a read-only dashboard (runs in a daemon thread alongside the proxy)

## Key Modules

| Module | Responsibility |
|--------|---------------|
| `src/credit_keeper/cli.py` | CLI entry point; parses args, constructs mitmproxy DumpMaster, wires addons, optionally starts web UI |
| `src/credit_keeper/config.py` | Dataclasses (Config, Rule, HeaderOp, CredentialPoolConfig) + YAML parser + validation |
| `src/credit_keeper/rules.py` | `rule_matches()` for filter evaluation, `apply_ops()` for header mutation |
| `src/credit_keeper/addon.py` | `HeaderCustomizer` - mitmproxy addon that applies header rules per flow |
| `src/credit_keeper/pool_addon.py` | `CredentialPoolAddon` - mitmproxy addon for credential capture, usage tracking, and rotation |
| `src/credit_keeper/db.py` | `CredentialDB` - thread-safe SQLite storage with CRUD + query methods |
| `src/credit_keeper/webui/app.py` | FastAPI app factory with routes for dashboard, credentials, usage, requests |

## How the Credential Pool Works

The credential pool operates in three phases:

### 1. Capture (request hook)
- Checks if the request host is in `intercept_hosts`
- Extracts the Authorization header (or other configured headers)
- Computes a SHA-256 hash of the credential for identification
- Optionally extracts a refresh token header
- Logs the request to the `request_log` table

### 2. Track (response hook)
- Watches for responses to the `usage_path` endpoint (default: `/getUsageLimits`)
- Parses the JSON response to extract: `currentUsage`, `usageLimit`, `userId`, `subscriptionTitle`, `daysUntilReset`
- Upserts the credential record and inserts a usage snapshot
- Marks the credential as exhausted if `currentUsage >= usageLimit`

### 3. Rotate (request hook, auto_rotate=true)
- Before forwarding a request, checks if the current credential is exhausted
- If exhausted, calls `get_best_available_credential()` which picks the credential with the highest `(usage_limit - current_usage)` from the latest snapshot
- Swaps the Authorization header in the outgoing request
- Falls back to random selection if no usage data exists

## Config Format

```yaml
rules:
  - name: <string>           # Required identifier
    host: "<glob>"           # Optional fnmatch filter
    host_regex: "<regex>"    # Optional regex filter on host
    url_regex: "<regex>"     # Optional regex filter on full URL
    methods: [GET, POST]     # Optional method filter
    apply_to: request|response|both  # Optional, inferred if omitted
    request:                 # Header ops for requests
      - {action: set|add|remove|replace, name: <header>, value: ..., pattern: ...}
    response:                # Header ops for responses
      - ...

credential_pool:
  enabled: true
  intercept_hosts: ["host1.example.com", "host2.example.com"]
  usage_path: "/getUsageLimits"
  extract_headers: ["Authorization"]
  auto_rotate: true
  refresh_token_header: "X-Refresh-Token"
```

## Running Tests

```bash
export PATH="$HOME/.local/bin:$PATH"
uv sync --python 3.12 --extra dev
uv run --python 3.12 pytest -q
```

The test suite uses `mitmproxy.test.tflow` to construct fake HTTPFlow objects, so no real proxy needs to run during tests. The `test_webui.py` uses FastAPI's TestClient (backed by httpx).

## Development Workflow

1. Make changes in `src/credit_keeper/`
2. Run `uv run --python 3.12 pytest -q` to verify
3. Smoke-test the CLI: `uv run --python 3.12 credit-keeper --help`
4. Optionally run the proxy: `uv run --python 3.12 credit-keeper -c examples/headers.example.yaml -p 18080 --webui-port 9090`

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **mitmproxy as a library** | De-facto Python HTTPS proxy with a stable addon API. Handles TLS interception, CA cert generation, HTTP/2, and flow lifecycle out of the box. |
| **SQLite for storage** | Zero-config, file-based, good enough for local multi-user tracking. WAL mode enables concurrent read (web UI) and write (proxy) without blocking. |
| **FastAPI for web UI** | Lightweight, async-compatible, excellent for Jinja2 templating. Runs in a daemon thread so it does not block the mitmproxy event loop. |
| **SHA-256 credential hashing** | Allows identification and deduplication without comparing full auth headers in queries. |
| **Usage-based rotation (not random)** | Maximizes total available credits by always picking the credential with the most remaining headroom. Falls back to random if no usage data exists yet. |
| **YAML config** | Human-readable, supports complex structures (lists of rules, nested ops), widely understood. |

## Important Notes

- Python 3.12+ is required (mitmproxy 12 depends on it)
- The project uses `uv` for dependency management (not pip directly)
- mitmproxy 12 removed `ctx.log`; use `logging.getLogger(__name__)` instead
- DumpMaster must be constructed inside an async context (mitmproxy 12.2.3 requirement)
- The web UI uses Tailwind CSS via CDN; no frontend build step is needed
- Database files are created with mode 0600 for security
- The `intercept_host` (singular string) config key is still accepted for backward compatibility
