# credit-keeper

> Proxy HTTP/HTTPS viết lại header theo file YAML.
> A header-rewriting HTTP/HTTPS proxy driven by a YAML config.

`credit-keeper` là một CLI Python nhỏ, dùng [mitmproxy](https://mitmproxy.org/)
như một thư viện. Bạn khai báo các quy tắc (rule) trong file YAML; công cụ sẽ
chạy một proxy HTTP/HTTPS và sửa header trên request hoặc response khi traffic
đi qua. Mỗi rule có thể lọc theo host (glob hoặc regex), URL regex, HTTP method,
sau đó `set`, `add`, `remove`, hoặc `replace` (regex) header.

## Cài đặt / Installation

Yêu cầu: Python 3.12 trở lên và [`uv`](https://docs.astral.sh/uv/).

```bash
# Cài uv nếu chưa có:
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# Đồng bộ môi trường (tạo .venv và cài deps):
uv sync --python 3.12

# Có dev deps để chạy test:
uv sync --python 3.12 --extra dev
```

Sau khi `uv sync`, lệnh `credit-keeper` sẵn sàng dưới `uv run`:

```bash
uv run --python 3.12 credit-keeper --help
```

## Quick start

1. Tạo file config, ví dụ `headers.yaml`:

   ```yaml
   rules:
     - name: stamp-ua
       host: "*.example.com"
       request:
         - {action: set, name: User-Agent, value: "credit-keeper/0.1"}
         - {action: add, name: X-Forwarded-For, value: "10.0.0.1"}

     - name: drop-server
       response:
         - {action: remove, name: Server}
   ```

2. Chạy proxy:

   ```bash
   uv run --python 3.12 credit-keeper -c headers.yaml
   # credit-keeper listening on 127.0.0.1:8080, loaded 2 rules from headers.yaml
   ```

3. Trỏ trình duyệt hoặc hệ thống sang proxy `127.0.0.1:8080` (hoặc dùng
   `curl -x http://127.0.0.1:8080 ...`).

4. Đối với HTTPS, cài CA cert của mitmproxy: trong khi đã trỏ qua proxy, mở
   trình duyệt vào [http://mitm.it](http://mitm.it) và làm theo hướng dẫn cho
   hệ điều hành của bạn. Nếu chỉ test bằng `curl`, có thể bỏ qua bằng
   `--ssl-insecure` ở phía proxy và `-k` ở `curl`.

CLI options:

| Flag                        | Default       | Mô tả |
|-----------------------------|---------------|-------|
| `-c`, `--config PATH`       | (bắt buộc)    | File YAML chứa các rule. |
| `--listen-host HOST`        | `127.0.0.1`   | Địa chỉ bind proxy. |
| `-p`, `--listen-port PORT`  | `8080`        | Cổng nghe. |
| `--mode MODE`               | `regular`     | mitmproxy proxy mode (regular, transparent, socks5, reverse:..., upstream:...). |
| `--ssl-insecure`            | tắt           | Không verify TLS upstream. |
| `--version`                 |               | In phiên bản và thoát. |

Bạn cũng có thể chạy bằng `python -m credit_keeper -c headers.yaml`.

## Config reference

File config là một YAML mapping với một key duy nhất ở cấp cao nhất, `rules`,
chứa danh sách các rule. Các filter trong cùng một rule được kết hợp bằng AND.

### Rule fields

| Field        | Type                                        | Default     | Mô tả |
|--------------|---------------------------------------------|-------------|-------|
| `name`       | string (required)                           | -           | Tên gợi nhớ; xuất hiện trong thông báo lỗi. |
| `host`       | string                                      | none        | `fnmatch` glob, không phân biệt hoa thường. Ví dụ: `*.api.example.com`. |
| `host_regex` | string                                      | none        | Python regex áp lên host bằng `re.search`. |
| `url_regex`  | string                                      | none        | Python regex áp lên full URL bằng `re.search`. |
| `methods`    | list of strings                             | none        | Danh sách HTTP method (không phân biệt hoa thường). |
| `apply_to`   | `"request"` \| `"response"` \| `"both"`     | xem dưới    | Hướng nào rule sẽ chạy. |
| `request`    | list of header ops                          | `[]`        | Op áp dụng trên request đi ra. |
| `response`   | list of header ops                          | `[]`        | Op áp dụng trên response đi vào. |

`apply_to` mặc định là `"request"`. Nếu bạn bỏ trống và rule chỉ có `response`
ops, nó tự chuyển sang `"response"`. Nếu cả `request` lẫn `response` đều có
ops, nó tự chuyển sang `"both"`.

### HeaderOp actions

| Action     | Required fields           | Ví dụ |
|------------|---------------------------|-------|
| `set`      | `name`, `value`           | `{action: set, name: User-Agent, value: "credit-keeper/0.1"}` |
| `add`      | `name`, `value`           | `{action: add, name: X-Forwarded-For, value: "10.0.0.1"}` |
| `remove`   | `name`                    | `{action: remove, name: Server}` |
| `replace`  | `name`, `pattern`, `value`| `{action: replace, name: Authorization, pattern: "Bearer (.*)", value: "Bearer redacted-\\1"}` |

Ngữ nghĩa các action:

- `set` ghi đè toàn bộ giá trị hiện có của header bằng một giá trị mới.
- `add` thêm một giá trị nữa, giữ nguyên các giá trị hiện có.
- `remove` xoá header nếu có; không lỗi khi header chưa tồn tại.
- `replace` chạy `re.sub(pattern, value, current_value)` cho từng giá trị hiện
  có của header. Backref kiểu `\1` trong `value` hoạt động bình thường.

Xem `examples/headers.example.yaml` để có một config mẫu đầy đủ chú thích.

## Security

`credit-keeper` chạy đè lên mitmproxy nên có khả năng MITM toàn bộ HTTPS đi qua
nó. Vì vậy hãy đọc kỹ phần này trước khi dùng.

- **CA cert lifecycle.** Để intercept HTTPS, bạn phải cài CA cert của mitmproxy
  vào trust store. Cert này nằm ở `~/.mitmproxy/mitmproxy-ca-cert.pem`. Khi
  cert đã được tin cậy, **bất kỳ tiến trình nào** chạy trên máy bạn và bind
  vào `127.0.0.1:8080` (hoặc cổng khác đang chạy proxy) đều có thể giả mạo
  bất kỳ HTTPS site nào. Khi xong việc, gỡ cert khỏi trust store và xoá thư
  mục `~/.mitmproxy` nếu không cần lưu lại.
- **`--ssl-insecure` semantics.** Cờ này chỉ tắt verify chứng chỉ phía
  *upstream* (tức là proxy → server thật). Nó **không** giống `curl -k`.
  Khi bật, proxy vẫn xuất CA của mitmproxy cho client, nhưng tự nó chấp
  nhận mọi chứng chỉ từ server, kể cả giả mạo. Trên mạng không tin cậy,
  bật cờ này có nghĩa là `Authorization` header bạn đang viết lại có thể
  rơi vào tay kẻ tấn công đứng giữa.
- **Listen-host risk.** Mặc định `--listen-host 127.0.0.1` chỉ cho phép truy
  cập từ chính máy bạn. Đổi sang `0.0.0.0` (hoặc một interface LAN) sẽ biến
  máy bạn thành một authless TLS-intercepting gateway cho cả mạng nội bộ:
  bất kỳ ai chiếm được kết nối tới cổng đó đều có thể đọc và sửa traffic
  HTTPS của họ qua CA của bạn. `credit-keeper` sẽ in một cảnh báo
  (`logger.warning`) khi `--listen-host` không nằm trong dải loopback,
  nhưng nó **không** chặn việc bind. Hãy chỉ làm điều này trong môi trường
  đã có firewall/cách ly mạng phù hợp.
- **Config có giá trị nhạy cảm.** File config có thể chứa bearer token mẫu
  hoặc giá trị inject. `credit-keeper` không expand biến môi trường, nên
  giá trị bạn viết được dùng nguyên văn. Đừng commit config thật vào git
  công khai.

## Why mitmproxy

`credit-keeper` được xây dựng trên mitmproxy vì đây là thư viện proxy Python
hỗ trợ HTTPS phổ biến và ổn định nhất hiện nay. mitmproxy cung cấp một addon
API rất đơn giản (mỗi addon là một class Python với các hook như `request`,
`response`), can thiệp được cả request lẫn response trong cùng một nơi, và
xử lý sẵn các phần khó như TLS interception, cấp CA cert, parse HTTP/2.
Nhờ vậy `credit-keeper` chỉ tập trung vào việc khớp rule và sửa header,
không phải dựng lại tầng mạng.

## Development

```bash
export PATH="$HOME/.local/bin:$PATH"
uv sync --python 3.12 --extra dev
uv run --python 3.12 pytest -q
```

Smoke-test CLI:

```bash
uv run --python 3.12 credit-keeper --help
uv run --python 3.12 credit-keeper -c examples/headers.example.yaml -p 18080
# trong shell khác:
curl -x http://127.0.0.1:18080 -s -o /dev/null -w "%{http_code}\n" http://example.com
```

Test addon dùng `mitmproxy.test.tflow` để dựng `HTTPFlow` giả nên không cần
khởi động proxy thật.

## Project layout

```
credit-keeper/
├── pyproject.toml              # build / deps / console script
├── README.md                   # tài liệu này
├── examples/
│   └── headers.example.yaml    # config mẫu có chú thích
├── src/credit_keeper/
│   ├── __init__.py             # public API: Config, Rule, HeaderOp, ...
│   ├── __main__.py             # `python -m credit_keeper`
│   ├── config.py               # dataclass + YAML loader + ConfigError
│   ├── rules.py                # rule_matches, apply_ops, CaseInsensitiveHeaders
│   ├── addon.py                # mitmproxy addon HeaderCustomizer
│   └── cli.py                  # entry point `credit-keeper`
└── tests/
    ├── test_config.py
    ├── test_rules.py
    └── test_addon.py           # dùng mitmproxy.test.tflow
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for the
full text.
