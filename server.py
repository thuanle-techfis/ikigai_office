#!/usr/bin/env python3
"""Văn phòng tầng 8 – máy chủ chạy local.

Chỉ dùng thư viện có sẵn của Python 3 (không cần cài thêm gì).
  python3 server.py            -> mở http://localhost:8000
  python3 server.py 9000       -> chạy ở cổng khác
Đồng nghiệp cùng mạng LAN mở http://<địa-chỉ-IP-máy-này>:8000 để vào chung văn phòng.

Máy chủ này thay cho phần hạ tầng của claude.ai:
  - lưu mặt bằng, đồ đạc, tường, hàng chờ TV, trạng thái họp / tiếp khách  -> data/db.json
  - lưu ảnh, video, ghi âm tải lên                                          -> data/blobs/
  - truyền trò chuyện, cảm xúc, vị trí của mọi người theo thời gian thực     -> /api/events
"""
import hashlib, hmac, json, os, secrets, sys, threading, queue, uuid, mimetypes, re, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, 'public')
DATA = os.path.join(ROOT, 'data')
BLOBS = os.path.join(DATA, 'blobs')
DB_FILE = os.path.join(DATA, 'db.json')
MAX_UPLOAD = 50 * 1024 * 1024
MAX_REALTIME_BODY = 128 * 1024
MAX_SSE_QUEUE = 256
MAX_SSE_PEERS_PER_UID = 4
MAX_SSE_CLIENTS = 128
PEER_RE = re.compile(r'[\w-]{4,40}')
USERNAME_RE = re.compile(r'^[A-Za-z0-9_.-]{3,32}$')
PASSWORD_MIN = 8
PASSWORD_MAX = 256
SESSION_TTL = 7 * 24 * 60 * 60
AUTH_WINDOW = 15 * 60
AUTH_MAX_FAILURES = 8

lock = threading.RLock()
db = {'docs': {}}   # "collection/doc" -> {"data": {...}, "version": n}
users = {}          # user id -> display name
peers = {}          # peer id -> {"peer", "by", "presence"}
clients = {}        # peer id -> Queue of server-sent events
peer_sessions = {}  # peer id -> authenticated UID that owns the live stream
sessions = {}       # opaque token -> {uid, expiresAt}
auth_failures = {}  # client IP -> {count, windowStarted}
super_admin_uid = None
PROTECTED_DOCS = {'layout/current', 'live/visit', 'live/chairman', 'auth/superadmin'}


def ensure_super_admin(uid=None):
    """Return the configured super-admin UID, bootstrapping the first account once."""
    global super_admin_uid
    configured = os.environ.get('IKIGAI_SUPER_ADMIN_UID', '').strip()
    if configured:
        super_admin_uid = configured[:64]
        return super_admin_uid
    existing = db.get('docs', {}).get('auth/superadmin')
    if existing and isinstance(existing.get('data'), dict):
        value = str(existing['data'].get('uid', '')).strip()
        accounts = db.get('accounts', {})
        if value and (value in accounts or not uid or len(accounts) > 1):
            super_admin_uid = value[:64]
            return super_admin_uid
    if uid:
        value = str(uid).strip()[:64]
        if value:
            version = db.get('docs', {}).get('auth/superadmin', {}).get('version', 0) + 1
            db.setdefault('docs', {})['auth/superadmin'] = {
                'data': {'uid': value, 'createdAt': __import__('time').time()},
                'version': version,
            }
            super_admin_uid = value
            save_db()
    return super_admin_uid


def is_super_admin(uid):
    with lock:
        return bool(uid and ensure_super_admin(uid) == str(uid).strip())


def lock_presence_profile(uid, presence):
    """Persist the first name/gender and prevent later presence updates changing them."""
    if not uid or not isinstance(presence, dict):
        return presence
    safe_uid = re.sub(r'[^\w-]', '', str(uid))[:64]
    if not safe_uid:
        return presence
    profile_path = 'profiles/' + safe_uid
    account = db.get('accounts', {}).get(uid) or {}
    outfit = dict(presence.get('outfit') or {})
    name = str(account.get('name') or '').strip()[:20]
    gender = 'female' if account.get('gender') == 'female' else 'male'
    saved = db.get('docs', {}).get(profile_path)
    if not name:
        return presence
    presence = dict(presence)
    presence['nick'] = name
    outfit['gender'] = gender
    presence['outfit'] = outfit
    if not saved or not isinstance(saved.get('data'), dict) or saved['data'].get('name') != name or saved['data'].get('gender') != gender:
        db.setdefault('docs', {})[profile_path] = {
            'data': {'name': name, 'gender': gender},
            'version': saved.get('version', 0) + 1 if saved else 1,
        }
        save_db()
    return presence


