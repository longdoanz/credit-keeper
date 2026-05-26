# Giai đoạn 1: Discovery cho Kiro IDE

Hướng dẫn sniff traffic của Kiro IDE để xác định các thông số cần thiết cho credit-keeper trước khi đưa vào sử dụng thật.

## Mục đích

Trước khi cấu hình credit-keeper, cần biết:

1. **`intercept_hosts`** — Kiro gọi đến những host nào?
2. **`extract_headers`** — Header nào chứa credential? (thường là `Authorization`)
3. **`usage_path`** — Endpoint nào trả về quota/usage?

Cách duy nhất để biết chính xác là chạy proxy ở chế độ "spy mode" và quan sát traffic thật của Kiro.

## Yêu cầu

- Đã cài [credit-keeper](../README.md) (`uv sync --python 3.12`)
- Đã cài Kiro IDE (verify bằng cách double-click thử)
- Có AWS Builder ID để login Kiro
- Windows PowerShell (lệnh trong tài liệu này dùng cú pháp PowerShell)

## Quy trình

### Bước 1: Dừng credit-keeper nếu đang chạy

Tại terminal đang chạy `credit-keeper` (nếu có), bấm `Ctrl+C` để dừng.

Lý do: discovery dùng **`mitmweb`** (đi kèm credit-keeper) thay vì credit-keeper. `mitmweb` có UI inspect được full request/response body — credit-keeper UI chỉ hiện những gì đã được logging.

### Bước 2: Chạy mitmweb

Tại thư mục `credit-keeper`:

```powershell
uv run mitmweb --listen-port 8080 --set web_port=8081
```

Sẽ thấy log đại loại:

```
Web server listening at http://127.0.0.1:8081/
Proxy server listening at http://*:8080
```

Lần đầu chạy, mitmproxy tự tạo CA cert ở `$env:USERPROFILE\.mitmproxy\`.

Mở browser truy cập **http://127.0.0.1:8081** — đây là nơi xem traffic realtime.

### Bước 3: Tìm đường dẫn tới Kiro.exe

Mở **terminal PowerShell mới** (giữ nguyên terminal mitmweb đang chạy):

```powershell
Get-ChildItem -Path "$env:LOCALAPPDATA\Programs", "$env:ProgramFiles", "${env:ProgramFiles(x86)}", "D:\Software" `
              -Filter "Kiro.exe" -Recurse -ErrorAction SilentlyContinue `
            | Select-Object -ExpandProperty FullName
```

Copy lại đường dẫn xuất hiện. Nếu không tìm thấy, kiểm tra shortcut Kiro trên Desktop hoặc Start Menu — chuột phải → Properties → ô "Target".

### Bước 4: Set env và launch Kiro qua proxy

Trong terminal mới (terminal đã mở ở Bước 3):

```powershell
# 1. Trỏ HTTP/HTTPS qua proxy
$env:HTTP_PROXY  = "http://127.0.0.1:8080"
$env:HTTPS_PROXY = "http://127.0.0.1:8080"

# 2. Trust CA của mitmproxy (Kiro là Electron/Node)
$env:NODE_EXTRA_CA_CERTS = "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.pem"

# 3. Kiểm tra file CA đã tồn tại
Test-Path $env:NODE_EXTRA_CA_CERTS
# Phải in "True". Nếu False → quay lại Bước 2 chạy mitmweb trước.

# 4. Launch Kiro (THAY đường dẫn bằng kết quả Bước 3)
& "D:\Software\Kiro\Kiro.exe"
```

> **Quan trọng**: phải launch Kiro **từ terminal đã set env**. Mở Kiro bằng cách double-click icon sẽ không nhận env, traffic không đi qua proxy.

### Bước 5: Tạo traffic mẫu trong Kiro

Trong Kiro IDE, làm tuần tự:

1. **Login** (nếu chưa) — dùng AWS Builder ID
2. Mở 1 file code bất kỳ
3. **Chat với AI**: gõ câu hỏi như "explain this code"
4. **Yêu cầu generate code**: "write a hello world function"
5. **Để Kiro autocomplete** vài dòng

Mỗi action tạo HTTP request → xuất hiện trên http://127.0.0.1:8081

## Quan sát và ghi lại

Trên mitmweb UI (http://127.0.0.1:8081), click từng request để xem chi tiết. Cần thu thập 3 nhóm thông tin:

### A. Host xuất hiện

Cột "Host" — ghi lại các host xuất hiện nhiều. Loại trừ: telemetry, analytics, github, marketplace, update.

Với Kiro IDE, các host thường gặp:

| Host | Mục đích | Cần intercept? |
|------|----------|----------------|
| `q.us-east-1.amazonaws.com` | AI API (chat, generate, usage) | ✅ Bắt buộc |
| `prod.us-east-1.auth.desktop.kiro.dev` | Auth / refresh token | ⚠️ Optional |
| `prod.us-east-1.telemetry.desktop.kiro.dev` | Telemetry | ❌ Bỏ qua |
| `prod.download.desktop.kiro.dev` | Auto-update | ❌ Bỏ qua |

### B. Authorization header format

1. Click 1 request đến host AI (ví dụ `/generateAssistantResponse`)
2. Tab **Request** → mục **Headers**
3. Tìm header `Authorization`
4. Ghi lại **chỉ phần đầu** (không cần copy full token)

| Format gặp được | Có rotate được không? |
|-----------------|----------------------|
| `Bearer eyJraWQiOi...` (JWT) | ✅ Có |
| `Bearer aoaAAAAA...` (opaque token) | ✅ Có |
| `AWS4-HMAC-SHA256 Credential=AKIA.../...` | ❌ Không (Sigv4 ký theo nội dung request) |
| `Basic ...` | ✅ Có nhưng hiếm gặp |

### C. Endpoint quota/usage

Tìm request có URL chứa từ khóa: `usage`, `quota`, `limit`, `subscription`, `entitlement`.

1. Click vào request đó
2. Tab **Response** → **Body**
3. Copy JSON body

Cấu trúc credit-keeper parser mong đợi:

```json
{
  "usageBreakdownList": [
    {
      "currentUsage": 0,
      "usageLimit": 50,
      "displayName": "...",
      "resourceType": "...",
      "unit": "..."
    }
  ],
  "userInfo": {
    "userId": "..."
  },
  "subscriptionInfo": {
    "subscriptionTitle": "..."
  },
  "daysUntilReset": null,
  "nextDateReset": 1780272000.0
}
```

Nếu response thực tế khác cấu trúc trên, cần modify [src/credit_keeper/pool_addon.py](../src/credit_keeper/pool_addon.py) tại function `response()` cho phù hợp.

## Template báo cáo

Sau khi dùng Kiro 5-10 phút, tổng hợp theo template:

```
[A] Hosts thấy nhiều:
  - host1
  - host2

