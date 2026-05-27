# Plan — Warning-mode credit blending

> Every task MUST be self-contained: a fresh-read subagent (no conversation context) must be able to execute it with just the task + spec.md.
> Read `spec.md` in the same folder for overall context.

## Execution order

Tasks are ordered by dependency. Tasks marked `(parallel with Tx)` may be done concurrently with the listed task once their own dependencies are satisfied.

1. Task 1 — Config schema and YAML loader
2. Task 2 — DB helpers `is_in_warning` and `get_warning_count` (parallel with Task 1)
3. Task 3 — Addon: blending logic + in-memory counter (depends on 1, 2)
4. Task 4 — Web UI: Dashboard "Warning" card (depends on 2)
5. Task 5 — Web UI: Credentials table "Warning" badge (depends on 2, parallel with 4)
6. Task 6 — Example `config.yaml` update (parallel with 3)
7. Task 7 — CHANGELOG entry under `[Unreleased]` (after 1–5 land)
8. Task 8 — Smoke verification: full test run + manual `uv run credit-keeper -c config.yaml ...`

---

## Task 1 — Config schema and YAML loader

- **Type:** edit
- **Files involved:**
  - `src/credit_keeper/config.py`
  - `tests/test_config.py`
- **Depends on:** none
- **What to do:**
  1. In `src/credit_keeper/config.py`, add two fields to the `CredentialPoolConfig` dataclass right after `refresh_token_header`:
     ```python
     warning_threshold_pct: float = 0.0
     warning_blend_ratio: float = 0.0
     ```
  2. In `load_config`, inside the `credential_pool` parsing block (the section that already builds `usage_path`, `extract_headers`, etc.), read the two new keys with defaults `0.0`:
     ```python
     warning_threshold_pct = pool_raw.get("warning_threshold_pct", 0.0)
     warning_blend_ratio = pool_raw.get("warning_blend_ratio", 0.0)
     ```
  3. Add validation right after reading them:
     - Both must be numeric (`int` or `float`). Reject `str`, `bool` (note: `bool` is a subclass of `int`, but Python's `isinstance(True, int)` is True; for simplicity accept both `int` and `float` here — config files won't contain literal booleans for these fields).
     - `warning_threshold_pct` must satisfy `0 <= value <= 100`. Out of range → `raise ConfigError("'credential_pool.warning_threshold_pct' must be between 0 and 100")`.
     - `warning_blend_ratio` must satisfy `0 <= value <= 1`. Out of range → `raise ConfigError("'credential_pool.warning_blend_ratio' must be between 0 and 1")`.
  4. Pass the two new values into the `CredentialPoolConfig(...)` constructor call at the end of the block.
  5. In `tests/test_config.py`, add tests:
     - `test_credential_pool_parses_warning_fields`: load a YAML with both fields set to non-zero values; assert the resulting `CredentialPoolConfig` exposes them correctly.
     - `test_credential_pool_warning_fields_default_zero`: load a YAML without the keys; assert both default to `0.0`.
     - `test_warning_threshold_pct_out_of_range`: YAML with `warning_threshold_pct: 150`; assert `load_config` raises `ConfigError`.
     - `test_warning_blend_ratio_out_of_range`: YAML with `warning_blend_ratio: 1.5`; assert raises `ConfigError`.
- **Constraints / conventions:**
  - Use the existing `ConfigError` (subclass of `ValueError`) for validation failures.
  - Match the existing pattern of `pool_raw.get(key, default)` followed by type-then-range checks.
  - Keep field order consistent with the rest of the dataclass (new fields at the end).
- **Done criteria:**
  - [ ] New fields exist on `CredentialPoolConfig`.
  - [ ] `load_config` populates them from YAML.
  - [ ] Invalid values raise `ConfigError`.
  - [ ] All four new tests pass.
- **How to verify:**
  ```bash
  uv run python -m pytest tests/test_config.py -x -q
  ```

---

## Task 2 — DB helpers `is_in_warning` and `get_warning_count`

- **Type:** edit
- **Files involved:**
  - `src/credit_keeper/db.py`
  - `tests/test_db.py`
