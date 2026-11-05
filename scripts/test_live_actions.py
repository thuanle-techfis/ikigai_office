#!/usr/bin/env python3
"""Regression checks for rapid chairman/visitor start-stop actions."""

import http.cookiejar
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402


def action(feature, verb, **data):
    with server.lock:
        return server.apply_live_action({'feature': feature, 'action': verb, **data})


def expire(path):
    with server.lock:
        state = server.live_state(path)
        state['cooldownUntil'] = 0
        server.write_live_state(path, state)
        server.save_db()


def main():
    with tempfile.TemporaryDirectory(prefix='ikigai-live-actions-') as temporary:
        server.DB_FILE = str(Path(temporary) / 'db.json')
        server.BLOBS = str(Path(temporary) / 'blobs')
        Path(server.BLOBS).mkdir()
        server.db = {'docs': {}, 'accounts': {}}
        server.save_db()

        # Concurrent duplicate starts create exactly one chairman session.
        results = []
        threads = [threading.Thread(target=lambda: results.append(action(
            'chairman', 'start', track='chutich03', by='admin'
        ))) for _ in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        sessions = {result[0]['state']['sessionId'] for result in results}
        assert len(sessions) == 1
        chair_session = sessions.pop()
        assert server.live_state('live/chairman')['active'] is True

        # Ready is idempotent and a delayed ready cannot revive a stopped run.
        first_ready = action('chairman', 'ready', sessionId=chair_session)
        second_ready = action('chairman', 'ready', sessionId=chair_session)
        assert first_ready[0]['state']['audioAt'] > 0
        assert second_ready[0]['noop'] is True
        first_stop = action('chairman', 'stop', sessionId=chair_session)
        cooldown = first_stop[0]['state']['cooldownUntil']
        for _ in range(20):
            repeated = action('chairman', 'stop', sessionId=chair_session)
            assert repeated[0]['noop'] is True
            assert repeated[0]['state']['cooldownUntil'] == cooldown
        assert action('chairman', 'start', track='chutich')[1] == 409
        delayed_ready = action('chairman', 'ready', sessionId=chair_session)
        assert delayed_ready[0]['noop'] is True

        # A visit cannot overlap a chairman transition.
        assert action('visit', 'start')[1] == 409
        expire('live/chairman')
        visit_start = action('visit', 'start')[0]['state']
        visit_session = visit_start['sessionId']
        assert action('chairman', 'start', track='atay')[1] == 409

        # Completing/stopping an old visit cannot stop a newer visit.
        action('visit', 'stop', sessionId=visit_session)
        expire('live/visit')
        visit_session_2 = action('visit', 'start')[0]['state']['sessionId']
        stale_complete = action('visit', 'complete', sessionId=visit_session)
        assert stale_complete[0]['stale'] is True
        current_visit = server.live_state('live/visit')
        assert current_visit['active'] is True
        assert current_visit['sessionId'] == visit_session_2

        # Automatic calls without a session can never close the current run.
        assert action('visit', 'complete')[0]['stale'] is True
        assert server.live_state('live/visit')['active'] is True

        # Rapid start -> stop -> start remains stopped until cleanup finishes.
        action('visit', 'stop', sessionId=visit_session_2)
        blocked_restart = action('visit', 'start')
        assert blocked_restart[1] == 409
        assert server.live_state('live/visit')['active'] is False

        # Every response carries the doc version so clients can drop out-of-order replies.
        expire('live/visit')
        started = action('visit', 'start')[0]
        assert started['version'] == server.live_version('live/visit')
        action('visit', 'stop', sessionId=started['state']['sessionId'])
        expire('live/visit')

        http_checks()

    print('PASS: live action spam, stale requests, cooldowns, mutual exclusion and HTTP guards')


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b'{}')


def http_checks():
    os.environ.pop('IKIGAI_SUPER_ADMIN_UID', None)
    httpd = server.OfficeServer(('127.0.0.1', 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{httpd.server_address[1]}'
    try:
        admin, guest = Client(base), Client(base)
        assert admin.call('POST', '/api/auth/register', {
            'username': 'boss', 'password': 'password123', 'name': 'Boss'})[0] == 200
        assert guest.call('POST', '/api/auth/register', {
            'username': 'staff', 'password': 'password123', 'name': 'Staff'})[0] == 200

        # Non-admins cannot drive live actions; nobody can bypass the state machine.
        assert guest.call('POST', '/api/live/action', {'feature': 'visit', 'action': 'start'})[0] == 403
        for path in ('live/chairman', 'live/visit'):
            status, body = admin.call('POST', '/api/db/doc?path=' + path, {'data': {'active': True}})
            assert (status, body.get('error')) == (403, 'use_live_action'), (status, body)

        # 40 concurrent mixed start/stop clicks from the admin: exactly one session ever exists,
        # and the stored state always matches the last successful transition.
        def click(i):
            verb = 'start' if i % 2 == 0 else 'stop'
            return admin.call('POST', '/api/live/action',
                              {'feature': 'chairman', 'action': verb, 'track': 'atay'})
        with ThreadPoolExecutor(max_workers=16) as pool:
            replies = list(pool.map(click, range(40)))
        assert all(status in (200, 409) for status, _ in replies), replies
        sessions = {body['state']['sessionId'] for status, body in replies
                    if status == 200 and body['state'].get('active')}
        assert len(sessions) == 1, sessions
        status, stored = admin.call('GET', '/api/db/doc?path=live/chairman')
        assert status == 200 and stored['data']['sessionId'] in sessions
        # Once stopped, the meeting stays stopped through the cooldown despite further start spam.
        if not stored['data']['active']:
            assert stored['data']['cooldownUntil'] > 0
            assert click(0)[0] == 409
        versions = [body['version'] for status, body in replies if status == 200]
        assert max(versions) == stored['version']

        # While the chairman is active or cooling down, a visit cannot start (and vice versa).
        assert admin.call('POST', '/api/live/action', {'feature': 'visit', 'action': 'start'})[0] == 409
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == '__main__':
    main()
