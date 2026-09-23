#!/usr/bin/env python3
"""Replays real API answers for the package's tests.

The files in tests/fixtures/ were recorded from a running Sondavi instance over
real HTTP, so this package is tested against shapes the API actually produces
rather than against invented ones.

Adds only the two behaviours a static file cannot express: the bearer check, and
a 429 with Retry-After — which is the one thing a hand-written client gets wrong.

    python3 tests/fixture-server.py [port]
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
TOKEN = 'sdv_' + 'T' * 48
state = {'flaky_left': 2}


def load(name):
    with open(os.path.join(HERE, name), encoding='utf-8') as f:
        return json.load(f)


# Read from the recording rather than written down here: the id changes every time
# `clients/fixtures-aufzeichnen.sh` runs, and a hard-coded one turns every re-recording
# into a silent 404 that looks like a client bug.
SURVEYS = [str(s['id']) for s in load('surveys.json')['data']]
SURVEY_ID = SURVEYS[0]

# Which recording answers for which study. Positional rather than by id, because the ids
# change with every re-recording — the same reason SURVEY_ID is read rather than written down.
STUDY_FILES = {
    SURVEYS[0]: ('codebook.json', 'responses_page1.json'),
    SURVEYS[1]: ('codebook_wave2.json', 'responses_wave2.json'),
}

# Two further snapshot ids, each standing for a state the same recorded set can be in.
# A SECOND id rather than a query parameter: a client builds the path from the id alone,
# so anything else would only be reachable by a test that misuses the client - and such a
# test passes for the wrong reasons.
ERASED_ID = '00000000-0000-4000-8000-000000000001'    # a response was deleted afterwards
NARROWED_ID = '00000000-0000-4000-8000-000000000002'  # read by a token with fewer abilities
SNAPSHOT_FILES = {
    ERASED_ID: 'snapshot_responses_after_erasure.json',
    NARROWED_ID: 'snapshot_responses_narrowed.json',
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        return self.headers.get('Authorization') == f'Bearer {TOKEN}'

    def _split(self):
        path, _, qs = self.path.partition('?')
        query = dict(p.split('=', 1) for p in qs.split('&') if '=' in p)
        return path, query

    def do_POST(self):
        if not self._auth_ok():
            return self.send_json({'message': 'Invalid or expired token.'}, 401)

        path, _ = self._split()
        if re.match(rf'^/api/v1/surveys/{SURVEY_ID}/snapshots$', path):
            return self.send_json(load('snapshot.json'), 201)
        return self.send_json({'message': 'Not found.'}, 404)

    def do_GET(self):
        if not self._auth_ok():
            return self.send_json({'message': 'Invalid or expired token.'}, 401)

        path, query = self._split()

        # `?flaky=1` answers 429 twice, then normally.
        if 'flaky' in query and state['flaky_left'] > 0:
            state['flaky_left'] -= 1
            return self.send_json({'message': 'Too many requests.'}, 429, {'Retry-After': '1'})

        if path == '/api/v1/surveys':
            return self.send_json(load('surveys.json'))

        if path == '/api/v1/snapshots':
            return self.send_json(load('snapshots.json'))

        m = re.match(r'^/api/v1/snapshots/([0-9a-f-]+)/responses$', path)
        if m:
            return self.send_json(load(SNAPSHOT_FILES.get(m.group(1), 'snapshot_responses.json')))

        m = re.match(r'^/api/v1/snapshots/([0-9a-f-]+)$', path)
        if m:
            return self.send_json(load('snapshot.json'))

        m = re.match(r'^/api/v1/surveys/([0-9]+)/codebook$', path)
        if m and m.group(1) in STUDY_FILES:
            return self.send_json(load(STUDY_FILES[m.group(1)][0]))

        m = re.match(r'^/api/v1/surveys/([0-9]+)/responses$', path)
        if m and m.group(1) in STUDY_FILES:
            page = load(STUDY_FILES[m.group(1)][1])
            # Only the first study was recorded page by page; the cursor branch replays its
            # second page, which is what exercises the client's paging.
            if 'cursor' in query and m.group(1) == SURVEY_ID:
                page = {'data': page['data'][:1],
                        'meta': dict(page['meta'], count=1, remaining=0, next_cursor=None)}
                page['data'][0] = dict(page['data'][0], response_id=999, respondent_id='PNL-3')
                # The recorded PNL-3 skipped the image marking questions; a copy of PNL-1's
                # markings would count someone else's cells twice.
                for skipped in ('map', 'visits'):
                    page['data'][0].pop(skipped, None)
            elif 'cursor' in query:
                page = {'data': [], 'meta': dict(page['meta'], count=0, remaining=0, next_cursor=None)}
            return self.send_json(page)

        return self.send_json({'message': 'Not found.'}, 404)


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    HTTPServer(('127.0.0.1', port), Handler).serve_forever()
