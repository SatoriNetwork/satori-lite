"""Single source of Base deployment config for the neuron.

Fetches the current addresses from central's /api/v1/base/config (cached), so a
Base redeploy is one change in central and neurons pick it up automatically.
Falls back to the built-in defaults if central is unreachable; per-key env vars
override everything (an escape hatch). Nothing else in the neuron hardcodes a
Base address.
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600
_cache = {"data": None, "ts": 0.0}

# Fallback only (used if central is unreachable). Tracks the current deployment.
DEFAULTS = {
    "chainId": 8453,
    "chainName": "Base",
    "rpcUrl": "https://mainnet.base.org",
    "explorerUrl": "https://basescan.org",
    "symbol": "SATORI",
    "decimals": 18,
    "token": "0xd0a5bFF2789230b0fb1Bd989fc7CE7084c947E17",
    "merkle": "0x8d2EcABbDd72C427A62ECe45E30a75B41Eb71E9B",
    "airdrop": "0x4Ec6F7a801A3A39Ca5E77C6c0A397C93067Bafa1",
    "rewards": "0x965bcCAF7ac896ba5a8FD57d5E9Dc1F044792Be4",
    "engine": "0x433aBc7877F37560c50238a09F8F3CA207664e12",
    "games": "0xda8f3D153bb620EF676a10EF331B9B3Db4348f81",
    "registry": "0x0917659Fb65192B342A0Baf95b25E61Fc71a76F7",
    "hub": "0x195083eE270ad89459e8dbF2d40fDFc85fb21327",
    # SatoriRewardsLens: read-only views, e.g. claimable prediction rewards.
    "lens": "0x5a81cd45D6832CD6f2b4Ef53694ea07431c96DD5",
    # Nostr pubkey of the bridge that mirrors registry streams.
    "streamProvider": "cb45de2a76a5df79ea8aff0c867e96e527f803eb389420419e441a4946129838",
}

_ENV = {
    "chainId": "BASE_CHAIN_ID",
    "rpcUrl": "BASE_RPC_URL",
    "explorerUrl": "BASE_EXPLORER_URL",
    "token": "BASE_SATORI_TOKEN",
    "merkle": "BASE_SATORI_MERKLE",
    "airdrop": "BASE_SATORI_AIRDROP",
    "rewards": "BASE_SATORI_REWARDS",
    "engine": "BASE_SATORI_ENGINE",
    "games": "BASE_SATORI_GAMES",
    "registry": "BASE_SATORI_REGISTRY",
    "hub": "BASE_SATORI_HUB",
    "lens": "BASE_SATORI_LENS",
    "streamProvider": "BASE_STREAM_PROVIDER",
}


def _central_url():
    url = os.getenv("SATORI_API_URL")
    if url:
        return url
    try:
        from satorilib.config import get_api_url
        return get_api_url()
    except Exception:
        return None


def base_config(force=False) -> dict:
    """Current Base config: defaults, overlaid with central's published config,
    then with any env overrides. Cached for an hour."""
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < _TTL_SECONDS:
        return _cache["data"]
    cfg = dict(DEFAULTS)
    url = _central_url()
    if url:
        try:
            import requests
            resp = requests.get(f"{url}/api/v1/base/config", timeout=10)
            if resp.status_code == 200:
                cfg.update({k: v for k, v in resp.json().items() if v})
        except Exception as e:
            logger.debug("base config fetch failed, using defaults: %s", e)
    for key, env in _ENV.items():
        val = os.getenv(env)
        if val:
            cfg[key] = int(val) if key == "chainId" else val
    _cache["data"] = cfg
    _cache["ts"] = now
    return cfg
