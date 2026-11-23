# Triển khai production

## Kiến trúc

Production tách thành bốn tiến trình độc lập:

- `api`: phục vụ HTTP, không gọi Judge0/LLM đồng bộ;
- `grading-worker`: nhận lượt chấm từ hàng đợi PostgreSQL;
- `hint-worker`: claim yêu cầu gợi ý bằng `FOR UPDATE SKIP LOCKED` và lease;
- `migrate`: chạy Alembic một lần trước khi các dịch vụ khởi động.

PostgreSQL không mở cổng ra host. API chỉ bind vào `127.0.0.1`; cần đặt Nginx,
Caddy hoặc load balancer có TLS phía trước. Judge0 phải là instance riêng nằm trong
private network/VPN và có token; không dùng `ce.judge0.com` cho dữ liệu sinh viên.

## Khởi tạo

```bash
cp .env.production.example .env.production
chmod 600 .env.production
# Điền toàn bộ giá trị REPLACE_WITH_... và domain thật.

docker compose --env-file .env.production \
  -f compose.production.yaml up -d --build

# Nạp 20 bài tập và test case.
docker compose --env-file .env.production \
  -f compose.production.yaml run --rm migrate python -m app.seed

# Tạo tài khoản; mật khẩu được nhập ẩn, không đi qua argv/shell history.
docker compose --env-file .env.production \
  -f compose.production.yaml run --rm migrate \
  python -m scripts.create_student <ten_dang_nhap>
```

`AUTH_SESSION_SECRET` phải là chuỗi ngẫu nhiên tối thiểu 32 ký tự. Có thể tạo bằng:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Mật khẩu trong `DATABASE_URL` phải được URL-encode. Không commit `.env.production`,
API key, database dump hay Judge0 token vào Git.

## Kiểm tra sau triển khai

```bash
docker compose --env-file .env.production -f compose.production.yaml ps
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/health/ready
```

Readiness trả `503` nếu PostgreSQL lỗi hoặc heartbeat của `grading-worker` /
`hint-worker` quá hạn. Log không chứa source code, mật khẩu hoặc secret. Cấu hình
production sẽ từ chối khởi động nếu dùng demo auth, cookie không Secure, fake LLM,
hint đồng bộ, secret ngắn hoặc lease ngắn hơn thời gian retry LLM.

## Reverse proxy bắt buộc

Reverse proxy phải:

- redirect HTTP sang HTTPS và bật HSTS;
- chỉ chuyển `X-Forwarded-*` từ IP proxy đã khai báo trong `TRUSTED_PROXY_IPS` (nếu reverse proxy chạy trên host gọi qua cổng publish `127.0.0.1:8000` vào Docker bridge, cần đặt đúng IP gateway cầu nối Docker hoặc chạy proxy chung mạng Docker `backend` với IP tĩnh để tránh Uvicorn gom toàn bộ người dùng về một IP gateway);
- giới hạn request body và rate-limit `/api/auth/login`, `/api/submissions`;
- đặt timeout lớn hơn polling HTTP nhưng không giữ kết nối LLM;
- không cache response API chứa dữ liệu sinh viên.

Ứng dụng tự đặt CSP, `X-Frame-Options`, `nosniff`, Referrer Policy và Permissions
Policy. Phiên dùng cookie `HttpOnly + Secure + SameSite=Lax`; thao tác ghi bắt buộc
CSRF token. Đổi mật khẩu hoặc đăng xuất tăng `session_version`, vô hiệu hóa các
phiên cũ của tài khoản.

## Backup và khôi phục

Tạo backup định kỳ sang storage mã hóa nằm ngoài host Docker:

```bash
docker compose --env-file .env.production -f compose.production.yaml \
  exec -T db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > "backup-$(date +%Y%m%d-%H%M%S).dump"
```

Phải kiểm thử restore trên database tách biệt trước khi coi backup là hợp lệ. Không
restore đè production khi chưa dừng ghi, xác nhận đúng file và có rollback plan.

## Cập nhật và rollback

1. Backup database và lưu image tag đang chạy.
2. Build image mới trong CI; migration phải qua `upgrade → downgrade → upgrade`.
3. Chạy `docker compose ... up -d --build`; service `migrate` hoàn tất trước API.
4. Kiểm tra `/health/ready`, đăng nhập, nộp một bài và tạo một gợi ý.
5. Rollback image nếu ứng dụng lỗi. Chỉ downgrade database khi migration xác nhận
   an toàn và đã có backup.

Scale worker bằng `--scale grading-worker=N --scale hint-worker=M`. PostgreSQL
locking ngăn hai worker hoàn tất cùng một job; `claim_token` ngăn worker lease cũ
ghi đè kết quả của worker mới.
