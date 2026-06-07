"""Witness voting: stream vote allocation and stream flag event builders (Model B dual-sig)."""
import json
import time

KIND_STREAM_VOTE_ALLOCATION = 34610
KIND_STREAM_FLAG = 34611

VALID_FLAG_REASONS = frozenset({'spam', 'misleading', 'inactive', 'other'})


def build_vote_allocation(
    allocations: list[dict],
    wallet_manager,
    nostr_pubkey: str,
) -> tuple[dict, list[list[str]]]:
    """Build and sign a STREAM_VOTE_ALLOCATION inner payload.

    Args:
        allocations: list of {'stream_name': str, 'provider_pubkey': str, 'percentage': float}
        wallet_manager: WalletManager instance (must have sign_message, wallet_pubkey, wallet_evr_address)
        nostr_pubkey: caller's Nostr pubkey hex

    Returns:
        (inner_payload_dict, nostr_tags)

    Raises:
        ValueError: if total > 100, any percentage <= 0, or duplicate streams
    """
    if not allocations:
        raise ValueError('allocations must not be empty')

    total = sum(a['percentage'] for a in allocations)
    if total > 100.0:
        raise ValueError(f'Total allocation {round(total, 4)}% exceeds 100%')
    if any(a['percentage'] <= 0 for a in allocations):
        raise ValueError('All percentages must be > 0')

    seen = set()
    for a in allocations:
        key = (a['stream_name'], a['provider_pubkey'])
        if key in seen:
            raise ValueError(f"Duplicate stream: {a['stream_name']}")
        seen.add(key)

    payload = {
        'action': 'stream_vote_allocation',
        'voter_wallet_pubkey': wallet_manager.wallet_pubkey,
        'voter_evr_address': wallet_manager.wallet_evr_address,
        'voter_nostr_pubkey': nostr_pubkey,
        'allocated_at': int(time.time()),
        'allocations': allocations,
        'total_percentage': round(total, 4),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    payload['evr_signature'] = wallet_manager.sign_message(canonical)

    tags = [
        ['d', nostr_pubkey],
        ['satori', 'stream_vote_allocation'],
    ]
    return payload, tags


def build_stream_flag(
    stream_name: str,
    provider_pubkey: str,
    reason: str,
    details: str,
    wallet_manager,
    nostr_pubkey: str,
) -> tuple[dict, list[list[str]]]:
    """Build and sign a STREAM_FLAG inner payload.

    Raises:
        ValueError: if reason is not in VALID_FLAG_REASONS
    """
    if reason not in VALID_FLAG_REASONS:
        raise ValueError(f"Invalid reason '{reason}'. Must be one of: {', '.join(sorted(VALID_FLAG_REASONS))}")

    d_tag = f'{nostr_pubkey}|||{stream_name}|||{provider_pubkey}'
    payload = {
        'action': 'stream_flag',
        'flagged_stream_name': stream_name,
        'flagged_provider_pubkey': provider_pubkey,
        'reason': reason,
        'details': (details or '').strip(),
        'flagger_wallet_pubkey': wallet_manager.wallet_pubkey,
        'flagger_evr_address': wallet_manager.wallet_evr_address,
        'flagger_nostr_pubkey': nostr_pubkey,
        'flagged_at': int(time.time()),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    payload['evr_signature'] = wallet_manager.sign_message(canonical)

    tags = [
        ['d', d_tag],
        ['satori', 'stream_flag'],
        ['stream', stream_name],
    ]
    return payload, tags
