"""Persisted contribution budget for prompt, bounded scheduled delivery.

100 new event identities per 30 minutes, with at most 100 in reserve: at most
4,900 new identities in any rolling day, below the hub's 5,000 host limit.
Retries reuse reservations after a lost response; restarts do not refill them.
This is a transport budget, never a change to event payloads or user policy.
"""
import math

from ..layout import contained, read_json, atomic_json
from ..network.contracts import fields, require, number, identifier


class DeliveryBudget:
    CAPACITY = 100
    INTERVAL = 1800.0

    def __init__(self, root, marker, now):
        self.path = contained(root, 'state/ratings-delivery-budget.json')
        self.binding = {k: marker[k] for k in ('installation_id', 'generation')}
        number(now)
        self.now = now

    def select(self, events):
        if not events:
            return events
        value = {'format': 1, **self.binding, 'updated_at': self.now,
                 'tokens': float(self.CAPACITY), 'reserved': []}
        if self.path.exists():
            value = read_json(self.path)
            fields(value, ('format', 'installation_id', 'generation', 'updated_at', 'tokens', 'reserved'))
            require(type(value['format']) is int and value['format'] == 1
                    and all(value[k] == v for k, v in self.binding.items()), 'ratings_delivery_binding_invalid')
            number(value['updated_at']); number(value['tokens'])
            require(0 <= value['tokens'] <= self.CAPACITY and type(value['reserved']) is list
                    and len(value['reserved']) <= self.CAPACITY, 'ratings_delivery_budget_invalid')
            for event_id in value['reserved']:
                identifier(event_id)
            require(len(set(value['reserved'])) == len(value['reserved']), 'ratings_delivery_budget_invalid')
        # A backwards wall clock neither refills credits nor moves the stored
        # boundary back, preventing repeated refills while the clock catches up.
        updated = max(self.now, value['updated_at'])
        tokens = min(self.CAPACITY, value['tokens'] +
                     (updated - value['updated_at']) * self.CAPACITY / self.INTERVAL)
        reserved = set(value['reserved'])
        selected = []
        for event in events:
            if event['event_id'] not in reserved:
                if math.floor(tokens + 1e-9) < 1:
                    break
                tokens = max(0.0, tokens - 1)
            selected.append(event)
        # Current oldest pending batch includes any retryable reservations.
        # Terminal/quarantined events never refund a consumed credit.
        atomic_json(self.path, {'format': 1, **self.binding, 'updated_at': updated,
                    'tokens': tokens, 'reserved': [e['event_id'] for e in selected]})
        return selected
