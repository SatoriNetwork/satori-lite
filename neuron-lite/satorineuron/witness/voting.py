"""Witness voting: stream vote allocation event builder (Model B dual-sig)."""
import json
import time

KIND_STREAM_VOTE_ALLOCATION = 34610


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
