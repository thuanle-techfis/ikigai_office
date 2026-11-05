# ikigai office, bản chạy trên máy tính

Bản này giữ nguyên mọi thứ đã làm trên claude.ai: mặt bằng, đồ đạc, tường, nhân vật, trang phục, trò chuyện, chiếu lên TV, họp với Chủ tịch và tiếp khách. Mặt bằng hiện tại đã được nạp sẵn.

## Cần gì

## Tài khoản và chơi online

- Mở cùng một địa chỉ server trên các máy trong mạng LAN. Mỗi người chọn **Tạo tài khoản** hoặc **Đăng nhập** trước khi vào văn phòng.
- Tài khoản được lưu ở server; mật khẩu được băm bằng PBKDF2-HMAC-SHA256 và phiên đăng nhập dùng cookie HttpOnly.
- Tài khoản đăng ký đầu tiên trên server được cấp `super_admin`. Có thể cố định tài khoản quản trị bằng biến môi trường `IKIGAI_SUPER_ADMIN_UID`.
- Tên và giới tính được khóa sau khi tạo hồ sơ; người chơi chỉ được đổi trang phục. Các chuyển động, cảm xúc, chat và sự kiện được đồng bộ theo thời gian thực.

### Lưu ý khi đưa lên mạng rộng

Server mẫu này dùng HTTP thuần cho mạng LAN. Nếu mở Internet, cần đặt sau reverse proxy HTTPS, giới hạn firewall, bổ sung rate limit/giám sát đăng nhập và dùng kho lưu trữ riêng cho dữ liệu/blob.

- **Python 3.8 trở lên.** Không cần cài thêm thư viện nào.
  - Windows: tải ở python.org. Khi cài, nhớ tick **Add Python to PATH**.
  - macOS và Linux: thường đã có sẵn `python3`.
- Một trình duyệt mới (Chrome, Edge, Firefox, Safari).

## Chạy

- **Windows:** bấm đúp `start-windows.bat`.
- **macOS / Linux:** mở Terminal trong thư mục này rồi chạy `./start-mac-linux.sh`, hoặc `python3 server.py`.

Trình duyệt sẽ mở `http://localhost:8000`. Muốn dùng cổng khác thì chạy `python3 server.py 9000`.

Để dừng, bấm `Ctrl + C` trong cửa sổ máy chủ.

### Chạy production

Production sẽ tắt hoàn toàn đồng nghiệp mẫu và ẩn công tắc bật NPC:

```bash
IKIGAI_ENV=production python3 server.py
```

Trên Windows Command Prompt, chạy `set IKIGAI_ENV=production` trước khi chạy `python server.py`.

### Đưa lên Internet miễn phí (Cloudflare Tunnel)

```bash
brew install cloudflared          # một lần
./deploy/start-production.sh      # in ra link https://….trycloudflare.com cho mọi người
```

- Dữ liệu production nằm ở `data-prod/` (lần chạy đầu sao chép từ `data/`), tách khỏi dữ liệu phát triển.
- Máy chủ chỉ nghe trên `127.0.0.1`; người ngoài chỉ vào được qua tunnel HTTPS. Máy Mac phải bật và giữ cửa sổ mở (script tự chặn máy ngủ).
- Link miễn phí đổi mỗi lần khởi động lại. Muốn link cố định cần tên miền trên Cloudflare (named tunnel).
- Ai có link đều đăng ký được tài khoản; chỉ super admin có quyền sắp xếp văn phòng, họp, tiếp khách.

Biến môi trường hỗ trợ: `PORT`, `IKIGAI_HOST`, `IKIGAI_DATA_DIR`, `IKIGAI_TRUST_PROXY=1` (đứng sau proxy: dùng IP thật trong `X-Forwarded-For` cho giới hạn đăng nhập, bật cookie `Secure` khi HTTPS), `IKIGAI_COOKIE_SECURE=1`, `IKIGAI_SUPER_ADMIN_UID`. Kiểm tra sống: `GET /healthz`.

### Kiểm thử

```bash
python3 scripts/test_live_actions.py   # spam họp/tiếp khách, request trễ, cooldown
python3 scripts/test_full_office.py    # 60 người, tranh ghế, chat, rớt mạng, giới hạn kết nối
python3 scripts/simulate_players.py --players 40
```

## Vào chung văn phòng với đồng nghiệp

Máy chạy `server.py` đóng vai máy chủ. Người khác cùng mạng (cùng Wi-Fi hoặc LAN) mở:

```
http://<địa chỉ IP của máy chủ>:8000
```

Ví dụ `http://192.168.1.25:8000`. Cách xem địa chỉ IP:
- Windows: chạy `ipconfig`, xem dòng IPv4.
- macOS: vào Cài đặt hệ thống, mục Mạng.
- Linux: chạy `ip addr`.

Nếu máy khác không vào được, cho phép Python đi qua tường lửa của máy chủ.

Mọi người thấy nhau đi lại, ngồi, trò chuyện, nhảy và vỗ tay theo thời gian thực. Mặt bằng lưu một nơi trên máy chủ nên ai cũng thấy giống nhau.

## Khác với bản trên claude.ai

