"""Execute durable operations through the existing native room adapter.

This coordinator never invents successful provider outcomes. Applying intents
survive crashes as uncertain and are reconciled before any new mutation.
"""
import logging
from ..network.contracts import NetworkError
from .contracts import ERRORS
from .projection import export

log = logging.getLogger(__name__)

class Deferred(Exception):
    """The native adapter proves no requested provider mutation was dispatched."""
class BeforeMutationError(Exception):
    def __init__(self,code):
        if code not in ERRORS:raise ValueError('invalid_managed_error')
        self.code=code

class Engine:
    def __init__(self,state,adapter):self.state=state;self.adapter=adapter
    def step(self):
        for value in self.state.pending():
            id=value['id'];action=value['action']
            if action=='inspect':
                result=self.state.begin(id,ready=True)
                if result['phase']=='applying':
                    result=self.state.finish(id,'applied',result={'application':'inspect','revision':self.state.room(value['alias'])['revision']})
                return result
            if action=='cancel':
                result=self.state.begin(id,ready=True)
                if result['phase']=='applying':
                    try:result=self.state.cancel_target(id)
                    except NetworkError:result=self.state.finish(id,'refused',error='managed_application_requires_review')
                    export(self.state)
                return result
            try:
                self.adapter.preflight(value)
                ready=self.adapter.ready(value)
            except BeforeMutationError as exc:
                result=self.state.reject_before_mutation(id,exc.code);export(self.state);return result
            if ready and hasattr(self.adapter,'admit'):ready=self.adapter.admit(value)
            result=self.state.begin(id,ready=ready)
            if result['phase']=='waiting_boundary':continue
            if result['phase']!='applying':export(self.state);return result
            try:
                export(self.state)
                native=self.adapter.apply(value)
                result=self.state.finish(id,'applied',result=native)
            except Deferred:
                self.state.defer(id);result=self.state.operation(id)
            except BeforeMutationError as exc:
                result=self.state.reject_before_mutation(id,exc.code);export(self.state)
            except Exception as error:
                # No exception text/provider body/configuration enters receipts.
                log.warning('managed operation failed: id=%s alias=%s exception_type=%s',
                            id,value['alias'],type(error).__name__)
                result=self.state.finish(id,'uncertain',error='provider_outcome_uncertain')
            return result
        return None
