"""Redact before persistence and again before central persistence.

Known current local values cover opaque provider formats. Format rules also
cover historical credentials no longer present in the current configuration.
No value, line, or exception originating in a log is interpreted as code.
"""
import re
from urllib.parse import quote, quote_plus

REDACTED = '[REDACTED]'
_KEY = r'(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|oauth[_-]?(?:code|token|secret)|authorization|proxy[_-]?authorization|discord[_-]?(?:token|webhook)|hub[_-]?(?:token|credential|secret)|credential|invitation|invite[_-]?(?:token|code)|password|passwd|api[_-]?key|secret)'
_ASSIGNMENT = re.compile(r'''(?ix)(["']?\b''' + _KEY + r'''\b["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}\]\r\n]+)''')
_AUTH = re.compile(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+')
_PREFIXED = re.compile(r'\b(?:prn_|pri_)[A-Za-z0-9_-]{20,}\b|\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b')
_JWT = re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b')
_DISCORD = re.compile(r'\b(?:mfa\.[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,})\b')
_WEBHOOK = re.compile(r'https?://(?:canary\.|ptb\.)?(?:discord(?:app)?\.com)/api(?:/v\d+)?/webhooks/\d+/[^\s"<>]+', re.I)
_URL_AUTH = re.compile(r'(https?://)[^\s/@]+@', re.I)
_URL_QUERY = re.compile(r'(?i)([?&](?:' + _KEY + r'|code|state|token|key|signature|sig|auth|ticket)=)[^&\s"<>]+')
_PRIVATE_DOWNLOAD = re.compile(r'(https?://[^\s/]+/(?:operator-downloads|invitations|enroll)/)[A-Za-z0-9_-]{16,}', re.I)
_PEM = re.compile(r'-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-\r\n]*PRIVATE KEY-----|\Z)')
_CONTROL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_COMMAND_SECRET = re.compile(r'(?im)(\.(?:password|passwd|pass|token|secret)\s+)[^\r\n]+')
_PROSE_SECRET = re.compile(r'(?i)(\b(?:password|client[_-]?secret|access[_-]?token|refresh[_-]?token)\s+(?:is|was|to)\s+)[^\s,;]+')


def scrub(value, secrets=()):
    """Return printable text. Never report the secret or original on failure."""
    if not isinstance(value, str):
        value = str(value)
    # Redact exact known values and their common URL-encoded forms first.
    candidates = set()
    for secret in secrets:
        if isinstance(secret, str) and secret:
            candidates.update((secret, quote(secret, safe=''), quote_plus(secret)))
    for secret in sorted(candidates, key=len, reverse=True):
        value = value.replace(secret, REDACTED)
    value = _PEM.sub(REDACTED, value)
    value = _WEBHOOK.sub(REDACTED, value)
    value = _PRIVATE_DOWNLOAD.sub(lambda m: m[1] + REDACTED, value)
    value = _URL_AUTH.sub(lambda m: m[1] + REDACTED + '@', value)
    value = _URL_QUERY.sub(lambda m: m[1] + REDACTED, value)
    value = _COMMAND_SECRET.sub(lambda m: m[1] + REDACTED, value)
    value = _PROSE_SECRET.sub(lambda m: m[1] + REDACTED, value)
    value = _AUTH.sub(lambda m: m[1] + ' ' + REDACTED, value)
    value = _ASSIGNMENT.sub(lambda m: m[1] + REDACTED, value)
    value = _PREFIXED.sub(REDACTED, value)
    value = _JWT.sub(REDACTED, value)
    value = _DISCORD.sub(REDACTED, value)
    # Terminal escapes cannot turn a private search result into instructions.
    return _CONTROL.sub('', value)


def secrets_from_mapping(value):
    """Extract only credential fields from already-authorized local documents.

    Does not open files, traverse arbitrary paths, or include ordinary IDs.
    """
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and re.fullmatch(_KEY + r'|token|webhook_url|room_password', key, re.I):
                if isinstance(item, str) and item:
                    result.append(item)
            elif isinstance(item, (dict, list)):
                result.extend(secrets_from_mapping(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(secrets_from_mapping(item))
    return result
