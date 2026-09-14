#!/usr/bin/env python3
"""Runtime student adapter for the scope-stamper, with default-closed output."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
import importlib.machinery

BIN = Path(__file__).resolve().parent
LIB = importlib.machinery.SourceFileLoader('mem0_scope_lib_student', str(BIN / 'mem0_scope_lib.py')).load_module()

STUDENT_URL = os.environ.get('STUDENT_URL') or os.environ.get('BORG_SCOPE_STUDENT_URL', '')
STUDENT_MODEL = os.environ.get('STUDENT_MODEL', str(LIB.CONFIG.values['BORG_GRAPH_MODEL']))
_THINK_RE = re.compile(r'^\s*<think>.*?</think>\s*', re.S | re.I)


def parse_student_response(raw: str) -> dict | None:
    """Accept only the one-object array emitted by the v2 training contract."""
    text = _THINK_RE.sub('', raw or '', count=1).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return None
    obj = value[0]
    if set(obj) != {'i', 'c', 's', 'p'}:
        return None
    if type(obj['i']) is not int or obj['i'] != 0:
        return None
    if not isinstance(obj['c'], str) or not isinstance(obj['s'], str):
        return None
    if type(obj['p']) is not int or obj['p'] not in (0, 1):
        return None
    return {'i': 0, 'c': obj['c'].strip().lower(), 's': obj['s'].strip().lower(), 'p': obj['p']}


class StudentClassifier:
    """Drop-in classifier facade backed by an OpenAI-compatible mlx_lm server."""

    def __init__(self, endpoints=None, model=STUDENT_MODEL, log=None, origin_default=False,
                 timeout=300):
        if not endpoints and not STUDENT_URL:
            raise ValueError('BORG_SCOPE_STUDENT_URL is required when no endpoint is supplied')
        self.url = (endpoints or [STUDENT_URL])[0].rstrip('/')
        self.endpoints = [self.url]  # backfill spawns one worker per endpoint (2026-09-05 dry-run fix)
        self.model = model
        self.log = log
        self.timeout = timeout
        self.origin_default = origin_default
        self.clients = LIB.load_clients()
        self.menu = LIB.client_menu(self.clients)

    def _say(self, message):
        if self.log:
            self.log(message)

    def _chat(self, host: str, prompt: str) -> str:
        body = json.dumps({
            'model': self.model,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0,
            'max_tokens': 64,
        }).encode()
        request = urllib.request.Request(
            host.rstrip('/') + '/v1/chat/completions', data=body,
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.load(response)
        return payload['choices'][0]['message']['content']

    def classify_batch(self, batch: list[dict], host: str) -> list[dict]:
        # The backfill hands over --batch rows (20 nightly); the student serves one
        # note per HTTP request, so loop here (2026-09-05 dry-run fix; was a hard error).
        out: list[dict] = []
        for row in batch:
            out.extend(self._classify_one(row, host))
        return out

    def _classify_one(self, row: dict, host: str) -> list[dict]:
        prompt = LIB.build_prompt([{'data': row.get('data', '')}], self.menu, maxlen=300)
        label = None
        started = time.perf_counter()
        for attempt in range(2):
            try:
                label = parse_student_response(self._chat(host, prompt))
            except Exception as exc:
                self._say(f'student call failed attempt {attempt + 1}: {exc}')
                label = None
            if label is not None:
                break
        latency_ms = (time.perf_counter() - started) * 1000
        self._say(f'student call latency_ms={latency_ms:.2f}')
        if label is None:
            return [{
                'id': row['id'], 'cat': '', 'slug': '', 'sensitive': False,
                'scope': LIB.SCOPE_PERSONAL,
                'reason': 'student failed -> quarantined (runtime default-closed)',
                'quarantined': True,
            }]
        scope, reason = LIB.to_scope(label['c'], label['s'], bool(label['p']), self.clients,
                                     row.get('data', ''))
        cat = '' if label['c'] == 'unclassified' else label['c']
        return [{
            'id': row['id'], 'cat': cat, 'slug': label['s'],
            'sensitive': bool(label['p']) or reason.endswith('(regex floor)'),
            'scope': scope, 'reason': reason, 'quarantined': False,
        }]

    def run(self, rows: list[dict], batch_size: int, out_path: Path,
            done_ids: set[str] | None = None):
        """Append one durable result per not-yet-done row, preserving incumbent semantics."""
        del batch_size  # the runtime contract is deliberately one note per request
        done_ids = done_ids or set()
        todo = [row for row in rows if row['id'] not in done_ids]
        with open(out_path, 'a') as fh:
            for row in todo:
                result = self.classify_batch([row], self.url)
                for record in result:
                    fh.write(json.dumps(record, ensure_ascii=False) + '\n')
                fh.flush()
                os.fsync(fh.fileno())
                yield 1, len(todo)


if __name__ == '__main__':
    raise SystemExit('import StudentClassifier from this module')
