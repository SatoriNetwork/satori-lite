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
    "token": "0x263d7ED43E19Fc573DE929815b1B322437DcD3A1",
    "merkle": "0xED3CC885b19D29D834991e3dce1bb796a0395d78",
    "rewards": "0xb448DFe8BA08A008A275Cfa91c7ac2F191c0b36E",
    "engine": "0xc00FDabbf510b92Bd2c3f2eC0541E979EC124b16",
    "games": "0xDC7d53C7DF8764e814DED7aD2a6A8324d6aB39fc",
    "registry": "0x954C3217B4C4725e2e0ca77a7dB5d89cd91da81B",
}

_ENV = {
    "chainId": "BASE_CHAIN_ID",
    "rpcUrl": "BASE_RPC_URL",
    "explorerUrl": "BASE_EXPLORER_URL",
    "token": "BASE_SATORI_TOKEN",
    "merkle": "BASE_SATORI_MERKLE",
    "rewards": "BASE_SATORI_REWARDS",
    "engine": "BASE_SATORI_ENGINE",
    "games": "BASE_SATORI_GAMES",
    "registry": "BASE_SATORI_REGISTRY",
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
