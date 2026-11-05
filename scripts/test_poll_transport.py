#!/usr/bin/env python3
"""Long-polling transport (used behind Cloudflare quick tunnels) mixed with SSE clients.

    python3 scripts/test_poll_transport.py
"""

import http.cookiejar
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import server  # noqa: E402
from simulate_players import Bot, wait_for  # noqa: E402


class PollBot:
    """Behaves like the browser runtime in polling mode."""

    def __init__(self, index, base):
        self.index, self.base = index, base
        self.name = f'Poll {index:02d}'
        self.peer = f'poll{index:04d}abcd'
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.visible, self.messages, self.fresh_count = set(), set(), 0
        self.stop = threading.Event()
        self.guard = threading.Lock()

    def call(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b'{}')

    def start(self):
        _, account = self.call('POST', '/api/auth/register', {
            'username': f'poll_{self.index:02d}', 'password': 'password123', 'name': self.name})
        self.uid = account['uid']
        threading.Thread(target=self.loop, daemon=True).start()
        assert wait_for(lambda: self.fresh_count > 0, 5)
        assert self.presence()[0] == 200

    def presence(self, seat=None):
        return self.call('POST', '/api/room/presence', {'peer': self.peer, 'by': self.uid, 'presence': {
            'uid': self.uid, 'nick': self.name, 'seat': seat, 'spot': [1, 1], 'status': 'work'}})

    def loop(self):
        while not self.stop.is_set():
            status, body = self.call('GET', '/api/poll?peer=' + self.peer)
            if status != 200:
                time.sleep(0.5)
                continue
            with self.guard:
                if body.get('fresh'):
                    self.fresh_count += 1
                    if self.fresh_count > 1:  # re-registered after a gap: announce again, like the runtime
                        threading.Thread(target=self.presence, daemon=True).start()
                for event in body.get('events', []):
                    if event.get('type') == 'peers':
                        self.visible = {p['by'] for p in event.get('peers', [])}
                    elif event.get('type') == 'room' and event.get('topic') == 'chat':
                        self.messages.add(event['data']['text'])

    def chat(self, text, to=None):
        data = {'text': text, 'ch': 'dm' if to else 'all', **({'to': to} if to else {})}
        return self.call('POST', '/api/room/emit', {'peer': self.peer, 'by': self.uid, 'topic': 'chat', 'data': data})


def main():
    with tempfile.TemporaryDirectory(prefix='ikigai-poll-') as temporary:
        server.DB_FILE = str(Path(temporary) / 'db.json')
        server.BLOBS = str(Path(temporary) / 'blobs')
        Path(server.BLOBS).mkdir()
        server.db = {'docs': {}, 'accounts': {}}
        for table in (server.users, server.peers, server.clients, server.peer_sessions,
                      server.sessions, server.auth_failures, server.emit_log, server.poll_seen, server.poll_inflight):
            table.clear()
        server.save_db()
        server.POLL_TTL = 4  # make the "tab went away" check fast for the test (waiting polls are never reaped)
        httpd = server.OfficeServer(('127.0.0.1', 0), server.Handler)
        base = f'http://127.0.0.1:{httpd.server_port}'
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        pollers, sse = [], []
        try:
            pollers = [PollBot(i, base) for i in range(1, 31)]
            sse = [Bot(i, base, 'mix') for i in range(1, 11)]
            with ThreadPoolExecutor(max_workers=20) as pool:
                list(pool.map(lambda b: b.start(), pollers))
                list(pool.map(lambda b: b.start((0, 0)), sse))
            everyone = {b.uid for b in pollers + sse}
            assert wait_for(lambda: all(b.visible >= everyone for b in pollers)
                            and all(b.visible_uids >= everyone for b in sse), 10), 'mixed presence did not converge'
            print('OK  30 polling + 10 SSE players all see the same 40 people')

            pollers[0].chat('qua long-poll')
            sse[0].chat('qua sse')
            assert wait_for(lambda: all({'qua long-poll', 'qua sse'} <= b.messages for b in pollers)
                            and all({'qua long-poll', 'qua sse'} <= b.snapshot()[1] for b in sse), 8)
            pollers[1].chat('rieng tu', pollers[2].uid)
            time.sleep(1)
            assert [b.index for b in pollers if 'rieng tu' in b.messages] == [2, 3]
            print('OK  chat crosses transports; DMs reach only the two people')

            # Closing a tab: the leave beacon frees the seat immediately.
            assert pollers[3].presence(seat='seat-A')[0] == 200
            pollers[3].stop.set()
            pollers[3].call('POST', '/api/room/leave', {'peer': pollers[3].peer})
            assert pollers[3].peer not in server.peers
            assert pollers[4].presence(seat='seat-A')[1].get('seatTaken') is None
            print('OK  leave beacon removes the player and frees their seat at once')

            # A tab that stops polling (laptop asleep) is removed after POLL_TTL ...
            sleeper = pollers[5]
            sleeper.stop.set()
            assert wait_for(lambda: sleeper.peer not in server.peers, server.POLL_TTL + 30), 'stale poller kept'
            assert wait_for(lambda: sleeper.uid not in pollers[0].visible, 5)
            # ... and comes back on its own when it wakes up.
            sleeper.stop.clear()
            threading.Thread(target=sleeper.loop, daemon=True).start()
            assert wait_for(lambda: sleeper.uid in pollers[0].visible, 10), 'woken tab did not rejoin'
            print('OK  sleeping tab is removed, then rejoins by itself when it wakes')

            # SSE reconnect: the old stream closing late must not delete the new presence.
            bot = sse[1]
            with server.lock:
                old_q = server.clients[bot.peer]
                new_q = server.queue.Queue(server.MAX_SSE_QUEUE)
                server.clients[bot.peer] = new_q  # the reconnected stream took over
                assert server.drop_peer(bot.peer, old_q) is False
                assert bot.peer in server.peers
                server.clients[bot.peer] = old_q
            print('OK  a late-closing old stream no longer removes a reconnected player')
        finally:
            for b in pollers:
                b.stop.set()
            for b in sse:
                b.close()
            server.broadcast({'type': 'stop'})
            httpd.shutdown()
            httpd.server_close()
    print('PASS: long-polling transport, mixed with SSE')


if __name__ == '__main__':
    main()
