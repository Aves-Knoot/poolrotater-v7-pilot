"""Explicit root publishing commands; never an HTTP signing endpoint."""
from .contracts import require

ACTIONS=('init','publish','promote','renew','resume','status')


def command(action,*,custody,served=None,bundle=None,sha256=None,before_bundle=None,before_sha256=None,channel=None):
    import os
    require(os.geteuid()==0,'release_administrator_required')
    require(action in ACTIONS,'release_action_refused')
    from .publisher import Publisher,initialize
    if action=='init':
        require(served is not None,'release_served_directory_required')
        return initialize(custody,served)
    publisher=Publisher(custody)
    if action=='publish':
        require(bundle is not None and before_bundle is not None and sha256 is not None and before_sha256 is not None,
                'release_artifact_and_baseline_required')
        return publisher.publish(bundle,sha256,before_bundle=before_bundle,before_sha256=before_sha256,
                                 channel=channel or'pilot',execution='pilot-live')
    if action=='promote':return publisher.promote(sha256,channel=channel)
    return getattr(publisher,action)()
