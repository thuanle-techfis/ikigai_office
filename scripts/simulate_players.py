#!/usr/bin/env python3
"""Run real HTTP/SSE clients against a temporary office server.

Example: python scripts/simulate_players.py --players 20 --duration 120
Open the printed URL while the script runs to watch the bots in the game.
The temporary accounts and database disappear when the script exits.
"""

import argparse
import http.cookiejar
import json
import math
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402


class Bot:
    def __init__(self, index, base_url, run_id):
        self.index = index
        self.name = f'Bot {index:02d}'
        self.username = f'sim_{run_id}_{index:02d}'
        self.password = secrets.token_urlsafe(18)
        self.peer = 'p' + secrets.token_hex(8)
        self.base_url = base_url
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.uid = None
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.error = None
        self.visible_uids = set()
        self.received_messages = set()
        self.event_count = 0
        self.guard = threading.Lock()

    def request(self, path, payload):
        body = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            self.base_url + path, data=body, method='POST',
            headers={'Content-Type': 'application/json'},
        )
        with self.opener.open(request, timeout=10) as response:
            return json.load(response)

    def start(self, spot):
        account = self.request('/api/auth/register', {
            'username': self.username, 'password': self.password,
            'name': self.name, 'gender': 'female' if self.index % 2 else 'male',
        })
        self.uid = account['uid']
        self.thread = threading.Thread(target=self.listen, name=self.name, daemon=True)
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            raise RuntimeError(f'{self.name}: SSE could not connect: {self.error}')
        self.move(spot)

    def listen(self):
        try:
            request = urllib.request.Request(self.base_url + '/api/events?peer=' + self.peer)
            with self.opener.open(request, timeout=20) as response:
                self.ready.set()
                for line in response:
                    if self.stop.is_set():
                        break
                    if not line.startswith(b'data: '):
                        continue
                    event = json.loads(line[6:])
                    with self.guard:
                        self.event_count += 1
                        if event.get('type') == 'peers':
                            self.visible_uids = {
                                peer['by'] for peer in event.get('peers', []) if peer.get('by')
                            }
                        elif event.get('type') == 'room' and event.get('topic') == 'chat':
                            message_id = event.get('data', {}).get('text')
                            if message_id:
                                self.received_messages.add(message_id)
        except (OSError, ValueError, urllib.error.HTTPError) as error:
            if not self.stop.is_set():
                self.error = error
                self.ready.set()

    def move(self, spot):
        self.request('/api/room/presence', {
            'peer': self.peer, 'by': self.uid,
            'presence': {
                'uid': self.uid, 'nick': self.name, 'seat': None,
                'spot': [round(spot[0], 2), round(spot[1], 2)],
                'status': 'work', 'shirt': '#6e9eff',
                'outfit': {
                    'gender': 'female' if self.index % 2 else 'male',
                    'sleeve': 'short' if self.index % 3 == 0 else 'long',
                    'pants': 'navy' if self.index % 2 else 'black',
                    'vest': self.index % 4 == 0,
                },
            },
        })

    def chat(self, text, recipient=None):
        data = {'text': text, 'ch': 'dm' if recipient else 'all'}
        if recipient:
            data['to'] = recipient
        self.request('/api/room/emit', {
            'peer': self.peer, 'by': self.uid, 'topic': 'chat', 'data': data,
        })

    def snapshot(self):
        with self.guard:
            return len(self.visible_uids), set(self.received_messages), self.event_count

    def close(self):
        self.stop.set()


def spot_for(index, step, room):
    col, row = (index - 1) % 5, (index - 1) // 5
    columns = 5
    rows = max(1, math.ceil(room['players'] / columns))
    spacing_x = min(1.15, (room['w'] - 1.8) / max(1, columns - 1))
    spacing_z = min(1.15, (room['d'] - 1.8) / max(1, rows - 1))
    sway = math.sin(step * 0.65 + index * 0.7) * 0.18
    x = room['x'] + (col - 2) * spacing_x + sway
    z = room['z'] + (row - (rows - 1) / 2) * spacing_z + math.cos(step * 0.65 + index) * 0.18
    return x, z


