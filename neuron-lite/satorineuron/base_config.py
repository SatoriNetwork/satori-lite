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
    "chainId": 84532,
    "chainName": "Base Sepolia",
    "rpcUrl": "https://sepolia.base.org",
    "explorerUrl": "https://sepolia.basescan.org",
    "symbol": "SATORI",
    "decimals": 18,
    "token": "0x4dee947383d5C9c8E421F55Fe3518a29324aE469",
    "merkle": "0x78C83AE275e9659Bc8490fa33943e22ec7c5A7e7",
    "airdrop": "0xF15CEAec0e29A0B6551180F8617850bc5356938C",
    "rewards": "0xde5d2B4b92c1231EC19cd3877D7C3eb6b44982Ac",
    "engine": "0xE8fA688ea29850305216121C0bddfFb9096A1093",
    "games": "0xB06e419D24cCbA967c6193e9e374117171B400C3",
    "registry": "0x1ED75AB0a66Ba7d23C3E5c84798e90205E76743a",
    "hub": "0x1eec7E1443C1065ab78180A14326e5Ac8284f143",
    # SatoriRewardsLens: read-only views, e.g. claimable prediction rewards.
    "lens": "0x9BF5266F60092F1bFbd5F16d21d0bE9673c0F91F",
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