- **Depends on:** none (parallel with Task 1)
- **What to do:**
  1. In `src/credit_keeper/db.py`, add a new method on `CredentialDB` placed near `is_dead` for thematic grouping:
     ```python
     def is_in_warning(self, client_id: str, threshold_pct: float) -> bool:
         """Return True when the latest usage snapshot for client_id shows
         remaining/limit*100 < threshold_pct. Returns False for empty
         client_id, non-positive threshold, missing snapshot, or non-positive
         usage_limit.
         """
         if not client_id or threshold_pct <= 0:
             return False
         with self._lock:
             row = self._conn.execute(
                 """
                 SELECT current_usage, usage_limit FROM usage_snapshots
                 WHERE client_id = ?
                 ORDER BY id DESC LIMIT 1
                 """,
                 (client_id,),
             ).fetchone()
         if row is None:
             return False
         current_usage, usage_limit = row
         if not usage_limit or usage_limit <= 0:
             return False
         remaining_pct = (usage_limit - current_usage) * 100.0 / usage_limit
         return remaining_pct < threshold_pct
     ```
  2. Add a second method placed near `get_dead_count`:
     ```python
     def get_warning_count(self, threshold_pct: float) -> int:
         """Number of distinct client_ids whose latest snapshot is in warning."""
         if threshold_pct <= 0:
             return 0
         with self._lock:
             row = self._conn.execute(
                 """
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
                 """,
                 (threshold_pct,),
             ).fetchone()
         return row[0] if row else 0
     ```
  3. Add a small helper for the addon to look up `client_id` by `auth_hash` without re-implementing SQL each time:
     ```python
     def get_client_id_by_auth_hash(self, auth_hash: str) -> str | None:
         with self._lock:
             row = self._conn.execute(
                 "SELECT client_id FROM credentials WHERE auth_hash = ?",
                 (auth_hash,),
             ).fetchone()
         if row is None:
             return None
         return row[0]
     ```
  4. In `tests/test_db.py` add:
     - `test_is_in_warning_below_threshold`: insert a snapshot with `current_usage=1850, usage_limit=2000`; `is_in_warning("c1", 10.0)` is True.
     - `test_is_in_warning_above_threshold`: insert a snapshot with `current_usage=500, usage_limit=2000`; `is_in_warning("c1", 10.0)` is False.
     - `test_is_in_warning_empty_client_id`: `is_in_warning("", 10.0)` is False.
     - `test_is_in_warning_zero_threshold`: snapshot near depletion; `is_in_warning("c1", 0)` is False (feature off).
     - `test_is_in_warning_no_snapshot`: no rows for `c2`; `is_in_warning("c2", 10.0)` is False.
     - `test_is_in_warning_zero_limit`: snapshot with `usage_limit=0`; `is_in_warning("c1", 10.0)` is False.
     - `test_is_in_warning_uses_latest_snapshot`: insert two snapshots for same client (older near limit, newer not); confirms newest is used (False).
     - `test_get_warning_count_zero_threshold`: feature off → returns 0.
     - `test_get_warning_count_counts_distinct_clients`: insert snapshots for three clients, two in warning, one not; assert `get_warning_count(10.0) == 2`.
     - `test_get_client_id_by_auth_hash_found`: upsert a credential, look it up by hash, get the client_id.
     - `test_get_client_id_by_auth_hash_missing`: lookup returns None for unknown hash.
- **Constraints / conventions:**
  - All SQL access inside `with self._lock:` blocks.
  - Match docstring style of neighboring methods (one-line summary, optional details).
  - Don't add a column or table.
- **Done criteria:**
  - [ ] Three new methods exist on `CredentialDB`.
  - [ ] All eleven new tests pass.
  - [ ] No regressions in existing `test_db.py`.
- **How to verify:**
  ```bash
  uv run python -m pytest tests/test_db.py -x -q
  ```

---

## Task 3 — Addon: blending logic + in-memory counter

- **Type:** edit
- **Files involved:**
  - `src/credit_keeper/pool_addon.py`
  - `tests/test_pool_addon.py`
