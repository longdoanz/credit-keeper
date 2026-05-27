# Changelog

Mọi thay đổi đáng chú ý của project được ghi lại trong file này.

Format dựa trên [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — 2026-05-27

Đợt cập nhật chính: bổ sung hỗ trợ **kiro-cli** (Amazon Q CLI fork dùng AWS SDK protocol) và cải thiện độ tin cậy của rotation.

### Added

- **Match request theo `x-amz-target` header** cho client dùng AWS SDK protocol (kiro-cli).
  - Thêm field `usage_amz_target` vào `CredentialPoolConfig`.
  - `pool_addon.response()` giờ match HOẶC URL path (REST-style, Kiro IDE) HOẶC `x-amz-target` header (AWS SDK-style, kiro-cli).
  - Backward-compatible: config cũ không khai báo `usage_amz_target` vẫn hoạt động như cũ.

- **Phát hiện credential chết** (dead detection) qua HTTP 401/403.
  - Khi backend reject token (401/403), proxy tự động mark credential là `is_dead=1` để loại khỏi rotation pool.
  - Mark theo `auth_hash` cụ thể (không phải toàn bộ user) để token mới refresh vẫn được pool.
  - Logic recovery: khi token được refresh, credential mới (hash mới) tự động được insert với `is_dead=0`.

- **Database schema**:
  - Thêm column `is_dead INTEGER DEFAULT 0` trong table `credentials`.
  - Migration tự động cho DB cũ qua `ALTER TABLE`.

- **DB methods** mới trong `CredentialDB`:
  - `mark_dead(auth_hash)` — đánh dấu token bị backend từ chối.
  - `mark_alive(auth_hash)` — clear cờ dead (dùng cho recovery thủ công).
  - `is_dead(auth_hash)` — query trạng thái dead.
  - `get_dead_count()` — đếm số credential dead cho Dashboard.

- **Web UI**:
  - Card "Dead" mới trên Dashboard (grid từ 4 sang 5 cột).
  - Badge "Dead" (màu xám) trong table Credentials, ưu tiên hiển thị cao hơn "Exhausted".

- **Script `scripts/exhaust_user.py`**:
  - Lệnh `revive` — clear cờ `is_dead` cho tất cả credential.
  - Lệnh `reset-all` — clear cả `is_exhausted` lẫn `is_dead`.
  - Output `show` thêm cột `dead`.

- **Documentation**:
  - `docs/kiro-discovery.md` — hướng dẫn discovery phase cho Kiro IDE (sniff traffic để xác định `intercept_hosts`, `extract_headers`, `usage_path`).

- **Config example** (`config.yaml`):
  - Cấu hình mẫu cho Kiro IDE và kiro-cli, gồm cả `usage_amz_target: "AmazonCodeWhispererService.GetUsageLimits"`.

### Changed

- **Thuật toán chọn credential** (`get_best_available_credential`):
  - Thêm tiebreaker `c.last_seen_at DESC` sau primary sort `(usage_limit - current_usage) DESC`.
  - Lý do: khi nhiều credential cùng user có cùng remaining credits, SQLite trước đây chọn không xác định, thường lấy token cũ (đã expire). Giờ luôn ưu tiên token mới nhất → giảm tỷ lệ rotation vào token đã chết.

- **Query rotation** (`get_available_credential`, `get_best_available_credential`):
  - Thêm điều kiện `AND is_dead = 0` để loại trừ credential đã được mark dead.

### Fixed

- **kiro-cli request không được track usage**: trước đây, do kiro-cli dùng AWS JSON-RPC (POST `/?profileArn=...` + `x-amz-target: AmazonCodeWhispererService.GetUsageLimits`) thay vì REST (`GET /getUsageLimits`), code cũ chỉ match URL path nên bỏ qua → Dashboard không cập nhật usage cho user CLI.

- **Rotation chọn token đã expire**: do thiếu tiebreaker `last_seen_at`, SQL `ORDER BY (limit - usage) DESC LIMIT 1` trả về token không xác định khi nhiều token cùng user có cùng remaining. Hệ quả: thường lấy token cũ (insert đầu tiên) đã expire → AWS 403.

- **Token chết không bị loại khỏi pool**: trước đây không có cơ chế detect token bị backend reject vĩnh viễn (expired/revoked). Rotation vẫn chọn lại token chết nhiều lần liên tiếp. Giờ tự động mark dead ngay lần đầu nhận 401/403.

### Technical Notes

- Khi pool cạn (tất cả credentials exhausted hoặc dead), proxy **không chặn request** — fallback forward với token gốc, để client tự nhận response từ backend (có thể 403 nếu token gốc thật sự hết quota). Behavior này có chủ ý để tránh false-negative khi DB flag sai.

- Caveat đã biết: "phantom credentials" tích lũy theo thời gian do mỗi lần refresh token tạo `auth_hash` mới. Cần cron job dọn dẹp `is_dead=1 AND last_seen_at < N days` trong tương lai.

### Tests

- 83 unit tests pass, gồm:
  - `test_db.py` — DB CRUD + queries mới
  - `test_pool_addon.py` — capture, tracking, rotation, mark_dead
  - `test_config.py` — parse `usage_amz_target`
  - `test_webui.py` — dashboard hiển thị dead count

### Files Changed

```
src/credit_keeper/config.py                              # +usage_amz_target field, YAML parsing
src/credit_keeper/pool_addon.py                          # x-amz-target matching, 401/403 detection
src/credit_keeper/db.py                                  # is_dead column, mark_dead/alive/is_dead/get_dead_count
src/credit_keeper/webui/app.py                           # dashboard dead count
src/credit_keeper/webui/templates/dashboard.html         # 5-card grid with Dead
src/credit_keeper/webui/templates/credentials.html       # Dead badge
scripts/exhaust_user.py                                  # revive, reset-all commands
config.yaml                                              # usage_amz_target example
docs/kiro-discovery.md                                   # new — Phase 1 discovery guide
```

## [0.1.0] — Initial release

- Config-driven HTTP/HTTPS proxy built on mitmproxy 12.
- Header mutation rules: `set`, `add`, `remove`, `replace`.
- Credential pool with auto-rotation based on usage tracking.
- SQLite storage with WAL mode.
- FastAPI web UI: Dashboard, Credentials, Requests, Usage history.
- CLI flags: `--listen-host`, `-p`, `--mode`, `--ssl-insecure`, `--db`, `--webui-port`.