[B] Format Authorization header:
  Authorization: <Bearer/AWS4-HMAC.../...>

[C] Endpoint quota:
  URL path: /...
  Response body: { ... }

[D] Endpoint AI chính (request tốn credit):
  - vd: POST /generateAssistantResponse
```

## Kết quả mẫu (Kiro IDE, 2026-05-26)

Tham khảo — đây là kết quả thực tế khi sniff Kiro IDE bản KIRO FREE:

**[A] Hosts:**
- `q.us-east-1.amazonaws.com` — AI API
- `prod.us-east-1.auth.desktop.kiro.dev` — refresh token
- `prod.us-east-1.telemetry.desktop.kiro.dev` — telemetry (bỏ qua)

**[B] Authorization:**
```
Authorization: Bearer aoaAAAAAGoVYNsKbQgg6...
```
Opaque token → rotate được ✅

**[C] Endpoint quota:**
```
GET https://q.us-east-1.amazonaws.com/getUsageLimits
    ?origin=AI_EDITOR
    &profileArn=arn:aws:codewhisperer:us-east-1:...
    &resourceType=AGENTIC_REQUEST

Response (KIRO FREE):
{
  "daysUntilReset": null,
  "nextDateReset": 1780272000.0,
  "subscriptionInfo": {
    "subscriptionTitle": "KIRO FREE",
    "type": "Q_DEVELOPER_STANDALONE_FREE",
    "overageCapability": "OVERAGE_INCAPABLE"
  },
  "usageBreakdownList": [
    {
      "currentUsage": 0,
      "usageLimit": 50,
      "displayName": "Credit",
      "resourceType": "CREDIT",
      "unit": "INVOCATIONS"
    }
  ],
  "userInfo": {
    "userId": "d-9067c98495.145874e8-a051-70f1-312d-4d5ffd65c74d"
  }
}
```

→ Trùng khít cấu trúc parser mặc định, không cần sửa code.

**[D] Endpoint AI:**
- `POST /generateAssistantResponse` — chat/generate, tốn credit chính
- `POST /mcp` — MCP tools
- `GET /ListAvailableModels` — list models (không tốn credit)

## Troubleshoot

| Triệu chứng | Nguyên nhân & Fix |
|-------------|-------------------|
| mitmweb không thấy request nào từ Kiro | Kiro không nhận env proxy. Verify trong terminal đã launch Kiro: `[System.Environment]::GetEnvironmentVariable('HTTP_PROXY','Process')` |
| Kiro báo `ERR_CERT_AUTHORITY_INVALID` cho TẤT CẢ request | `NODE_EXTRA_CA_CERTS` không trỏ đúng file CA. Verify `Test-Path $env:NODE_EXTRA_CA_CERTS` phải True |
| Kiro báo `ERR_CERT_AUTHORITY_INVALID` chỉ cho `prod.download.desktop.kiro.dev` | Bỏ qua được — đây là auto-update check, dùng network stack riêng không respect `NODE_EXTRA_CA_CERTS`. Không ảnh hưởng chức năng AI |
| Kiro không login được | Auth flow mở external browser → tạm thời unset proxy, login xong rồi set lại proxy và restart Kiro |
| Quá nhiều request nhiễu trên mitmweb | Gõ filter ở thanh tìm kiếm: `~d q\.|kiro\.` để chỉ hiện AWS Q và Kiro hosts |
| `Test-Path` trả về `False` | Chưa chạy mitmweb lần nào → quay lại Bước 2 |

## Bước tiếp theo

Sau khi có đầy đủ thông tin (A, B, C), chuyển sang **Giai đoạn 2: Setup production** — viết file `config.yaml` thật và triển khai cho nhiều user.