def load_db():
    global db, super_admin_uid
    if os.path.exists(DB_FILE):
        with open(DB_FILE, encoding='utf-8') as f:
            db = json.load(f)
    db.setdefault('docs', {})
    db.setdefault('accounts', {})
    super_admin_uid = None


def save_db():
    tmp = DB_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False)
    os.replace(tmp, DB_FILE)


def normalize_username(value):
    username = str(value or '').strip()
    return username if USERNAME_RE.fullmatch(username) else None


def password_hash(password, salt=None):
    raw = str(password or '').encode('utf-8')
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', raw, salt, 310000)
    return salt.hex(), digest.hex()


def verify_password(password, account):
    try:
        salt = bytes.fromhex(account['passwordSalt'])
        _, digest = password_hash(password, salt)
        return hmac.compare_digest(digest, str(account.get('passwordHash', '')))
    except (KeyError, TypeError, ValueError):
        return False


def public_account(uid, account):
    return {'uid': uid, 'username': account.get('username', ''), 'name': account.get('name', ''),
            'gender': account.get('gender', 'male'), 'superAdmin': bool(uid and uid == ensure_super_admin())}


def issue_session(uid):
    token = secrets.token_urlsafe(32)
    sessions[token] = {'uid': uid, 'expiresAt': time.time() + SESSION_TTL}
    return token


def auth_rate_limit(ip):
    with lock:
        now = time.time()
        item = auth_failures.get(ip)
        if not item or now - item.get('windowStarted', 0) >= AUTH_WINDOW:
            auth_failures[ip] = {'count': 0, 'windowStarted': now}
            return 0
        if item.get('count', 0) >= AUTH_MAX_FAILURES:
            return max(1, int(AUTH_WINDOW - (now - item['windowStarted'])))
        return 0


def record_auth_failure(ip):
    with lock:
        auth_rate_limit(ip)
        auth_failures[ip]['count'] += 1


def clear_auth_failures(ip):
    with lock:
        auth_failures.pop(ip, None)


def session_uid(headers):
    auth = str(headers.get('Authorization', ''))
    token = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    if not token:
        cookie = str(headers.get('Cookie', ''))
        for part in cookie.split(';'):
            key, _, value = part.strip().partition('=')
            if key == 'ikigai_session':
                token = value
                break
    if not token:
        return None
    session = sessions.get(token)
    if not session or session.get('expiresAt', 0) <= time.time():
        sessions.pop(token, None)
        return None
    uid = session.get('uid')
    return uid if uid in db.get('accounts', {}) else None


def valid_path(p, even):
    """Documents have an even number of segments, collections an odd number."""
    if not p or not re.fullmatch(r'[\w\-.~:@+]+(/[\w\-.~:@+]+)*', p):
        return False
    return (len(p.split('/')) % 2 == 0) == even


def broadcast(event):
    msg = json.dumps(event, ensure_ascii=False)
    with lock:
        qs = list(clients.values())
    for q in qs:
        enqueue_event(q, msg)


def broadcast_to_users(event, uids):
    msg = json.dumps(event, ensure_ascii=False)
    wanted = set(uids)
    with lock:
        qs = [q for peer, q in clients.items() if peers.get(peer, {}).get('by') in wanted]
    for q in qs:
        enqueue_event(q, msg)


def enqueue_event(q, msg):
    try:
        q.put_nowait(msg)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(json.dumps({'type': 'resync'}))
        except queue.Full:
            pass


