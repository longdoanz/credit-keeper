# Spec — Warning-mode credit blending

> This document is the source of truth for implementation. Subagents read this file instead of re-reading the brainstorming conversation.

## 1. Goal

- **Problem to solve**: When a pooled user is near credit depletion (e.g., 1900/2000 used), the user's own account gets fully drained quickly because credit-keeper rotates ALL their requests to pool only when fully exhausted. This means the user has a sudden cliff: full access → 0 → rely entirely on others. There is no smooth gradient where the user could extend their own credit lifetime by mixing in pool usage.
- **Value delivered**: A user near depletion enters a "warning" state where their requests are split between their own account and the pool according to a configurable ratio (e.g., 20% own / 80% pool). This stretches the user's remaining credits over a much longer period — for example, 200 remaining credits at 20% own ratio effectively serve 1000 requests before true depletion.

## 2. Scope

**In scope**

- New config fields `warning_threshold_pct` and `warning_blend_ratio` under `credential_pool`.
- New helper `CredentialDB.is_in_warning(client_id, threshold_pct)` that computes warning state on-the-fly from the latest `usage_snapshots` row for that client.
- New helper `CredentialDB.get_warning_count(threshold_pct)` for the Web UI dashboard.
- Modified `CredentialPoolAddon.request()` to add a new "warning blend" branch between the existing "exhausted" and "normal" branches.
- An in-memory dict `self._owner_counter: dict[str, int]` on `CredentialPoolAddon` keyed by `client_id` for deterministic blending.
- Web UI: a "Warning" count card on Dashboard and a "Warning" status badge on Credentials table.
- Unit tests for blending logic, threshold detection, and Web UI display.
- Update `config.yaml` example and `CHANGELOG.md`.

