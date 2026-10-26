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
    "token": "0xe6A4b12Eae75795E4D427706dc67c1392f75D54A",
    "merkle": "0xeCc7f1d5Df7aCBD7D1b0615490A88d14CA2B1F1c",
    "airdrop": "0x5BF17B223D623CdbD37E3497b95c911e954a3E65",
    "rewards": "0x618138368f9Fc69585907d576DEA5C89F1C798D1",
    "engine": "0x438Ca7a87b4D384b57f27c0829C06Ef1F1f90A60",
    "games": "0x4AD128bF49841d8e970dC36E1D1e43b36a79B612",
    "registry": "0xcE4278b73dFE00772B485697FD1E97Bb0669c87f",
    "hub": "0x97D53d240e3dcfC6569d7B4001b03062681f4818",
    # SatoriRewardsLens: read-only views, e.g. claimable prediction rewards.
    "lens": "0xBadE17EA7759D4fc19c1468Fc135e3292591A412",
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
