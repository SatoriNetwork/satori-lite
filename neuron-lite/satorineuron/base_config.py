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
    "token": "0x7Dd4F6EF7FE534e4962f975cffe93D19d9B891E1",
    "merkle": "0x20d398d45A3cC6D6A73a2381294E801736E120aB",
    "airdrop": "0xf8482f9640A7A2bb4Ab372DE677E8a82eBEC04A4",
    "rewards": "0xF353eb4843E3B8822dd3fE88e638B02C456749BE",
    "engine": "0x90C5Bc6f0322900ebAA12bb382Db44be7bc2e240",
    "games": "0x05096F2caB4D6E806604caDc2F78A8563be74783",
    "registry": "0x95989fc59B0Ed1c323Af14d9139cD05F0379F556",
    "hub": "0x17719B7B1A630dECBc5B7FFDdEd14ffb24120183",
    # SatoriRewardsLens: read-only views, e.g. claimable prediction rewards.
    "lens": "0x877DD8bD95E1301E283CACF31F0ec60401dB746a",
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