def peers_event():
    with lock:
        return {'type': 'peers', 'peers': list(peers.values())}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    # ---------- helpers ----------
    def send_json(self, obj, code=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def auth_uid(self):
        return session_uid(self.headers)

    def require_auth(self):
        uid = self.auth_uid()
        if not uid:
            self.send_json({'error': 'not_authenticated'}, 401)
            return None
        return uid

    def auth_response(self, uid, account):
        token = issue_session(uid)
        return self.send_json(public_account(uid, account), extra_headers={
            'Set-Cookie': f'ikigai_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}'
        })

    def read_body(self, limit=5 * 1024 * 1024):
        n = int(self.headers.get('Content-Length') or 0)
        if n < 0 or n > limit:
            raise OverflowError('too_large')
        return self.rfile.read(n) if n else b''

    def read_json(self, limit=5 * 1024 * 1024):
        return json.loads(self.read_body(limit).decode('utf-8') or '{}')

    def send_file(self, path, ctype):
        """Static files and uploads, with byte ranges so audio and video can seek."""
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get('Range')
        if rng:
            m = re.match(r'bytes=(\d*)-(\d*)', rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                elif m.group(2):
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                code = 206
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if code == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with open(path, 'rb') as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p == '/api/auth/me':
            uid = self.auth_uid()
            account = db.get('accounts', {}).get(uid) if uid else None
            if not uid or not isinstance(account, dict):
                return self.send_json({'error': 'not_authenticated'}, 401)
            return self.send_json(public_account(uid, account))
        if p == '/api/events':
            return self.events(q.get('peer', [''])[0])
        if p == '/api/db/doc':
            if not self.require_auth():
                return
            path = q.get('path', [''])[0]
            if not valid_path(path, True):
                return self.send_json({'error': 'bad_path'}, 400)
            with lock:
                d = db['docs'].get(path)
            return self.send_json({'exists': bool(d), 'data': d['data'] if d else None, 'version': d['version'] if d else 0})
        if p == '/api/db/collection':
            if not self.require_auth():
                return
            path = q.get('path', [''])[0]
            if not valid_path(path, False):
                return self.send_json({'error': 'bad_path'}, 400)
            pre = path + '/'
            with lock:
                docs = [{'id': k[len(pre):], 'data': v['data'], 'version': v['version']}
                        for k, v in db['docs'].items() if k.startswith(pre) and '/' not in k[len(pre):]]
            return self.send_json({'docs': docs})
        if p == '/api/user/role':
            actual = self.require_auth()
            if not actual:
                return
            requested = str(q.get('id', [''])[0])[:64]
            uid = requested if requested == actual else actual
            with lock:
                admin = ensure_super_admin()
            return self.send_json({'id': uid, 'role': 'super_admin' if uid == admin else 'viewer',
                                   'superAdmin': bool(uid == admin)})
        if p == '/api/users':
            if not self.require_auth():
                return
            ids = [i for i in q.get('ids', [''])[0].split(',') if i]
            with lock:
                return self.send_json({i: {'id': i, 'name': users.get(i, '')} for i in ids})
        if p.startswith('/_blob/'):
            if not self.require_auth():
                return
            bid = p[len('/_blob/'):]
            fp = os.path.join(BLOBS, bid)
            if not re.fullmatch(r'[0-9a-f]{32}', bid) or not os.path.exists(fp):
                return self.send_json({'error': 'not_found'}, 404)
            tp = fp + '.type'
            ctype = open(tp).read().strip() if os.path.exists(tp) else 'application/octet-stream'
            return self.send_file(fp, ctype)
        rel = 'index.html' if p in ('/', '') else p.lstrip('/')
        fp = os.path.normpath(os.path.join(PUBLIC, rel))
        try:
            inside_public = os.path.commonpath([os.path.normcase(os.path.abspath(PUBLIC)),
                                                 os.path.normcase(os.path.abspath(fp))]) == os.path.normcase(os.path.abspath(PUBLIC))
        except ValueError:
            inside_public = False
        if not inside_public or not os.path.isfile(fp):
            return self.send_json({'error': 'not_found'}, 404)
        ctype = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype.endswith('javascript'):
            ctype += '; charset=utf-8'
        self.send_file(fp, ctype)

    def events(self, peer):
        uid = self.auth_uid()
        if not uid:
            return self.send_json({'error': 'not_authenticated'}, 401)
        if not PEER_RE.fullmatch(peer or ''):
            return self.send_json({'error': 'bad_peer'}, 400)
        with lock:
            owner = peer_sessions.get(peer)
            if owner and owner != uid:
                return self.send_json({'error': 'peer_not_owned'}, 403)
            if not owner and sum(1 for item in peer_sessions.values() if item == uid) >= MAX_SSE_PEERS_PER_UID:
                return self.send_json({'error': 'too_many_connections'}, 429)
            if not owner and len(clients) >= MAX_SSE_CLIENTS:
                return self.send_json({'error': 'server_busy'}, 503, {'Retry-After': '30'})
            peer_sessions[peer] = uid
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        q = queue.Queue(MAX_SSE_QUEUE)
        with lock:
            clients[peer] = q
        q.put(json.dumps(peers_event(), ensure_ascii=False))
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(('data: ' + msg + '\n\n').encode('utf-8'))
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with lock:
                if clients.get(peer) is q:
                    del clients[peer]
                gone = peers.pop(peer, None)
                if not clients.get(peer) and peer_sessions.get(peer) == uid:
                    peer_sessions.pop(peer, None)
            if gone:
                broadcast(peers_event())

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p == '/api/blob':
                if not self.require_auth():
                    return
                raw = self.read_body(MAX_UPLOAD)
                ctype = (self.headers.get('X-Content-Type') or 'application/octet-stream').split(';')[0].strip()
                if not re.fullmatch(r'image/(png|jpeg|gif|webp)|video/(mp4|webm)|audio/(mpeg|mp4|webm|ogg)', ctype):
                    return self.send_json({'error': 'unsupported_type'}, 415)
                bid = uuid.uuid4().hex
                with open(os.path.join(BLOBS, bid), 'wb') as f:
                    f.write(raw)
                with open(os.path.join(BLOBS, bid + '.type'), 'w') as f:
                    f.write(ctype)
                return self.send_json({'id': bid, 'url': '/_blob/' + bid, 'sizeBytes': len(raw), 'contentType': ctype})
            tv_doc_request = p == '/api/db/doc' and q.get('path', [''])[0].startswith('tv/')
            body = self.read_json(MAX_REALTIME_BODY if p in {
                '/api/auth/register', '/api/auth/login', '/api/auth/logout',
                '/api/tv/clear', '/api/user', '/api/room/presence', '/api/room/emit'
            } or tv_doc_request else 5 * 1024 * 1024)
            if p == '/api/auth/register':
                ip = self.client_address[0]
                retry = auth_rate_limit(ip)
                if retry:
                    return self.send_json({'error': 'too_many_attempts'}, 429, {'Retry-After': str(retry)})
                username = normalize_username(body.get('username'))
                password = str(body.get('password') or '')
                name = str(body.get('name') or '').strip()[:20]
                gender = 'female' if body.get('gender') == 'female' else 'male'
                if not username or not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX) or not name:
                    return self.send_json({'error': 'invalid_registration'}, 400)
                with lock:
                    if any(a.get('usernameLower') == username.lower() for a in db.get('accounts', {}).values()):
                        record_auth_failure(ip)
                        return self.send_json({'error': 'username_taken'}, 409)
                    uid = 'u_' + secrets.token_hex(12)
                    salt, digest = password_hash(password)
                    db.setdefault('accounts', {})[uid] = {
                        'username': username, 'usernameLower': username.lower(),
                        'passwordSalt': salt, 'passwordHash': digest,
                        'name': name, 'gender': gender, 'createdAt': time.time()
                    }
                    users[uid] = name
                    ensure_super_admin(uid)
                    save_db()
                clear_auth_failures(ip)
                return self.auth_response(uid, db['accounts'][uid])
            if p == '/api/auth/login':
                ip = self.client_address[0]
                retry = auth_rate_limit(ip)
                if retry:
                    return self.send_json({'error': 'too_many_attempts'}, 429, {'Retry-After': str(retry)})
                username = normalize_username(body.get('username'))
                password = str(body.get('password') or '')
                with lock:
                    found = next(((uid, account) for uid, account in db.get('accounts', {}).items()
                                  if account.get('usernameLower') == (username or '').lower()), None)
                if len(password) > PASSWORD_MAX or not found or not verify_password(password, found[1]):
                    record_auth_failure(ip)
                    return self.send_json({'error': 'invalid_credentials'}, 401)
                uid, account = found
                users[uid] = account.get('name', '')
                clear_auth_failures(ip)
                return self.auth_response(uid, account)
            if p == '/api/auth/logout':
                uid = self.auth_uid()
                if uid:
                    for token, session in list(sessions.items()):
                        if session.get('uid') == uid:
                            sessions.pop(token, None)
                return self.send_json({'ok': True}, extra_headers={
                    'Set-Cookie': 'ikigai_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'
                })
            if p == '/api/tv/clear':
                uid = self.require_auth()
                if not uid:
                    return
                if not is_super_admin(uid):
                    return self.send_json({'error': 'not_permitted'}, 403)
                with lock:
                    paths = [path for path in db['docs'] if path.startswith('tv/') and path.count('/') == 1]
                    for path in paths:
                        db['docs'].pop(path, None)
                    if paths:
                        save_db()
                for path in paths:
                    broadcast({'type': 'db', 'path': path})
                return self.send_json({'ok': True, 'deleted': len(paths)})
            if p == '/api/db/doc':
                uid = self.require_auth()
                if not uid:
                    return
                path = q.get('path', [''])[0]
                if not valid_path(path, True):
                    return self.send_json({'error': 'bad_path'}, 400)
                if path in PROTECTED_DOCS and not is_super_admin(uid):
                    return self.send_json({'error': 'not_permitted'}, 403)
                if path.startswith('tv/') and path.count('/') == 1 and path != 'tv/' + uid and not is_super_admin(uid):
                    return self.send_json({'error': 'not_permitted'}, 403)
                if not is_super_admin(uid) and path != 'tv/' + uid:
                    return self.send_json({'error': 'not_permitted'}, 403)
                with lock:
                    if body.get('delete'):
                        db['docs'].pop(path, None)
                        version = 0
                    else:
                        data = body.get('data')
                        if not isinstance(data, dict):
                            return self.send_json({'error': 'bad_data'}, 400)
                        if path.startswith('tv/') and path.count('/') == 1 and path == 'tv/' + uid:
                            items = data.get('items')
                            if not isinstance(items, list) or len(items) > 3:
                                return self.send_json({'error': 'bad_tv_queue'}, 400)
                        if body.get('merge') and path in db['docs']:
                            data = {**db['docs'][path]['data'], **data}
                        version = db['docs'].get(path, {}).get('version', 0) + 1
                        db['docs'][path] = {'data': data, 'version': version}
                    save_db()
                broadcast({'type': 'db', 'path': path})
                return self.send_json({'ok': True, 'version': version})
            if p == '/api/user':
                uid_actual = self.require_auth()
                if not uid_actual:
                    return
                uid = str(body.get('id', ''))[:64]
                if uid and uid == uid_actual:
                    with lock:
                        account = db.get('accounts', {}).get(uid, {})
                        users[uid] = str(account.get('name') or '')[:20]
                        ensure_super_admin()
                else:
                    return self.send_json({'error': 'not_permitted'}, 403)
                return self.send_json({'ok': True})
            if p == '/api/room/presence':
                uid_actual = self.require_auth()
                if not uid_actual:
                    return
                peer = str(body.get('peer', ''))
                if not PEER_RE.fullmatch(peer):
                    return self.send_json({'error': 'bad_peer'}, 400)
                with lock:
                    uid = str(body.get('by', ''))[:64]
                    if uid != uid_actual:
                        return self.send_json({'error': 'not_permitted'}, 403)
                    owner = peer_sessions.get(peer)
                    if owner and owner != uid_actual:
                        return self.send_json({'error': 'peer_not_owned'}, 403)
                    peer_sessions[peer] = uid_actual
                    if peers.get(peer, {}).get('by') not in (None, uid_actual):
                        return self.send_json({'error': 'peer_not_owned'}, 403)
                    presence = lock_presence_profile(uid, body.get('presence'))
                    peers[peer] = {'peer': peer, 'by': uid, 'presence': presence}
                broadcast(peers_event())
                return self.send_json({'ok': True})
            if p == '/api/room/emit':
                uid_actual = self.require_auth()
                if not uid_actual:
                    return
                if str(body.get('by', ''))[:64] != uid_actual:
                    return self.send_json({'error': 'not_permitted'}, 403)
                peer = str(body.get('peer', ''))[:40]
                with lock:
                    if peer_sessions.get(peer) != uid_actual or peers.get(peer, {}).get('by') != uid_actual:
                        return self.send_json({'error': 'not_permitted'}, 403)
                topic = str(body.get('topic', ''))[:40]
                data = body.get('data')
                event = {'type': 'room', 'topic': topic, 'peer': peer,
                         'by': uid_actual, 'data': data}
                if topic == 'chat' and isinstance(data, dict) and data.get('ch') == 'dm':
                    target = str(data.get('to', ''))[:64]
                    if not target:
                        return self.send_json({'error': 'bad_recipient'}, 400)
                    broadcast_to_users(event, {uid_actual, target})
                else:
                    broadcast(event)
                return self.send_json({'ok': True})
            return self.send_json({'error': 'not_found'}, 404)
        except OverflowError:
            return self.send_json({'error': 'too_large'}, 413)
        except json.JSONDecodeError:
            return self.send_json({'error': 'bad_json'}, 400)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    os.makedirs(BLOBS, exist_ok=True)
    load_db()
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    srv.daemon_threads = True
    print(f'Văn phòng tầng 8 đang chạy tại http://localhost:{port}')
    print(f'Đồng nghiệp cùng mạng mở http://<IP máy này>:{port}   (Ctrl+C để dừng)')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nĐã dừng.')


if __name__ == '__main__':
    main()