**Out of scope** (state explicitly so subagents don't expand on their own)

- No new DB column or table. Warning state is derived, not stored.
- No persistence of the blending counter (it lives only in process memory).
- No reset logic for the counter on restart, on quota reset, or on warning exit. Modulo arithmetic handles long-term ratio correctness regardless of counter value.
- No exclusion of warning users from `get_best_available_credential` selection. Existing "highest remaining" tiebreaker provides natural protection.
- No detection of AWS quota reset event. The proxy passively observes via `/getUsageLimits` responses; existing `mark_available()` on snapshot already handles `is_exhausted` clearing.
- No predictive rate analysis (not "burn rate", just a fixed % threshold).
- No daily-quota lifeline behavior. This is volume-based blending, not time-windowed throttling.
- No change to `is_dead` semantics. Dead is unaffected by quota reset.

## 3. Data model

- **No new tables. No new columns.**
- New in-memory field on `CredentialPoolAddon`:
  - `self._owner_counter: dict[str, int]` — keyed by `client_id`, value is the number of requests this client has made while in warning state. Increments by 1 per intercepted request when the client is in warning. Never decrements. Never persists. Default factory: `defaultdict(int)`.
- New config dataclass fields on `CredentialPoolConfig` (in `src/credit_keeper/config.py`):
  - `warning_threshold_pct: float = 0.0` — Activate warning when `remaining / limit * 100 < this value`. Default `0.0` means feature is disabled (backward-compat).
  - `warning_blend_ratio: float = 0.0` — Fraction of requests to send through the owner's own token while in warning mode. Domain `[0.0, 1.0]`. `0.0` means "always rotate to pool" (same as exhausted behavior). `1.0` means "always use owner" (warning has no rotation effect). Default `0.0` disables the feature.
- New SQL helper methods on `CredentialDB`:
  - `is_in_warning(client_id: str, threshold_pct: float) -> bool` — returns True when the latest `usage_snapshots` row for `client_id` shows `(usage_limit - current_usage) / usage_limit * 100 < threshold_pct`. Returns False if `client_id` is empty, threshold is `<= 0`, no snapshot exists, or `usage_limit <= 0`.
  - `get_warning_count(threshold_pct: float) -> int` — returns the number of distinct `client_id` values whose latest snapshot meets the warning condition. Returns 0 if threshold is `<= 0`.

## 4. Flows & states

A request from client X falls into exactly one of three branches inside `request()`:

1. **Exhausted** — `is_exhausted(auth_hash)` returns True → always rotate (existing behavior).
2. **Warning** — `is_exhausted == False` AND `is_in_warning(client_id, threshold)` returns True → blend.
3. **Normal** — neither → forward as-is (existing behavior).

**Happy path (warning + blend_ratio = 0.2)**

1. Request arrives with `Authorization: Bearer <token>`.
2. Existing logic stores `auth_hash` and `client_id` (if known) in `flow.metadata` and writes a row to `request_log`.
3. `is_exhausted(auth_hash)` is False → skip rotation.
4. `config.warning_blend_ratio > 0` AND `is_in_warning(client_id, threshold_pct)` returns True → enter blend.
5. `every_nth = round(1 / blend_ratio)` → `5`.
6. `self._owner_counter[client_id] += 1` → e.g. counter becomes 7.
7. `7 % 5 == 2`, not zero → rotate request through pool.
8. `get_best_available_credential(exclude_auth_hash=auth_hash)` returns another credential, header is swapped, log line `"warning-blend: rotated client_id X (counter=7) to <hash>"` is emitted at INFO level.
9. If selection returns None → log a warning, leave the header unchanged (request goes through with owner token; matches existing fallback behavior).
10. Eventually the counter reaches a multiple of 5 → no rotation, owner token used as-is, log line `"warning-blend: client_id X owner-turn (counter=N)"` at DEBUG level. Counter still increments.

**Error branches / edge cases**

- `client_id` unknown (request before first `/getUsageLimits` response captured for this token): treat as not-in-warning, do not increment counter, take the normal branch.
- `warning_blend_ratio == 0.0` (default): the whole warning branch is bypassed via a `if config.warning_blend_ratio <= 0:` guard at the top, so `is_in_warning` is not even called. Keeps the addon zero-overhead for users who don't opt in.
- `warning_blend_ratio == 1.0`: `every_nth = round(1/1.0) = 1`, so `counter % 1 == 0` always → no rotation ever. Effectively warning becomes a no-op. Tolerable; explicit validation rejecting this is not required.
- `warning_blend_ratio` out of `(0.0, 1.0]` range (e.g. negative or > 1): config loader must reject with `ConfigError` at startup so misconfigurations fail fast.
- `every_nth` computed from very small ratios (e.g. 0.01 → 100) is fine; modulo still works.
- Pool empty when blend says "rotate to pool": `get_best_available_credential` returns None → log a warning, fall through with the owner token (same as current exhausted-with-empty-pool fallback). Owner's credit gets consumed; this is unavoidable when the pool is truly dry.
- Proxy restart during warning: counter dict is empty again. The next requests start counter at 1, so the first `every_nth - 1` requests rotate to pool and the `every_nth`-th uses owner. Long-term ratio is preserved; user may notice a brief skew right after restart.
- Multiple distinct auth_hashes for the same client_id (token refresh): they all share the same counter slot because the counter is keyed by `client_id`. Correct behavior.
- Snapshot stale (e.g. `/getUsageLimits` not called recently): `is_in_warning` uses the most recent row available. If usage has moved at the backend but we haven't observed it, warning state lags reality. Acceptable for the use case.
- Quota reset at backend (e.g., new month): next `/getUsageLimits` snapshot will show low `currentUsage` → `is_in_warning` returns False naturally. `is_exhausted` is cleared by existing `mark_available()`. Counter stays at its prior value but no longer ticks because the warning branch is skipped. When the same user re-enters warning later, the counter continues from where it left off; modulo still produces the correct rotation pattern.

## 5. Interface / Contract

**Config (`src/credit_keeper/config.py`)**

```python
@dataclass
class CredentialPoolConfig:
    # ... existing fields kept as-is ...
    warning_threshold_pct: float = 0.0
    warning_blend_ratio: float = 0.0
```

YAML keys: `warning_threshold_pct`, `warning_blend_ratio`. Both optional. If both are `0` (default), the feature is fully disabled and the addon behaves exactly as before.

Validation (in `load_config`):
- If present, both must parse as numeric (`int` or `float`).
- `warning_threshold_pct` must satisfy `0 <= value <= 100`; otherwise raise `ConfigError`.
- `warning_blend_ratio` must satisfy `0 <= value <= 1`; otherwise raise `ConfigError`.

**DB helpers (`src/credit_keeper/db.py`)**

```python
def is_in_warning(self, client_id: str, threshold_pct: float) -> bool: ...
def get_warning_count(self, threshold_pct: float) -> int: ...
```

Both methods must take `self._lock` for the duration of the SQLite query.

`is_in_warning` SQL sketch:
```sql
SELECT current_usage, usage_limit FROM usage_snapshots
WHERE client_id = ?
ORDER BY id DESC LIMIT 1
```
If `client_id` is empty/None, return False without querying. If `threshold_pct <= 0`, return False without querying. If no row, or `usage_limit <= 0`, return False.

`get_warning_count` SQL sketch:
```sql
SELECT COUNT(*) FROM (
  SELECT u.client_id, u.current_usage, u.usage_limit
  FROM usage_snapshots u
  INNER JOIN (
    SELECT client_id, MAX(id) AS max_id
    FROM usage_snapshots
    GROUP BY client_id
  ) latest ON u.id = latest.max_id
  WHERE u.usage_limit > 0
    AND ((u.usage_limit - u.current_usage) * 100.0 / u.usage_limit) < ?
)
```
Returns 0 if `threshold_pct <= 0`.

**Addon (`src/credit_keeper/pool_addon.py`)**

- Constructor adds `self._owner_counter: dict[str, int] = {}` (use a plain dict, manage the missing-key case explicitly).
- `request()` insertion point: after the existing `rotation if is_exhausted` block, add an `elif warning_blend_ratio > 0 and client_id and is_in_warning(...)` block that increments the counter and conditionally rotates (or not) based on the modulo.
- `client_id` is obtained by looking up `auth_hash` in the `credentials` table (a new tiny helper `get_client_id_by_auth_hash(auth_hash)` may be added if needed; alternatively re-use `is_dead` pattern of fetching via a SELECT). Subagent: prefer adding a single helper to avoid repeated SELECTs.

**Web UI (`src/credit_keeper/webui/app.py`)**

- Dashboard: compute `warning = db.get_warning_count(threshold_pct)` (read `threshold_pct` from config — passed into `create_app` if needed, or via a closure). Pass `warning` into the template context.
- `active = total - exhausted - dead` becomes `active = total - exhausted - dead - warning` (warning is mutually exclusive with exhausted/dead because warning requires `is_exhausted == 0` per business rule — verify).

Caveat for subagent: a credential row in DB can have `is_exhausted = 0` AND `is_dead = 0` but the user is in warning (warning is per-client computed, not per-credential). The "Active vs Warning" buckets count rows of the same physical credential potentially under different categories. Subagent should choose a clear UI semantic and document it in the template comments.

Recommended UI semantic: card numbers count credential ROWS (not client_ids), but the "Warning" badge in the Credentials table marks rows whose owning client_id is in warning. The dashboard "Warning" card uses `get_warning_count` which counts distinct client_ids in warning. Subagent may note the discrepancy with a tooltip or short label like "Warning users" vs "Warning credentials" — pick one and stick with it.

**Template changes**

- `dashboard.html`: change `md:grid-cols-5` to `md:grid-cols-6`, insert a Warning card between Active and Exhausted with yellow text (Tailwind `text-yellow-600`).
- `credentials.html`: add a new badge "Warning" (yellow). Status priority order (highest first): Dead → Exhausted → Warning → Active.

**Script (`scripts/exhaust_user.py`)**

- Output of `show` does not need a new column (warning is dynamic); leave as-is unless the subagent finds an idiomatic spot.

## 6. Dependencies & technical constraints

- Modules/services touched: `config.py`, `db.py`, `pool_addon.py`, `webui/app.py`, `webui/templates/dashboard.html`, `webui/templates/credentials.html`.
- Tests: `tests/test_config.py`, `tests/test_db.py`, `tests/test_pool_addon.py`, `tests/test_webui.py`.
- Docs: `config.yaml` example, `CHANGELOG.md` entry under `[Unreleased]`.
- Libraries used: stdlib only (`sqlite3`, `threading`). No new dependency.
- Required conventions extracted from the codebase:
  - Dataclasses with `field(default_factory=...)` for collections.
  - SQL queries inside `with self._lock:` blocks; commit explicitly with `self._conn.commit()` when mutating.
  - mitmproxy addon hooks documented with `# type: ignore[no-untyped-def]` because mitmproxy types are missing.
  - Logging via `logger = logging.getLogger(__name__)`; truncate hashes to `[:8]` in log lines.
  - Test files use plain `pytest` style (functions, not classes); fixtures with `tmp_path` for DB.
  - YAML validation raises `ConfigError` (subclass of `ValueError`) with a descriptive message.
  - Templates use Tailwind classes already loaded via CDN in `base.html`.
  - Vietnamese is acceptable in user-facing docs and CHANGELOG; English in code comments and identifiers.

## 7. Non-functional

- **Performance**: `is_in_warning` runs on every intercepted request when the feature is enabled. The SQL is a single indexed lookup on `usage_snapshots` (existing `client_id` column; if no index exists, consider adding `CREATE INDEX IF NOT EXISTS idx_snapshots_client ON usage_snapshots(client_id, id)` — small win). For pools of <100 users this is negligible.
- **Concurrency**: The in-memory counter is mutated from mitmproxy event-loop threads. Because mitmproxy 12 processes flows on a single asyncio loop, the dict operations are effectively single-threaded; no explicit lock needed. The Web UI runs in a separate daemon thread but reads only DB, never touches `_owner_counter`.
- **Logging**: INFO line on rotation due to blending; DEBUG line on owner-turn; WARNING on pool-empty fallback. Hash truncated to 8 chars. `client_id` is included in the log to aid debugging.
- **Security**: No new secret-handling. Tokens still hashed before storage.
- **Backward compat**: Feature is opt-in via config; defaults make `pool_addon.py` and Web UI behave identically to current version.

## 8. Done criteria

- [ ] `warning_threshold_pct` and `warning_blend_ratio` parse correctly from YAML; out-of-range values raise `ConfigError`.
- [ ] With `warning_blend_ratio: 0.0`, the addon behaves exactly as the current code (no extra DB calls, no log lines).
- [ ] With `warning_blend_ratio: 0.2` and a client in warning, exactly 1 of every 5 requests forwards with the owner token; the other 4 are rotated to the pool.
- [ ] When the client's latest snapshot drops back below the threshold (e.g., after a quota reset), the warning branch is no longer entered.
- [ ] Web UI Dashboard displays a "Warning" count card and the number matches `db.get_warning_count`.
- [ ] Credentials table shows a yellow "Warning" badge for rows whose client_id is in warning, ordered after Dead and Exhausted in priority.
- [ ] All existing tests still pass (`uv run python -m pytest -x -q` → 83 + new tests).
- [ ] New tests cover: config parsing (valid + invalid), `is_in_warning` boundary conditions, `get_warning_count` aggregation, the request-modulo selection in `pool_addon`, the dashboard rendering.
- [ ] `CHANGELOG.md` `[Unreleased]` section lists the new config fields, the new DB helpers, the new addon logic, and the UI changes.
- [ ] `config.yaml` includes an example with the new fields, commented to explain when to use them.
