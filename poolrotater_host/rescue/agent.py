"""Durable recovery exchanges and at-most-once command dispatch."""
import time

from ..layout import atomic_json, file_lease, read_json
from ..network.contracts import fields, number, require
from .contracts import exchange, request, receipt, ERRORS


class Agent:
    def __init__(self, directory, binding, backend, transport, *, clock=time.time):
        self.directory = directory
        self.binding = binding
        self.backend = backend
        self.transport = transport
        self.clock = clock

    def step(self):
        with file_lease(self.directory / 'agent.lock'):
            path = self.directory / 'state.json'
            state = read_json(path) if path.exists() else {
                'format': 1, 'binding': self.binding, 'sequence': 1,
                'pending': None, 'receipt': None, 'commands': {}}
            fields(state, ('format', 'binding', 'sequence', 'pending', 'receipt', 'commands'))
            require(state['format'] == 1 and state['binding'] == self.binding, 'rescue_binding_changed')
            require(type(state['commands']) is dict and len(state['commands']) <= 256, 'rescue_state_refused')
            now = self.clock()
            # A crash after dispatch is an uncertain outcome, never permission
            # to execute the same request again, even after a lost hub reply.
            for key, saved in state['commands'].items():
                if saved['receipt'] is None:
                    saved['receipt'] = receipt({'id': key, 'result': 'uncertain',
                                                'error': 'rescue_outcome_uncertain'})
                    state['receipt'] = saved['receipt']
            state['commands'] = {k: v for k, v in state['commands'].items()
                                 if v['expires_at'] + 3600 >= now}
            if state['pending'] is not None and now - state['pending']['sent_at'] > 240:
                # Discard only the diagnostic envelope, retaining its command
                # receipt. A new sequence cannot equivocate with the old one.
                state['sequence'] += 1
                state['pending'] = None
            if state['pending'] is None:
                state['pending'] = exchange({'protocol_version': 1,
                    **{k: self.binding[k] for k in ('installation_id', 'generation')},
                    'sequence': state['sequence'], 'sent_at': now,
                    'observation': self.backend.observe(), 'receipt': state['receipt']})
            atomic_json(path, state)
            try:
                reply = self.transport(state['pending'])
                fields(reply, ('ok', 'sequence', 'received_at', 'request'))
                require(reply['ok'] is True and reply['sequence'] == state['pending']['sequence'],
                        'rescue_state_refused')
                number(reply['received_at'])
                if reply['request'] is not None:
                    request(reply['request'])
            except Exception:
                return {'ok': False, 'phase': 'disconnected', 'error': 'rescue_transport_unavailable'}
            state.update(sequence=state['sequence'] + 1, pending=None)
            # Clear only the receipt actually acknowledged by this envelope.
            if state['receipt'] is not None and state['receipt'] == read_json(path)['pending']['receipt']:
                state['receipt'] = None
            atomic_json(path, state)
            command = reply['request']
            if command is None:
                return {'ok': True, 'phase': 'connected'}
            key = command['id']
            previous = state['commands'].get(key)
            if previous is not None:
                require(previous['request'] == command, 'rescue_request_changed')
                state['receipt'] = previous['receipt']
                atomic_json(path, state)
                return {'ok': True, 'phase': 'reported', 'replayed': True}
            require(len(state['commands']) < 256, 'rescue_attempt_limit')
            # Persist before *any* action, including inspect/collection.
            state['commands'][key] = {'request': command, 'expires_at': command['expires_at'], 'receipt': None}
            atomic_json(path, state)
            result = {'id': key, 'result': 'refused', 'error': 'rescue_update_requires_review'}
            try:
                require(all(command[k] == self.binding[k] for k in ('host_id', 'installation_id', 'generation')),
                        'rescue_binding_changed')
                require(self.clock() < command['expires_at'] <= self.clock() + 1800, 'rescue_request_expired')
                self.backend.perform(command)
                result.update(result='complete', error=None)
            except Exception as error:
                code = getattr(error, 'code', None)
                if code not in ERRORS or code is None:
                    code = str(error) if str(error) in ERRORS else 'rescue_update_requires_review'
                result['error'] = code
            state['receipt'] = receipt(result)
            state['commands'][key]['receipt'] = state['receipt']
            atomic_json(path, state)
            return {'ok': result['result'] == 'complete', 'phase': result['result'], 'error': result['error']}
