"""Register a qualified artifact without changing any signed channel target.

Used by the separately sealed root release administrator. This library does not
discover signer custody, initialize keys, contact a provider or manage services.
"""
from pathlib import Path


def register(publisher, bundle, sha256, *, before_bundle, before_sha256, expected_channels):
    from poolrotater_host.layout import file_lease
    from poolrotater_host.catalog_publication import _copy_target
    from poolrotater_host.runtime_package import inspect
    from poolrotater_host.updates.publisher import classify
    from poolrotater_host.updates.contracts import digest, fingerprint, require
    digest(sha256);digest(before_sha256)
    require(type(expected_channels)is dict,'update_expected_channels_required')
    with file_lease(publisher.custody/'publish.lock'):
        now=publisher._load()
        require(publisher.state['pending']is None,'update_publication_requires_resume')
        require(publisher.state['channels']==expected_channels,'update_channel_targets_changed')
        before=inspect(before_bundle,before_sha256,execution='pilot-live')
        after=inspect(bundle,sha256,execution='pilot-live')
        kind=classify(before,after)
        existing=next((r for r in publisher.state['releases'] if r['artifact']['sha256']==sha256),None)
        item={'sequence':existing['sequence'] if existing else publisher.state['release_sequence']+1,
              'version':after['version'],'kind':kind,
              'artifact':{'sha256':sha256,'bytes':Path(bundle).stat().st_size},
              'from_sha256':[before_sha256],'manifest_sha256':fingerprint(after),
              'execution':'pilot-live','rollback_safe':kind=='worker'}
        if existing is not None:
            require(existing==item,'update_registered_descriptor_changed')
            return {'ok':True,'already_registered':True,'sequence':publisher.state['sequence'],
                    'channels':dict(expected_channels),'channel_targets_unchanged':True}
        _copy_target(publisher.served/'targets'/(sha256+'.runtime.zip'),bundle,item['artifact'],mode=0o644)
        result=publisher._commit(now,channels=dict(expected_channels),
            releases=[*publisher.state['releases'],item],release_sequence=item['sequence'])
        require(result['channels']==expected_channels,'update_channel_targets_changed')
        return {**result,'registered_version':after['version'],'channel_targets_unchanged':True}
