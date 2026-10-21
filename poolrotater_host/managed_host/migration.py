"""Resumable local participation migration, independent of room/provider locks."""
import os
from pathlib import Path
from ..layout import atomic_json,file_lease,read_json
from ..network.contracts import fields,fingerprint,require


def profile_plan(client):
    """Validate enrolled binding and derive the fixed mandatory profiles.

    No credentials, provider requests or configuration writes are returned.
    Used by stopped root preparation to pin changes before publication.
    """
    from ..ratings.sync import Synchronizer
    from ..ratings.migration import local_status
    from .. import reporting
    require(local_status(client.root)['capture_enabled'],'ratings_migration_required')
    profile=client.profile();credential=client.credential();marker=client.marker
    require(credential is not None and client.pending(profile) is None,'network_enrollment_required')
    client.check_account(profile['account_id'])
    desired={**profile,'directory':True}
    previous_path=Path(client.root)/'state/managed-participation-migration.json'
    previous=read_json(previous_path) if os.path.lexists(previous_path) else None
    accepted={fingerprint(profile),fingerprint(desired)}
    binding={k:credential[k] for k in ('host_id','installation_id','generation','server_epoch')}
    if previous is not None:
        fields(previous,('format',*binding,'previous_profile','desired_profile'))
        require(type(previous['format']) is int and previous['format']==1
                and all(previous[k]==v for k,v in binding.items())
                and fingerprint(profile) in (previous['previous_profile'],previous['desired_profile'])
                and fingerprint(desired)==previous['desired_profile'],'managed_policy_migration_requires_review')
        accepted.update((previous['previous_profile'],previous['desired_profile']))
    sync=Synchronizer(client.root,network=client)
    if os.path.lexists(sync.profile_path):
        old=sync.profile()
        require(all(old[k]==credential[k] for k in ('host_id','server_epoch'))
                and old['network_profile_sha256'] in accepted,'ratings_restored_binding_requires_review')
    from ..ratings.reader import NETWORK_CONSUMPTION_QUALIFIED
    require(NETWORK_CONSUMPTION_QUALIFIED,'ratings_shadow_qualification_required')
    identity={k:marker[k] for k in ('installation_id','generation')}
    return {'config/network.json':desired,
            'config/ratings.json':{'format':1,**identity,'share_ratings':True,
                'network_profile_sha256':fingerprint(desired),
                **{k:credential[k] for k in ('host_id','server_epoch')}},
            'config/reporting.json':{'format':1,**identity,**dict.fromkeys(reporting.CHOICES,True)},
            'config/ratings-reader.json':{'format':1,**identity,'mode':'network','server_epoch':credential['server_epoch']},
            'config/discord-relay.json':{'format':1,'enabled':True}}

def migrate(client,store,*,export_projection=True):
    root=Path(client.root);marker=client.marker
    from .policy import enabled
    require(enabled(root),'managed_policy_required')
    proof=read_json(root/'state/managed-root-policy.json') if (root/'state/managed-root-policy.json').exists() else None
    require(proof is not None,'managed_root_policy_required')
    fields(proof,('format','policy_version','installation_id','generation','automatic_updates','administration_helper'))
    require(type(proof['format']) is int and proof['format']==1 and type(proof['policy_version']) is int
            and proof['policy_version']==1 and all(proof[k]==marker[k] for k in ('installation_id','generation'))
            and proof['automatic_updates'] is True and proof['administration_helper'] is True,'managed_root_policy_required')
    with file_lease(root/'state/managed-policy-migration.lock'):
        if store.snapshot()['policy_version']==1:return
        from ..ratings.sync import Synchronizer
        from ..ratings.migration import local_status
        require(local_status(root)['capture_enabled'],'ratings_migration_required')
        sync=Synchronizer(root,network=client)
        path=root/'state/managed-participation-migration.json'
        with file_lease(sync.lock_path),file_lease(client.lock_path):
            profile=client.profile();credential=client.credential()
            require(credential is not None and client.pending(profile) is None,'network_enrollment_required')
            client.check_account(profile['account_id'])
            desired={**profile,'directory':True}
            previous=read_json(path) if os.path.lexists(path) else None
            binding={k:credential[k] for k in ('host_id','installation_id','generation','server_epoch')}
            if previous is None:
                previous={'format':1,**binding,'previous_profile':fingerprint(profile),'desired_profile':fingerprint(desired)}
                atomic_json(path,previous)
            fields(previous,('format',*binding,'previous_profile','desired_profile'))
            require(type(previous['format']) is int and previous['format']==1
                    and all(previous[k]==v for k,v in binding.items())
                    and fingerprint(profile) in (previous['previous_profile'],previous['desired_profile'])
                    and fingerprint(desired)==previous['desired_profile'],'managed_policy_migration_requires_review')
            if os.path.lexists(sync.profile_path):
                old=sync.profile()
                require(all(old[k]==credential[k] for k in ('host_id','server_epoch'))
                        and old['network_profile_sha256'] in (previous['previous_profile'],previous['desired_profile']),
                        'ratings_restored_binding_requires_review')
            value={'format':1,**{k:marker[k] for k in ('installation_id','generation')},
                   'share_ratings':True,'network_profile_sha256':previous['desired_profile'],
                   **{k:credential[k] for k in ('host_id','server_epoch')}}
            # Durable old/new pins make a crash between these two documents
            # resumable, without accepting unrelated network/profile changes.
            atomic_json(client.profile_path,desired);atomic_json(sync.profile_path,value)
        from .. import reporting
        reporting.configure(root,dict.fromkeys(reporting.CHOICES,True))
        from ..ratings.reader import configure
        configure(sync,mode='network')
        atomic_json(root/'config/discord-relay.json',{'format':1,'enabled':True})
        from .policy import seed
        seed(root,store,export_projection=export_projection)
        store.policy_applied(1)
