# credit-keeper

A shared-credit proxy that lets multiple users pool their API credentials. When one user's credits are exhausted, the proxy automatically rotates to the credential with the most remaining credits. Built on [mitmproxy 12](https://mitmproxy.org/) as a Python library.

## Features

- **Header rewriting** via YAML config (set, add, remove, replace with regex)
- **Credential pool** - intercepts Authorization headers, stores credentials in SQLite
- **Usage tracking** - intercepts `getUsageLimits` API responses to track `currentUsage` / `usageLimit` per credential
- **Smart rotation** - when a credential is exhausted, automatically swaps to the credential with the most remaining credits
- **Multi-host intercept** - configurable list of hosts to intercept (not limited to a single host)
- **Refresh token storage** - captures and stores refresh tokens for future token renewal
- **Web UI dashboard** (FastAPI + Tailwind CSS) showing:
  - Credential overview (total, active, exhausted)
  - Per-user usage share breakdown with progress bars
  - Detailed credentials table with usage, remaining, status, last activity
  - Usage history per client
  - Request log with pagination

## Installation

Requirements: Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
# Install uv if not already present:
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# Sync the environment (creates .venv and installs dependencies):
uv sync --python 3.12

# Include dev dependencies for running tests:
uv sync --python 3.12 --extra dev
```

After `uv sync`, the `credit-keeper` command is available via `uv run`:

```bash
uv run --python 3.12 credit-keeper --help
```

## Quick Start

1. Create a config file, e.g. `config.yaml`:

   ```yaml
   rules:
     - name: stamp-ua
       host: "*.example.com"
       request:
         - {action: set, name: User-Agent, value: "credit-keeper/0.1"}

   credential_pool:
     enabled: true
     intercept_hosts:
       - "q.us-east-1.amazonaws.com"
       - "q.us-west-2.amazonaws.com"
     usage_path: "/getUsageLimits"
     extract_headers: ["Authorization"]
     auto_rotate: true
     refresh_token_header: "X-Refresh-Token"
   ```

2. Start the proxy (with the web UI on port 9090):

   ```bash
   uv run --python 3.12 credit-keeper -c config.yaml --webui-port 9090
   # credit-keeper listening on 127.0.0.1:8080, loaded 1 rules from config.yaml
   ```

3. Point your browser or application to use `127.0.0.1:8080` as the HTTP proxy.

4. For HTTPS interception, install the mitmproxy CA certificate: while connected through the proxy, open [http://mitm.it](http://mitm.it) in a browser and follow the instructions for your OS. Alternatively, use `--ssl-insecure` on the proxy side and `-k` with `curl` for quick testing.

5. Open [http://127.0.0.1:9090](http://127.0.0.1:9090) to view the web dashboard.

## CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `-c`, `--config PATH` | (required) | Path to the YAML config file |
| `--listen-host HOST` | `127.0.0.1` | Address to bind the proxy on |
| `-p`, `--listen-port PORT` | `8080` | Port to listen on |
| `--mode MODE` | `regular` | mitmproxy proxy mode (regular, transparent, socks5, reverse:..., upstream:...) |
| `--ssl-insecure` | off | Do not verify upstream TLS certificates |
| `--db PATH` | `./credit-keeper.db` | Path to the SQLite database for the credential pool |
| `--webui-port PORT` | (disabled) | Port to run the web UI dashboard on |
| `--version` | | Print version and exit |

You can also run as a Python module: `python -m credit_keeper -c config.yaml`.

## Config Reference

The config is a YAML file with two top-level sections: `rules` and `credential_pool`.

### Rules

The `rules` key contains a list of header-rewriting rules. Filters within a single rule combine with AND logic.

#### Rule Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string (required) | - | Human-readable name; appears in error messages |
| `host` | string | none | `fnmatch` glob, case-insensitive (e.g. `*.api.example.com`) |
| `host_regex` | string | none | Python regex applied via `re.search` on the host |
| `url_regex` | string | none | Python regex applied via `re.search` on the full URL |
| `methods` | list of strings | none | HTTP methods to match (case-insensitive) |
| `apply_to` | `"request"` / `"response"` / `"both"` | inferred | Direction the rule applies to |
| `request` | list of header ops | `[]` | Operations applied to outgoing requests |
| `response` | list of header ops | `[]` | Operations applied to incoming responses |

When `apply_to` is omitted, it defaults to `"request"`. If only `response` ops are provided, it flips to `"response"`. If both lists have ops, it becomes `"both"`.

#### Header Op Actions

| Action | Required Fields | Example |
|--------|----------------|---------|
| `set` | `name`, `value` | `{action: set, name: User-Agent, value: "mybot/1.0"}` |
| `add` | `name`, `value` | `{action: add, name: X-Forwarded-For, value: "10.0.0.1"}` |
| `remove` | `name` | `{action: remove, name: Server}` |
| `replace` | `name`, `pattern`, `value` | `{action: replace, name: Authorization, pattern: "Bearer (.*)", value: "Bearer redacted-\\1"}` |

- `set` overwrites all existing values for the header with a single new value.
- `add` appends another value, keeping existing ones.
- `remove` deletes the header if present; no error when absent.
- `replace` runs `re.sub(pattern, value, current_value)` on each existing value. Backreferences like `\1` work as expected.

### Credential Pool

The optional `credential_pool` section enables shared-credit management.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable the credential pool feature |
| `intercept_hosts` | list of strings | `[]` | Hosts to intercept for credential tracking |
| `usage_path` | string | `"/getUsageLimits"` | URL path that returns usage data |
| `extract_headers` | list of strings | `["Authorization"]` | Headers to extract as credentials |
| `auto_rotate` | bool | `true` | Automatically swap exhausted credentials |
| `refresh_token_header` | string | `""` | Header name containing a refresh token |

Backward compatibility: `intercept_host` (single string) is still accepted and coerced to a list.

## Web UI

When `--webui-port` is set, a FastAPI web dashboard starts alongside the proxy. Pages:

| Route | Description |
|-------|-------------|
| `/` | Dashboard overview: total/active/exhausted credentials, total usage, and per-user usage share breakdown |
| `/credentials` | Table of all credentials with usage, remaining credits, usage share %, status, refresh token presence, and last activity |
| `/usage/{client_id}` | Usage snapshot history for a specific user (current_usage, usage_limit, remaining, resource_type, days_until_reset) |
| `/requests` | Paginated request log (timestamp, method, URL, host, client_id, auth_hash) |

The UI uses Tailwind CSS via CDN for styling; no build step is required.

## Project Layout

```
credit-keeper/
├── pyproject.toml                  # Build config, dependencies, console script
├── README.md
├── AGENTS.md                       # AI assistant context file
├── LICENSE                         # MIT License
├── examples/
│   └── headers.example.yaml       # Annotated example config
├── src/credit_keeper/
│   ├── __init__.py                 # Public API exports, version
│   ├── __main__.py                 # python -m credit_keeper entry point
│   ├── config.py                   # Dataclasses + YAML loader + validation
│   ├── rules.py                    # Rule matching + header op application
│   ├── addon.py                    # mitmproxy addon (HeaderCustomizer)
│   ├── pool_addon.py              # mitmproxy addon (CredentialPoolAddon)
│   ├── db.py                       # SQLite storage layer (CredentialDB)
│   └── webui/
│       ├── __init__.py
│       ├── app.py                  # FastAPI application factory
│       └── templates/              # Jinja2 HTML templates (Tailwind CSS)
│           ├── base.html
│           ├── dashboard.html
│           ├── credentials.html
│           ├── usage.html
│           └── requests.html
└── tests/
    ├── test_config.py
    ├── test_rules.py
    ├── test_addon.py
    ├── test_pool_addon.py
    ├── test_db.py
    └── test_webui.py