- **Depends on:** Task 1 (config fields), Task 2 (DB helpers)
- **What to do:**
  1. In `CredentialPoolAddon.__init__`, add an instance dict:
     ```python
     self._owner_counter: dict[str, int] = {}
     ```
  2. In `request()`, after the existing exhausted/rotation block (the `if self.config.auto_rotate:` block), add a new conditional. Keep the structure flat and readable:
     ```python
     # Warning-mode blending: only when feature is enabled, the credential
     # isn't already being rotated for exhaustion, and the owner is in
     # warning state. Identified by client_id (shared across all of an
     # owner's refreshed tokens).
     if (
         self.config.warning_blend_ratio > 0
         and not self.db.is_exhausted(auth_hash)
     ):
         client_id = self.db.get_client_id_by_auth_hash(auth_hash) or ""
         if client_id and self.db.is_in_warning(
             client_id, self.config.warning_threshold_pct
         ):
             count = self._owner_counter.get(client_id, 0) + 1
             self._owner_counter[client_id] = count
             every_nth = max(1, round(1 / self.config.warning_blend_ratio))
             if count % every_nth != 0:
                 # Pool turn: rotate to another credential.
                 available = self.db.get_best_available_credential(
                     exclude_auth_hash=auth_hash
                 )
                 if available:
                     new_header, new_hash, _ = available
                     flow.request.headers[header_name] = new_header
                     flow.metadata["ck_auth_hash"] = new_hash
                     flow.metadata["ck_auth_header"] = new_header
                     logger.info(
                         "credit-keeper: warning-blend client_id=%s "
                         "(counter=%d, every_nth=%d) rotated %s -> %s",
                         client_id,
                         count,
                         every_nth,
                         auth_hash[:8],
                         new_hash[:8],
                     )
                 else:
                     logger.warning(
                         "credit-keeper: warning-blend client_id=%s would "
                         "rotate but pool empty; falling through with owner",
                         client_id,
                     )
             else:
                 logger.debug(
                     "credit-keeper: warning-blend client_id=%s owner-turn "
                     "(counter=%d, every_nth=%d)",
                     client_id,
                     count,
                     every_nth,
                 )
     ```
     IMPORTANT: place this AFTER the exhausted check so exhausted always takes priority. The `not self.db.is_exhausted(auth_hash)` guard makes the precedence explicit even though the exhausted branch already rotated the header.
  3. In `tests/test_pool_addon.py` add:
     - `test_warning_blend_rotates_when_not_owner_turn`: configure blend_ratio=0.2 (every_nth=5), threshold=10; seed DB so client_id "c1" is in warning; create a fake flow; call `addon.request(flow)`; counter becomes 1; 1 % 5 != 0 → header rotated to another credential. Assert header value changed.
     - `test_warning_blend_skips_rotation_on_owner_turn`: same setup; call `request()` five times in a row; the 5th call should NOT rotate (counter 5, 5%5==0). Assert header unchanged on the 5th call.
     - `test_warning_blend_inactive_when_above_threshold`: snapshot at 500/2000 (above threshold); call `request()`; counter does NOT tick; no rotation log.
     - `test_warning_blend_inactive_when_feature_disabled`: `warning_blend_ratio=0.0`; even with a client in warning, no warning logic runs.
     - `test_warning_blend_unknown_client_id`: auth_hash that isn't in `credentials` table; `get_client_id_by_auth_hash` returns None; warning branch is skipped silently.
     - `test_warning_blend_exhausted_takes_priority`: client is BOTH exhausted and (theoretically) in warning; the exhausted rotation runs and the warning branch does NOT increment the counter.
     - `test_warning_blend_pool_empty_fallthrough`: client in warning, blend says "rotate", but `get_best_available_credential` returns None; assert header unchanged and a warning log was emitted.
     - `test_warning_blend_counter_persists_across_requests`: send 3 requests; counter for that client_id is exactly 3 at the end.
     - `test_warning_blend_counter_shared_across_refreshed_tokens`: two different auth_hashes mapped to same client_id; send 1 request with each; counter for client_id is 2.
  4. The existing tests in `test_pool_addon.py` MUST still pass without modification. If any fail due to the new branch, adjust ONLY the new code (do not weaken existing tests).
- **Constraints / conventions:**
  - Mark `request(self, flow)` keeps `# type: ignore[no-untyped-def]` as today.
  - Use `logger.info` for rotation lines, `logger.debug` for owner-turn lines, `logger.warning` for pool-empty fallback. Include hash slices (`[:8]`) and `client_id` for grep-ability.
  - Use `self._owner_counter.get(client_id, 0) + 1` instead of `defaultdict`; the dict stays a plain dict.
  - Do NOT add any DB column.
- **Done criteria:**
  - [ ] New warning branch lives in `request()` after exhausted logic.
  - [ ] All nine new tests pass.
  - [ ] All previously passing `test_pool_addon.py` tests still pass.
- **How to verify:**
  ```bash
  uv run python -m pytest tests/test_pool_addon.py -x -q
  ```

---

## Task 4 — Web UI: Dashboard "Warning" card

- **Type:** edit
- **Files involved:**
  - `src/credit_keeper/webui/app.py`
  - `src/credit_keeper/webui/templates/dashboard.html`
  - `tests/test_webui.py`
