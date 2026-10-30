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
# A token without the ability to read identifiers: the files ZIP it gets has no respondent_id (SPECS §87.3).
TOKEN_PLAIN = 'sdv_' + 'P' * 48
state = {'flaky_left': 2, 'sim_step': 0, 'model_fail_left': 0}


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
    SURVEYS[2]: ('codebook_interview.json', 'responses_interview.json'),
    SURVEYS[3]: ('codebook_screening.json', 'responses_screening.json'),
    SURVEYS[4]: ('codebook_types.json', 'responses_types.json'),
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


# Simulations (SPECS §88): the recorded run of the interview question, step by step — two
# answers, then the summaries, then the choice — and the whole-survey interview's start.
SIM_START = load('simulation_start.json')
INTERVIEW_ID = str(SIM_START['data']['survey_id'])
SIM_ID = SIM_START['data']['id']
SIM_STEPS = ['simulation_step1.json', 'simulation_summary.json', 'simulation_done.json']
# A component name standing for "the day's turns are used up" — the recorded refusal, the
# same way the snapshot states have ids of their own.
EXHAUSTED = 'question:exhausted'
# A response that fails once at the model before it goes through (`model_failed`, nothing
# recorded), so the clients' sending-again is exercised.
FAIL_ONCE = 'FAIL ONCE'


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
        return self.headers.get('Authorization') in (f'Bearer {TOKEN}', f'Bearer {TOKEN_PLAIN}')

    def send_file(self, name):
        with open(os.path.join(HERE, name), 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _split(self):
        path, _, qs = self.path.partition('?')
        query = dict(p.split('=', 1) for p in qs.split('&') if '=' in p)
        return path, query

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        return json.loads(raw.decode() or '{}')

    def do_POST(self):
        if not self._auth_ok():
            return self.send_json({'message': 'Invalid or expired token.'}, 401)

        path, _ = self._split()
        body = self._body()
        if re.match(rf'^/api/v1/surveys/{SURVEY_ID}/snapshots$', path):
            return self.send_json(load('snapshot.json'), 201)

        if path == f'/api/v1/surveys/{INTERVIEW_ID}/simulations':
            state['sim_step'] = 0
            if body.get('component') == EXHAUSTED:
                return self.send_json(load('simulation_budget_exhausted.json'), 429, {'Retry-After': '3600'})
            if body.get('component') == 'survey-interview':
                return self.send_json(load('simulation_whole_start.json'), 201)
            if body.get('component') != 'question:day':
                return self.send_json({'message': 'The given data was invalid.', 'errors': {'component': ['No such component.']}}, 422)
            return self.send_json(SIM_START, 201)

        if path == f'/api/v1/simulations/{SIM_ID}/responses':
            if body.get('text') == FAIL_ONCE:
                state['model_fail_left'] += 1
                if state['model_fail_left'] == 1:
                    return self.send_json({'message': 'The language model did not answer. Nothing was recorded; send the same response again.', 'reason': 'model_failed'}, 502)
            if state['sim_step'] >= len(SIM_STEPS):
                return self.send_json({'message': 'This simulation has ended; start a new one.', 'reason': 'ended'}, 409)
            name = SIM_STEPS[state['sim_step']]
            state['sim_step'] += 1
            return self.send_json(load(name))

        return self.send_json({'message': 'Not found.'}, 404)

    def do_PATCH(self):
        if not self._auth_ok():
            return self.send_json({'message': 'Invalid or expired token.'}, 401)
        path, _ = self._split()
        state['last_patch'] = self._body()
        if path == f'/api/v1/simulations/{SIM_ID}':
            return self.send_json(load('simulation_attached.json'))
        return self.send_json({'message': 'Not found.'}, 404)

    def do_DELETE(self):
        if not self._auth_ok():
            return self.send_json({'message': 'Invalid or expired token.'}, 401)
        path, _ = self._split()
        if path == f'/api/v1/simulations/{SIM_ID}':
            return self.send_json({'deleted': True})
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

        if path == f'/api/v1/surveys/{INTERVIEW_ID}/components':
            return self.send_json(load('components.json'))
        if path == '/api/v1/simulations':
            return self.send_json(load('simulations.json'))
        if path == f'/api/v1/simulations/{SIM_ID}':
            return self.send_json(load('simulation.json'))
        # What the clients last attached, so a test can check what was sent.
        if path == '/__last_patch':
            return self.send_json(state.get('last_patch') or {})

        m = re.match(r'^/api/v1/snapshots/([0-9a-f-]+)/responses$', path)
        if m:
            return self.send_json(load(SNAPSHOT_FILES.get(m.group(1), 'snapshot_responses.json')))

        m = re.match(r'^/api/v1/snapshots/([0-9a-f-]+)$', path)
        if m:
            return self.send_json(load('snapshot.json'))

        # The uploaded files (SPECS §87.3), recorded for the first study.
        if path == f'/api/v1/surveys/{SURVEY_ID}/files':
            plain = self.headers.get('Authorization') == f'Bearer {TOKEN_PLAIN}'
            return self.send_file('files_plain.zip' if plain else 'files.zip')

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
                # PNL-3 is a person of the panel too (meta.panel_columns): their own code, not PNL-1's.
                for column, value in (('panel_patient_code', 'K-103'), ('panel_first_name', 'TEST Person 3'),
                                      ('panel_email', 'test.fixture3@example.org')):
                    if column in page['data'][0]:
                        page['data'][0][column] = value
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
