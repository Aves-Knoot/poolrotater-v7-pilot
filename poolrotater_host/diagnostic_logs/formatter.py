"""Source-side sanitization while protected credentials are accessible.

The independent network unit does not need access to OAuth or Discord secrets.
Install this on a dedicated bounded rotating JSONL sink in provider/workers.
The context provider must determine room visibility at emission time, not later
from the current public room roster. Raw ordinary logs remain local.
"""
import json
import logging

from .redact import scrub


class SanitizedJSONFormatter(logging.Formatter):
    def __init__(self, *, secret_provider=lambda: (), context_provider=lambda record: {}, version='unknown'):
        super().__init__('%(asctime)s [%(threadName)s] %(name)s %(levelname)s %(message)s')
        self.secret_provider = secret_provider
        self.context_provider = context_provider
        self.version = version

    def format(self, record):
        try:
            context = self.context_provider(record)
            kind = context.get('kind', 'operational')
            if kind not in ('operational', 'chat', 'command'): kind = 'operational'
            line = super().format(record)
            value = {'at': record.created, 'room': context.get('room'),
                     'version': context.get('version', self.version), 'kind': kind,
                     'private': context.get('private', False) is not False,
                     'text': scrub(line, self.secret_provider())}
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        except Exception:
            # Do not include exception text: secret-loader failures can contain
            # credentials. A safe operational marker makes the gap visible.
            return json.dumps({'at': record.created, 'room': None, 'version': self.version,
                               'kind': 'operational', 'private': False,
                               'text': '[diagnostic log omitted: source sanitization unavailable]'})
