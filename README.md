# credit-keeper

> Proxy giữ "credit" cho từng request: viết lại header HTTP/HTTPS theo file YAML.
> A header-keeping HTTP/HTTPS proxy that rewrites request and response headers from a YAML config.

## What it does

`credit-keeper` is a small Python CLI built on top of [mitmproxy](https://mitmproxy.org/)
as a library. You declare rules in YAML; it runs an HTTP/HTTPS proxy that mutates
request and response headers as traffic flows through. Each rule can target
traffic by host (glob or regex), URL regex, and HTTP method, then `set`, `add`,
`remove`, or regex-`replace` headers on requests, responses, or both.

## Status

Header rules engine complete, mitmproxy integration in next feature.

This first slice ships:

- the typed config model (`Config`, `Rule`, `HeaderOp`) and a YAML loader with
  validation that fails loudly with the offending rule's name,
- a pure-Python rule matcher and header mutator that works against either a
  mitmproxy `Headers` object or the bundled `CaseInsensitiveHeaders` helper,
- a pytest suite covering matching, every header action, and config validation,
- an annotated example config at `examples/headers.example.yaml`.

The CLI entry point and the mitmproxy addon land in the next feature.

## Quick start (dev)

```bash
export PATH="$HOME/.local/bin:$PATH"
uv sync --python 3.12 --extra dev
uv run --python 3.12 pytest -q
```

## Config schema

A config file is a YAML mapping with a single top-level key, `rules`, holding
a list of rules. Each rule has optional filters (combined with AND) and lists
of header operations.

### Rule

| Field        | Type                                        | Default     | Description |
|--------------|---------------------------------------------|-------------|-------------|
| `name`       | string (required)                           | -           | Human-readable identifier; surfaced in error messages. |
| `host`       | string                                      | none        | `fnmatch` glob, case-insensitive (e.g. `*.api.example.com`). |
| `host_regex` | string                                      | none        | Python regex applied to the request host with `re.search`. |
| `url_regex`  | string                                      | none        | Python regex applied to the full URL with `re.search`. |
| `methods`    | list of strings                             | none        | HTTP methods to match (case-insensitive). |
| `apply_to`   | `"request"` \| `"response"` \| `"both"`     | see below   | Which direction the rule fires on. |
| `request`    | list of header ops                          | `[]`        | Ops applied on the outgoing request. |
| `response`   | list of header ops                          | `[]`        | Ops applied on the incoming response. |

`apply_to` defaults to `"request"`. If you omit it and the rule only has
`response` ops, it auto-flips to `"response"`. If both `request` and `response`
ops are set, it auto-flips to `"both"`.

### HeaderOp

| Field    | Type                                                        | Required for     | Description |
|----------|-------------------------------------------------------------|------------------|-------------|
| `action` | `"set"` \| `"add"` \| `"remove"` \| `"replace"`             | always           | What to do with the header. |
| `name`   | string                                                      | always           | Header name (case-insensitive on match). |
| `value`  | string                                                      | `set`, `add`, `replace` | New value, additional value, or replacement string (supports regex backrefs like `\1`). |
| `pattern`| string (regex)                                              | `replace`        | Pattern matched against each existing value. |

Action semantics:

- `set` overwrites all existing values for that header with one new value.
- `add` appends another value, keeping any existing ones.
- `remove` deletes the header if present; missing header is a no-op.
- `replace` runs `re.sub(pattern, value, current_value)` against every existing
  value of the header. Backreferences in `value` work, e.g. `pattern: "Bearer (.*)"`,
  `value: "Bearer redacted-\1"`.

See `examples/headers.example.yaml` for a fully annotated sample.
