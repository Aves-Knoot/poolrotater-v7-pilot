"""Threshold Ed25519 release authority with durable rollback/freeze protection.

Trust roots are bootstrapped out of band. This initial protocol deliberately does
not accept a root replacement from the server: key rotation is a local reviewed
bootstrap operation. A catalog root never authorizes executable releases.
"""
from .contracts import (canonical, decode, digest, fields, fingerprint, index,
                        integer, require, root, timestamp)


def verify(raw, bootstrap, *, now, previous=None):
    # The manually pinned stopped bootstrap first imports approval dispatch
    # under system Python -I -S. Exact reviewed manual transitions do not need
    # release signatures. Signed verification executes only from the already
    # checked installed environment and must still load its pinned dependency.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    root(bootstrap); timestamp(now)
    require(now < bootstrap['expires_at'], 'update_root_expired')
    envelope = decode(raw)
    fields(envelope, ('signed', 'signatures'))
    payload = index(envelope['signed'])
    require(payload['repository_id'] == bootstrap['repository_id'], 'update_repository_changed')
    require(payload['issued_at'] <= now < payload['expires_at'], 'update_metadata_expired_or_future')
    signatures = envelope['signatures']
    require(type(signatures) is list and len(signatures) <= 8, 'update_signatures_refused')
    verified = set()
    for signature in signatures:
        fields(signature, ('keyid', 'signature')); keyid = digest(signature['keyid'])
        require(keyid not in verified and keyid in bootstrap['keys'], 'update_signer_refused')
        value = signature['signature']
        require(type(value) is str and len(value) == 128, 'update_signature_refused')
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(bootstrap['keys'][keyid])).verify(
                bytes.fromhex(value), canonical(payload))
        except (ValueError, InvalidSignature):
            require(False, 'update_signature_refused')
        verified.add(keyid)
    require(len(verified) >= bootstrap['threshold'], 'update_signature_threshold_refused')
    identity = fingerprint(bootstrap)
    content = fingerprint(payload)
    if previous is not None:
        fields(previous, ('bootstrap_sha256', 'sequence', 'payload_sha256', 'clock_high_water'))
        require(previous['bootstrap_sha256'] == identity, 'update_bootstrap_changed')
        integer(previous['sequence']); digest(previous['payload_sha256']); timestamp(previous['clock_high_water'])
        require(now >= previous['clock_high_water'], 'update_clock_rollback')
        require(payload['sequence'] >= previous['sequence'], 'update_metadata_rollback')
        if payload['sequence'] == previous['sequence']:
            require(content == previous['payload_sha256'], 'update_metadata_equivocation')
    return payload, {'bootstrap_sha256': identity, 'sequence': payload['sequence'],
                     'payload_sha256': content, 'clock_high_water': now}