- **Quyền:** tài khoản đầu tiên được đăng ký trên máy chủ được bootstrap thành `super_admin` (có thể chỉ định cố định bằng biến môi trường `IKIGAI_SUPER_ADMIN_UID`). Chỉ super admin được sắp xếp văn phòng, bấm Họp với Chủ tịch, Tiếp khách và thay đổi các dữ liệu dùng chung; người khác chỉ có quyền tương tác cá nhân.
- **Hồ sơ nhân vật:** sau khi tạo hồ sơ, tên và giới tính được khóa; người dùng chỉ có thể đổi trang phục. Nhân vật hỗ trợ các kiểu áo sơ mi ngắn tay, sơ mi dài tay và vest.
- **Tên hiển thị:** mỗi người tạo hồ sơ trong menu ở góc trên bên phải. Tên/giới tính được máy chủ giữ ổn định giữa các phiên; trang phục vẫn là tùy chọn riêng.
- **Di chuyển và bàn làm việc:** đoàn khách di chuyển theo đội hình cùng khu vực, có âm thanh bước chân; khi nhân vật ngồi vào bàn, màn hình trên bàn sẽ bật và tắt khi rời ghế.
- **Âm thanh:** nút loa ở góc trên tắt hoặc bật toàn bộ tiếng game trên máy của bạn, gồm TV, cuộc họp, bước chân và vỗ tay.
- **Họp với Chủ tịch:** chỉ super admin thấy nút bắt đầu/kết thúc họp. Trước khi bắt đầu, super admin chọn và có thể nghe thử một trong bốn bản ghi `aTay`, `chutich`, `chutich02`, `chutich03`; lựa chọn và thời điểm phát được đồng bộ cho mọi người. Bấm bắt đầu/kết thúc liên tục cũng an toàn: máy chủ chỉ giữ một lượt họp hoặc tiếp khách tại một thời điểm, bỏ qua yêu cầu trùng hoặc đến trễ, và chờ vài giây cho phần vỗ tay/tiễn khách xong mới cho bắt đầu lượt mới.
- **Chiếu lên TV:** không có trường chọn số phút trong giao diện; ảnh dùng thời lượng mặc định, video chạy theo độ dài tệp và YouTube dùng thời lượng mặc định của hệ thống.
- **Tiếp khách:** toàn bộ đoàn được giữ trong sảnh; khi bắt đầu tiếp khách, mọi nội dung đang chiếu trên TV được dừng và TV trở về trạng thái ban đầu.
- **Khôi phục TV:** super admin có nút `Khôi phục TV mặc định` để xóa toàn bộ hàng chờ trình chiếu trên mọi TV và trả TV về trạng thái ban đầu.
- **Trạng thái:** nút đặt trạng thái thủ công đã được gỡ khỏi giao diện. Trạng thái cuộc họp vẫn được hệ thống điều khiển nội bộ.
- **YouTube:** chiếu được trực tiếp trên TV, vì bản chạy trên máy tính không bị chặn như trên claude.ai. Máy cần có Internet, và video phải cho phép phát trên trang khác.
- **Không cần Internet** cho phần còn lại. Thư viện 3D và phông chữ đã được đóng gói sẵn.

## Dữ liệu nằm ở đâu

| Thư mục / tệp | Nội dung |
|---|---|
| `data/db.json` | Mặt bằng, đồ đạc, tường, hàng chờ TV, trạng thái họp và tiếp khách |
| `data/blobs/` | Ảnh và video do người dùng tải lên TV |
| `public/audio/` | Các bản ghi âm có sẵn cho cuộc họp với Chủ tịch (thêm tệp mới thì khai báo trong `CHAIR_TRACKS` ở `server.py` và `public/index.html`). Dùng AAC hoặc MP3: Chrome/Edge/Firefox không phát được `.m4a` mã hóa Apple Lossless (ALAC). Chuyển đổi: `ffmpeg -i vao.m4a -c:a aac -b:a 128k -movflags +faststart ra.m4a` |
| `public/` | Trang web (`index.html`), thư viện 3D, phông chữ |

- **Sao lưu:** chép thư mục `data` ra nơi khác.
- **Về lại mặt bằng ban đầu:** đóng máy chủ, chép đè `data/db.json` từ bản sao lưu.
- Trong `data/db.json` còn hai bản mặt bằng cũ (`backup-truoc-khi-sua`, `backup-ban-claude-1`) để đối chiếu.

## Mô phỏng 20 người chơi online

Chạy trong thư mục dự án:

```bash
python scripts/simulate_players.py --players 20 --duration 120 --interval 2
```

Mở địa chỉ `http://127.0.0.1:8123` trong lúc chương trình chạy, tạo tài khoản thử để vào xem bot di chuyển cùng bạn. Mỗi bot có tài khoản và kết nối sự kiện riêng. Chương trình kiểm tra sự hiện diện của cả nhóm, chat chung và tin nhắn riêng; cuối cùng in `PASS` hoặc báo lỗi. Máy chủ thử nghiệm chỉ lắng nghe trên máy này, dùng cơ sở dữ liệu tạm và tự xóa tài khoản thử khi kết thúc. Có thể đổi cổng bằng `--port`.

## Cấu trúc

```
van-phong-tang-8/
├─ server.py              máy chủ (Python thuần)
├─ start-windows.bat      chạy trên Windows
├─ start-mac-linux.sh     chạy trên macOS / Linux
├─ public/
│  ├─ audio/             các bản ghi âm họp với Chủ tịch
│  ├─ index.html          toàn bộ văn phòng 3D
│  ├─ local-runtime.js    nối trang với server.py (thay cho claude.ai)
│  ├─ vendor/             three.js r128, OrbitControls, CSS3DRenderer
│  └─ fonts/              Be Vietnam Pro, Noto Serif
└─ data/
   ├─ db.json
   └─ blobs/
```
