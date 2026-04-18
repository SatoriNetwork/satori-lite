"""Process-global wallet balance cache.

One long-lived WalletManager (persistent=True) + a 30 s TTL cache, shared by
every request in this Flask process. Dashboards that refresh within the TTL
return from memory in <10 ms; cache misses reuse the open ElectrumX socket
rather than opening a new one, so they finish in ~0.5 s instead of the 5+ s
the old retry-loop path used to take.

A single threading.Lock serialises the fetch, so N concurrent cache-miss
requests produce exactly one getBalances() call on ElectrumX.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

from satorineuron.init.wallet import WalletManager

log = logging.getLogger(__name__)

BALANCE_TTL_SECONDS = float(os.environ.get("BALANCE_TTL_SECONDS", "30"))
_BALANCE_KEY = "balance"

_manager: Optional[WalletManager] = None
_manager_lock = threading.Lock()
_cache: Dict[str, Tuple[Any, float]] = {}
_fetch_lock = threading.Lock()


def _get_manager() -> WalletManager:
    """Return the singleton WalletManager, constructing it on first use."""
    global _manager
    if _manager is not None:
        return _manager
    with _manager_lock:
        if _manager is None:
            log.info("creating process-global WalletManager (persistent=True)")
            _manager = WalletManager.create(
                useConfigPassword=False,
                persistent=True,
            )
        return _manager


def _fetch_snapshot() -> Dict[str, Any]:
    """Hit ElectrumX once and return a balance snapshot."""
    manager = _get_manager()
    if not manager.connect():
        log.warning("WalletManager.connect() returned False")

    def _read(obj, label: str) -> tuple[float, float]:
        if not obj or not hasattr(obj, "getBalances"):
            return 0.0, 0.0
        try:
            obj.getBalances()
        except Exception as e:
            log.error("%s getBalances failed: %s", label, e)
            return 0.0, 0.0
        satori = obj.balance.amount if getattr(obj, "balance", None) else 0.0
        evr = obj.currency.amount if getattr(obj, "currency", None) else 0.0
        return float(satori), float(evr)

    wallet_sat, wallet_evr = _read(manager.wallet, "wallet")
    vault_sat, vault_evr = _read(manager.vault, "vault")

    return {
        "total": wallet_sat + vault_sat,
        "wallet_balance": wallet_sat,
        "vault_balance": vault_sat,
        "total_evr": wallet_evr + vault_evr,
        "wallet_evr": wallet_evr,
        "vault_evr": vault_evr,
        "vault_unlocked": bool(
            manager.vault and getattr(manager.vault, "isDecrypted", True)
        ),
        "fetched_at": time.time(),
    }


def get_balance_snapshot(force: bool = False) -> Dict[str, Any]:
    """Return a balance snapshot, from cache if fresh.

    force=True bypasses the cache (used by the dashboard Refresh button and
    after a successful send, where the UTXO set just changed).
    """
    now = time.time()
    if not force:
        hit = _cache.get(_BALANCE_KEY)
        if hit is not None and hit[1] > now:
            snap = dict(hit[0])
            snap["cache_hit"] = True
            return snap

    with _fetch_lock:
        if not force:
            hit = _cache.get(_BALANCE_KEY)
            if hit is not None and hit[1] > time.time():
                snap = dict(hit[0])
                snap["cache_hit"] = True
                return snap
        snapshot = _fetch_snapshot()
        _cache[_BALANCE_KEY] = (snapshot, time.time() + BALANCE_TTL_SECONDS)
        out = dict(snapshot)
        out["cache_hit"] = False
        return out


def invalidate() -> None:
    """Drop the cached snapshot — call after a send so the next read is fresh."""
    _cache.pop(_BALANCE_KEY, None)
