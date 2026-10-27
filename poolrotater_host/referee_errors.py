"""Small, non-authoritative diagnostics for authenticated referee completions.

A server completion error confirms a rejected reply, not absence of a side
effect. Never use these diagnostics to grant ownership or replay a mutation.
"""
import re


def safe_completion(value):
    from .referee_protocol import message
    kind = message(value)
    if kind != 3 or 'error' not in value:
        return value
    # The server's RefereeHubException format is "Error <integer>: <text>".
    # Keep only that bounded number. Drop all original fields other than the
    # validated invocation identity; an upstream extra field cannot inject it.
    match = re.match(r'\AError ([1-9][0-9]{0,5}):(?: |$)', value['error'])
    result = {'type': 3, 'invocationId': value['invocationId'],
              'error': 'referee_command_rejected'}
    if match is not None:
        result['rejection_code'] = int(match[1])
    return result


class RefereeCommandRejected(RuntimeError):
    """Completed rejection with unknown mutation outcome; contains no raw text."""
    def __init__(self, code=None):
        self.rejection_code = code if type(code) is int and 0 < code < 10**6 else None
        super().__init__('referee_command_rejected' + (
            ' [code %d]' % self.rejection_code if self.rejection_code is not None else ''))


def rejection_details(error):
    """Also recognize the exact safe text after native worker RPC transport."""
    if type(getattr(error, 'args', None)) is not tuple or len(error.args) != 1:
        return None
    text = error.args[0]
    if type(text) is not str:
        return None
    match = re.fullmatch(r'referee_command_rejected(?: \[code ([1-9][0-9]{0,5})\])?', text)
    if match is None:
        return None
    result = {'failure_code': 'referee_command_rejected',
              'referee_completion': 'rejected', 'mutation_outcome': 'unknown'}
    if match[1] is not None:
        result['referee_rejection_code'] = int(match[1])
    return result