```

## Security

`credit-keeper` wraps mitmproxy and can MITM all HTTPS traffic passing through it. Read this section carefully.

- **CA certificate lifecycle.** To intercept HTTPS, you must install the mitmproxy CA certificate into your trust store. The cert is at `~/.mitmproxy/mitmproxy-ca-cert.pem`. Once trusted, any process that binds to the proxy port can impersonate any HTTPS site. When finished, remove the cert from your trust store and delete `~/.mitmproxy` if not needed.

- **`--ssl-insecure` semantics.** This flag disables upstream TLS certificate verification only (proxy to real server). The proxy still presents its own CA cert to clients. On untrusted networks, enabling this means that Authorization headers could be captured by a man-in-the-middle between the proxy and the upstream server.

- **Listen-host risk.** Default `--listen-host 127.0.0.1` restricts access to localhost. Changing to `0.0.0.0` or a LAN interface turns your machine into an unauthenticated TLS-intercepting gateway. `credit-keeper` logs a warning when the listen host is not loopback, but does not block binding. Only do this in firewalled/isolated environments.

- **Credential storage.** The SQLite database stores full Authorization header values (needed for rotation). Protect the `.db` file with appropriate filesystem permissions. New database files are created with mode `0600`.

- **Config secrets.** The config file may contain bearer tokens or sensitive header values. `credit-keeper` does not expand environment variables; literal values are sent on the wire. Do not commit real configs to public repositories.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for the full text.