def wait_for(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def main():
    parser = argparse.ArgumentParser(description='Simulate online players on a temporary local server.')
    parser.add_argument('--players', type=int, default=20, help='Number of bot accounts (1-40; default 20).')
    parser.add_argument('--duration', type=float, default=30, help='Seconds to keep bots online (default 30).')
    parser.add_argument('--interval', type=float, default=2, help='Seconds between movement updates (default 2).')
    parser.add_argument('--port', type=int, default=8123, help='Local test server port (default 8123).')
    args = parser.parse_args()
    if not 1 <= args.players <= 40 or args.duration <= 0 or args.interval < 0.5:
        parser.error('Use 1-40 players, a positive duration, and interval >= 0.5 seconds.')

    with tempfile.TemporaryDirectory(prefix='ikigai-bots-') as temporary:
        server.DB_FILE = str(Path(temporary) / 'db.json')
        server.BLOBS = str(Path(temporary) / 'blobs')
        Path(server.BLOBS).mkdir()
        source = json.loads((ROOT / 'data' / 'db.json').read_text(encoding='utf-8'))
        layout = source['docs']['layout/current']
        server.db = {'docs': {'layout/current': layout}, 'accounts': {}}
        server.users.clear()
        server.peers.clear()
        server.clients.clear()
        server.peer_sessions.clear()
        server.sessions.clear()
        server.auth_failures.clear()
        server.super_admin_uid = None
        server.save_db()

        httpd = ThreadingHTTPServer(('127.0.0.1', args.port), server.Handler)
        httpd.daemon_threads = True
        base_url = f'http://127.0.0.1:{httpd.server_port}'
        threading.Thread(target=httpd.serve_forever, name='office-server', daemon=True).start()
        print(f'Test server: {base_url} — open this URL to watch the bots.', flush=True)
        room = dict(layout['data']['rooms']['work'], players=args.players)
        bots = []
        run_id = secrets.token_hex(3)
        try:
            for index in range(1, args.players + 1):
                bot = Bot(index, base_url, run_id)
                bots.append(bot)
                bot.start(spot_for(index, 0, room))
            expected = {bot.uid for bot in bots}
            if not wait_for(lambda: all(bot.visible_uids >= expected for bot in bots), timeout=8):
                raise RuntimeError('Not all bots received the full presence list.')
            print(f'Online: {len(server.peers)} peers, {len(server.clients)} SSE streams; all {len(bots)} bots see the group.', flush=True)

            if len(bots) > 1:
                public_message = 'Bot online test: hello everyone'
                private_message = 'Bot online test: private message'
                bots[0].chat(public_message)
                bots[0].chat(private_message, bots[1].uid)
                if not wait_for(lambda: all(public_message in bot.snapshot()[1] for bot in bots)):
                    raise RuntimeError('Broadcast chat did not reach every bot.')
                if not wait_for(lambda: all((private_message in bot.snapshot()[1]) == (i < 2)
                                            for i, bot in enumerate(bots))):
                    raise RuntimeError('Direct message reached the wrong recipients.')
                print('Chat: broadcast reached everyone; DM reached only sender and recipient.', flush=True)

            started = time.monotonic()
            step = 0
            while time.monotonic() - started < args.duration:
                step += 1
                for bot in bots:
                    bot.move(spot_for(bot.index, step, room))
                remaining = args.duration - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(min(args.interval, remaining))

            visible = [bot.snapshot()[0] for bot in bots]
            errors = [f'{bot.name}: {bot.error}' for bot in bots if bot.error]
            if errors or min(visible) < args.players or len(server.peers) < args.players:
                raise RuntimeError(f'Online state diverged: visible={visible}, errors={errors}')
            print(f'PASS: {args.players} accounts, {step} movement rounds, {min(visible)}-{max(visible)} peers seen per bot.', flush=True)
        finally:
            for bot in bots:
                bot.close()
            server.broadcast({'type': 'sim-stop'})
            for bot in bots:
                if hasattr(bot, 'thread'):
                    bot.thread.join(timeout=2)
            httpd.shutdown()
            httpd.server_close()
        print('Temporary accounts and game state removed.', flush=True)


if __name__ == '__main__':
    main()
