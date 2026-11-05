#!/usr/bin/env python3
"""Load/edge-case checks for a full office: more players than seats, seat races,
live chat, flood guard, disconnects, connection cap and proxy-aware login limits.

Runs real HTTP/SSE clients against a temporary server and database:
    python3 scripts/test_full_office.py
"""

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import server  # noqa: E402
from simulate_players import Bot, wait_for  # noqa: E402

PLAYERS = 60
SEATS = 42


def presence(bot, seat=None, spot=None):
    body = json.dumps({'peer': bot.peer, 'by': bot.uid, 'presence': {
        'uid': bot.uid, 'nick': bot.name, 'seat': seat, 'spot': spot, 'status': 'work',
    }}).encode()
    request = urllib.request.Request(bot.base_url + '/api/room/presence', data=body, method='POST',
                                     headers={'Content-Type': 'application/json'})
    try:
        with bot.opener.open(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'{}')


def post(bot, path, payload, headers=None):
    request = urllib.request.Request(bot.base_url + path, data=json.dumps(payload).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with bot.opener.open(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'{}')


def seat_holders():
    with server.lock:
        holders = {}
        for peer, item in server.peers.items():
            seat = (item.get('presence') or {}).get('seat')
            if seat:
                holders.setdefault(seat, []).append(peer)
        return holders


def main():
    with tempfile.TemporaryDirectory(prefix='ikigai-full-') as temporary:
        server.DB_FILE = str(Path(temporary) / 'db.json')
        server.BLOBS = str(Path(temporary) / 'blobs')
        Path(server.BLOBS).mkdir()
        server.db = {'docs': {}, 'accounts': {}}
        for table in (server.users, server.peers, server.clients, server.peer_sessions,
                      server.sessions, server.auth_failures, server.emit_log):
            table.clear()
        server.super_admin_uid = None
        server.save_db()
        httpd = server.OfficeServer(('127.0.0.1', 0), server.Handler)
        httpd.daemon_threads = True
        base = f'http://127.0.0.1:{httpd.server_port}'
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        bots = []
        try:
            # --- 60 players online at once -------------------------------------------------
            bots = [Bot(i, base, 'full') for i in range(1, PLAYERS + 1)]
            with ThreadPoolExecutor(max_workers=20) as pool:
                list(pool.map(lambda b: b.start((0.0, 0.0)), bots))
            everyone = {b.uid for b in bots}
            assert wait_for(lambda: all(b.visible_uids >= everyone for b in bots), 10), 'presence did not converge'
            print(f'OK  {PLAYERS} players online, every client sees all {PLAYERS}')

            # --- everyone rushes for the seats; pairs race for the same seat --------------
            wanted = {b.uid: f'seat-{b.index % SEATS}' for b in bots}  # 18 seats contested by 2 players
            barrier = threading.Barrier(PLAYERS)

            def sit(bot):
                barrier.wait()
                return bot, presence(bot, seat=wanted[bot.uid])
            with ThreadPoolExecutor(max_workers=PLAYERS) as pool:
                results = list(pool.map(sit, bots))
            holders = seat_holders()
            assert all(len(v) == 1 for v in holders.values()), f'seat held twice: {holders}'
            assert len(holders) == SEATS, f'expected all {SEATS} seats filled, got {len(holders)}'
            losers = [b for b, (status, body) in results if body.get('seatTaken')]
            assert len(losers) == PLAYERS - SEATS, len(losers)
            print(f'OK  seat race: {SEATS} seats filled exactly once, {len(losers)} late players told the seat was taken')

            # Losers stand instead (no seat) and still appear online for everyone.
            for bot in losers:
                assert presence(bot, spot=[1.0, 2.0])[0] == 200
            assert wait_for(lambda: all(len(b.visible_uids) == PLAYERS for b in bots), 5)

            # --- live chat ---------------------------------------------------------------
            sender, target = bots[0], bots[1]
            assert post(sender, '/api/room/emit', {'peer': sender.peer, 'by': sender.uid, 'topic': 'chat',
                                                   'data': {'text': 'chao ca nha', 'ch': 'all'}})[0] == 200
            assert wait_for(lambda: all('chao ca nha' in b.snapshot()[1] for b in bots), 5), 'broadcast lost'
            assert post(sender, '/api/room/emit', {'peer': sender.peer, 'by': sender.uid, 'topic': 'chat',
                                                   'data': {'text': 'tin rieng', 'ch': 'dm', 'to': target.uid}})[0] == 200
            time.sleep(0.5)
            got_dm = [b.index for b in bots if 'tin rieng' in b.snapshot()[1]]
            assert got_dm == [1, 2], got_dm
            long_text = 'x' * 5000
            post(bots[2], '/api/room/emit', {'peer': bots[2].peer, 'by': bots[2].uid, 'topic': 'chat',
                                             'data': {'text': long_text, 'ch': 'all'}})
            assert wait_for(lambda: ('x' * server.CHAT_TEXT_MAX) in bots[3].snapshot()[1], 5), 'long text not capped'
            assert post(bots[2], '/api/room/emit', {'peer': bots[2].peer, 'by': bots[2].uid,
                                                    'topic': 'evil', 'data': {}})[0] == 400
            print('OK  chat: broadcast reaches all 60, DM only sender+recipient, long text capped, unknown topic rejected')

            # 20 people chatting at the same moment: nothing lost.
            talkers = bots[10:30]
            with ThreadPoolExecutor(max_workers=20) as pool:
                list(pool.map(lambda b: post(b, '/api/room/emit', {
                    'peer': b.peer, 'by': b.uid, 'topic': 'chat', 'data': {'text': f'msg-{b.index}', 'ch': 'all'}}),
                    talkers))
            expected = {f'msg-{b.index}' for b in talkers}
            assert wait_for(lambda: all(expected <= b.snapshot()[1] for b in bots), 8), 'concurrent chat lost'
            print('OK  20 simultaneous messages delivered to all 60 players')

            # Flood guard: one account spamming is throttled, others unaffected.
            spam = [post(bots[5], '/api/room/emit', {'peer': bots[5].peer, 'by': bots[5].uid, 'topic': 'chat',
                                                     'data': {'text': f'spam{i}', 'ch': 'all'}})[0] for i in range(30)]
            assert spam.count(200) == server.EMIT_MAX and spam.count(429) == 30 - server.EMIT_MAX, spam
            assert post(bots[6], '/api/room/emit', {'peer': bots[6].peer, 'by': bots[6].uid, 'topic': 'chat',
                                                    'data': {'text': 'van gui duoc', 'ch': 'all'}})[0] == 200
            print(f'OK  flood guard: {server.EMIT_MAX} messages/{server.EMIT_WINDOW}s per account, others unaffected')

            # --- a seated player drops off: seat frees up, others see them leave ------------
            leaver = next(b for b, (status, body) in results if not body.get('seatTaken'))
            freed = wanted[leaver.uid]
            leaver.close()
            # closing the reader alone does not drop the TCP stream; the next keep-alive ping does
            with server.lock:
                q = server.clients.get(leaver.peer)
            q.put('{"type":"bye"}')
            assert wait_for(lambda: leaver.peer not in server.peers, 45), 'disconnected peer not removed'
            assert freed not in seat_holders()
            newcomer = next(b for b in losers)
            assert presence(newcomer, seat=freed)[1].get('seatTaken') is None
            assert seat_holders()[freed] == [newcomer.peer]
            print('OK  disconnect frees the seat; a standing player can take it')

            # Presence from a tab without a live stream is refused (no ghost seats).
            ghost = Bot(99, base, 'ghost')
            account = ghost.request('/api/auth/register', {'username': 'sim_ghost', 'password': 'password123',
                                                            'name': 'Ghost'})
            ghost.uid = account['uid']
            assert presence(ghost, seat='seat-ghost')[0] == 409
            assert 'seat-ghost' not in seat_holders()
            print('OK  presence without a live connection is rejected (no ghost seats)')

            # --- connection cap ----------------------------------------------------------
            old_cap = server.MAX_SSE_CLIENTS
            server.MAX_SSE_CLIENTS = len(server.clients)
            try:
                over = Bot(98, base, 'over')
                try:
                    over.start((0, 0))
                    raise AssertionError('connection over the cap was accepted')
                except RuntimeError as error:
                    assert '503' in str(error), error
            finally:
                server.MAX_SSE_CLIENTS = old_cap
            print('OK  over the connection cap the server answers 503 instead of hanging')

            # --- login rate limit is per real client IP behind a proxy ---------------------
            server.TRUST_PROXY = True
            try:
                for _ in range(server.AUTH_MAX_FAILURES):
                    post(ghost, '/api/auth/login', {'username': 'sim_ghost', 'password': 'wrong-pass'},
                         {'X-Forwarded-For': '203.0.113.7'})
                assert post(ghost, '/api/auth/login', {'username': 'sim_ghost', 'password': 'wrong-pass'},
                            {'X-Forwarded-For': '203.0.113.7'})[0] == 429
                assert post(ghost, '/api/auth/login', {'username': 'sim_ghost', 'password': 'password123'},
                            {'X-Forwarded-For': '198.51.100.9'})[0] == 200
            finally:
                server.TRUST_PROXY = False
            print('OK  a user locked out for wrong passwords does not lock out everyone else')

            # --- logins survive a server restart ------------------------------------------
            token_count = len(server.sessions)
            server.load_db()
            assert len(server.sessions) == token_count and token_count > 0
            assert post(bots[7], '/api/room/emit', {'peer': bots[7].peer, 'by': bots[7].uid, 'topic': 'chat',
                                                    'data': {'text': 'sau restart', 'ch': 'all'}})[0] == 200
            raw = Path(server.sessions_file()).read_text()
            assert all(b.password not in raw for b in bots[:3])
            print('OK  sessions persist across restarts (only token hashes stored)')
        finally:
            for bot in bots:
                bot.close()
            server.broadcast({'type': 'stop'})
            httpd.shutdown()
            httpd.server_close()
    print('PASS: full office, seat races, live chat, disconnects, caps and login limits')


if __name__ == '__main__':
    main()