- **Depends on:** Task 2 (`get_warning_count`)
- **What to do:**
  1. `create_app` currently takes only `db_path`. To know the threshold without re-loading the YAML, change the signature to accept it. Open `src/credit_keeper/cli.py`, find where `create_app(db_path)` is invoked, and update the call to pass `config.credential_pool.warning_threshold_pct` (default 0.0 when no pool config). Update `create_app(db_path: str, warning_threshold_pct: float = 0.0)` accordingly.
  2. In the `dashboard` view function, compute:
     ```python
     warning = db.get_warning_count(warning_threshold_pct)
     active = total - exhausted - dead - warning
     ```
     Note: `active` may go negative if a credential's row is counted in two buckets (Warning is per-client, Exhausted/Dead are per-credential). For now, accept the discrepancy and just clamp to `max(0, active)`. Add a one-line code comment explaining the clamp.
  3. Pass `warning` into the template context.
  4. In `dashboard.html`:
     - Change grid `md:grid-cols-5` to `md:grid-cols-6`.
     - Insert a new card between Active and Exhausted:
       ```html
       <div class="bg-white rounded-lg shadow p-6">
           <div class="text-sm text-gray-500 uppercase">Warning</div>
           <div class="text-3xl font-bold text-yellow-600">{{ warning }}</div>
       </div>
       ```
  5. In `tests/test_webui.py`:
     - `test_dashboard_shows_warning_count`: configure `warning_threshold_pct=10`; seed DB with one client in warning (snapshot 1900/2000) and one not (snapshot 100/2000); request `/`; response body contains the substring `Warning` followed somewhere by `1`. (Use a simple text search on the rendered HTML; this is sufficient for our smoke test.)
     - `test_dashboard_warning_card_hidden_when_threshold_zero`: pass `warning_threshold_pct=0` (default), seed same DB; assert the rendered page shows `Warning` with value `0`. The card always renders but the number reflects threshold-off behavior.
- **Constraints / conventions:**
  - The Web UI is read-only and shouldn't mutate state.
  - Templates use Jinja2 + Tailwind via CDN; do not pull in new JS.
  - Keep `dashboard.html` formatting consistent with surrounding cards.
- **Done criteria:**
  - [ ] `dashboard.html` shows the Warning card in correct position.
  - [ ] `get_warning_count` value flows from DB → view → template.
  - [ ] Both new web tests pass.
- **How to verify:**
  ```bash
  uv run python -m pytest tests/test_webui.py -x -q
  ```

---

## Task 5 — Web UI: Credentials table "Warning" badge

- **Type:** edit
- **Files involved:**
  - `src/credit_keeper/webui/app.py`
  - `src/credit_keeper/webui/templates/credentials.html`
  - `tests/test_webui.py`
- **Depends on:** Task 2 (`is_in_warning`), Task 4 (signature change for `create_app`)
- **What to do:**
  1. In the `credentials` view, before passing context to template, build a set of warning client_ids:
     ```python
     warning_client_ids = {
         cid for cid in latest_usage.keys()
         if db.is_in_warning(cid, warning_threshold_pct)
     }
     ```
     Pass `warning_client_ids` into the template.
  2. In `credentials.html`, update the status cell to check warning AFTER dead/exhausted and BEFORE active:
     ```html
     <td class="px-4 py-2 text-sm">
         {% if cred["is_dead"] %}
             <span class="inline-block px-2 py-1 text-xs font-semibold text-gray-700 bg-gray-200 rounded">Dead</span>
         {% elif cred["is_exhausted"] %}
             <span class="inline-block px-2 py-1 text-xs font-semibold text-red-700 bg-red-100 rounded">Exhausted</span>
         {% elif cred["client_id"] in warning_client_ids %}
             <span class="inline-block px-2 py-1 text-xs font-semibold text-yellow-700 bg-yellow-100 rounded">Warning</span>
         {% else %}
             <span class="inline-block px-2 py-1 text-xs font-semibold text-green-700 bg-green-100 rounded">Active</span>
         {% endif %}
     </td>
     ```
  3. In `tests/test_webui.py`:
     - `test_credentials_table_shows_warning_badge`: seed a credential with `is_exhausted=0, is_dead=0` whose client_id is in warning; GET `/credentials`; assert HTML contains `Warning` near the row.
- **Constraints / conventions:**
  - Reuse the existing status badge styling (only color differs).
  - Don't query DB inside the template loop; precompute the set in the view.
- **Done criteria:**
  - [ ] Warning badge appears for the correct rows.
  - [ ] New web test passes.
- **How to verify:**
  ```bash
  uv run python -m pytest tests/test_webui.py -x -q
  ```

---

## Task 6 — Example `config.yaml` update

- **Type:** edit
- **Files involved:**
  - `config.yaml`
- **Depends on:** none (can run after Task 1 has defined the schema)
- **What to do:**
  Append (or update) the `credential_pool` section to include the two new fields with brief Vietnamese comments explaining behavior:
  ```yaml
  credential_pool:
    enabled: true
    intercept_hosts:
      - "q.us-east-1.amazonaws.com"
    usage_path: "/getUsageLimits"
    usage_amz_target: "AmazonCodeWhispererService.GetUsageLimits"
    extract_headers: ["Authorization"]
    auto_rotate: true
    # Warning-mode blending: khi remaining < threshold_pct, trộn owner + pool
    # theo blend_ratio để kéo dài tuổi thọ credit. Đặt cả 2 về 0 để tắt.
    warning_threshold_pct: 10.0    # vào warning khi remaining < 10% usage_limit
    warning_blend_ratio: 0.2       # 20% request dùng owner, 80% dùng pool
  ```
- **Constraints / conventions:**
  - Don't remove or reorder existing fields.
  - Keep comments Vietnamese (consistent with the rest of `config.yaml`).
- **Done criteria:**
  - [ ] `config.yaml` is loadable by `load_config` without errors.
- **How to verify:**
  ```bash
  uv run python -c "from credit_keeper.config import load_config; print(load_config('config.yaml'))"
  ```

---

## Task 7 — CHANGELOG entry under `[Unreleased]`

- **Type:** edit
- **Files involved:**
  - `CHANGELOG.md`
- **Depends on:** Tasks 1–5 (so the entry accurately reflects what landed)
- **What to do:**
  Under the existing `## [Unreleased]` section, add a new sub-section near the top (above the previous Added/Changed/Fixed lists from the prior batch). If the previous batch is already finalized, create a fresh `## [Unreleased] — <today>` block above it. Suggested entry text (Vietnamese, matching existing style):

  ```markdown
  ### Added — Warning-mode credit blending

  - **Config**: `warning_threshold_pct` và `warning_blend_ratio` trong `credential_pool`.
    Khi remaining của user < threshold_pct, addon tự động trộn owner + pool theo
    tỷ lệ blend_ratio (vd `0.2` = 1/5 request dùng owner, 4/5 dùng pool).
    Mục đích: kéo dài tuổi thọ credit của user gần cạn.
    Backward-compat: cả 2 default `0` → feature tắt.
  - **DB**: `is_in_warning(client_id, threshold_pct)`, `get_warning_count(threshold_pct)`,
    `get_client_id_by_auth_hash(auth_hash)`. Không thêm column/table — warning state
    compute on-the-fly từ `usage_snapshots`.
  - **Addon**: branch mới trong `request()` đặt sau exhausted check. Counter in-memory
    `dict[client_id, int]` (không persist). Modulo arithmetic đảm bảo tỷ lệ chính xác
    dài hạn.
  - **Web UI**: Dashboard có thêm card "Warning" (màu vàng). Credentials table có badge
    "Warning" với độ ưu tiên Dead > Exhausted > Warning > Active.
  ```
- **Constraints / conventions:**
  - Match the existing CHANGELOG voice and formatting.
- **Done criteria:**
  - [ ] CHANGELOG reflects all the new surface area.
- **How to verify:** Read the file; ensure the entry is present and renders as Markdown.

---

## Task 8 — Smoke verification

- **Type:** test
- **Files involved:** —
- **Depends on:** Tasks 1–7
- **What to do:**
  1. Run the full test suite:
     ```bash
     uv run python -m pytest -x -q
     ```
     All tests must pass.
  2. Run the proxy with the updated config to ensure no runtime errors at startup:
     ```bash
     uv run credit-keeper -c config.yaml -p 8080 --webui-port 9090
     ```
     Expect normal startup logs; Ctrl+C to stop.
  3. Quick manual check of the Web UI:
     - Open `http://127.0.0.1:9090/` → see 6-card grid with "Warning" card present (likely `0`).
     - Open `http://127.0.0.1:9090/credentials` → table loads without error.
- **Constraints / conventions:** —
- **Done criteria:**
  - [ ] All tests pass.
  - [ ] Proxy starts and Web UI renders without errors.
- **How to verify:** See commands above.

---

## Overall acceptance checklist

- [ ] All eight tasks done.
- [ ] `uv run python -m pytest -x -q` reports 0 failures (existing 83 + new ones).
- [ ] Manual Web UI sanity check passes.
- [ ] Matches every Done criterion in `spec.md` §8.
