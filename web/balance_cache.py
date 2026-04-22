"""Process-global wallet balance cache.

One long-lived WalletManager (persistent=True) + a cache that never
auto-expires. The cache is populated on first load and only refreshed when
the user clicks the Refresh button (force=True) or after a transaction
(invalidate() is called). No periodic polling of ElectrumX.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

from satorineuron.init.wallet import WalletManager

log = logging.getLogger(__name__)

_BALANCE_KEY = "balance"
_WALLET_KEY = "wallet_only"

_manager: Optional[WalletManager] = None
_manager_lock = threading.Lock()
_cache: Dict[str, Any] = {}
_fetch_lock = threading.Lock()
_wallet_fetch_lock = threading.Lock()


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
    manager.connect()

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
    """Return a balance snapshot, from cache unless force=True.

    force=True bypasses the cache (used by the dashboard Refresh button and
    after a successful send, where the UTXO set just changed).
    """
    if not force and _BALANCE_KEY in _cache:
        snap = dict(_cache[_BALANCE_KEY])
        snap["cache_hit"] = True
        return snap

    with _fetch_lock:
        if not force and _BALANCE_KEY in _cache:
            snap = dict(_cache[_BALANCE_KEY])
            snap["cache_hit"] = True
            return snap
        snapshot = _fetch_snapshot()
        _cache[_BALANCE_KEY] = snapshot
        _cache[_WALLET_KEY] = snapshot["wallet_balance"]
        out = dict(snapshot)
        out["cache_hit"] = False
        return out


def get_wallet_balance(force: bool = False) -> float:
    """Return wallet-only SATORI balance, cached independently of the full snapshot.

    Skips vault entirely so it's cheaper to call on pages that only need the
    wallet balance (e.g. marketplace subscribe check).
    """
    if not force and _WALLET_KEY in _cache:
        return _cache[_WALLET_KEY]
    with _wallet_fetch_lock:
        if not force and _WALLET_KEY in _cache:
            return _cache[_WALLET_KEY]
        manager = _get_manager()
        manager.connect()
        wallet_sat = 0.0
        if manager.wallet and hasattr(manager.wallet, 'getBalances'):
            try:
                manager.wallet.getBalances()
                wallet_sat = float(manager.wallet.balance.amount) if getattr(manager.wallet, 'balance', None) else 0.0
            except Exception as e:
                log.error("wallet-only getBalances failed: %s", e)
        _cache[_WALLET_KEY] = wallet_sat
        return wallet_sat


def invalidate() -> None:
    """Drop the cached snapshots — call after a send so the next read is fresh."""
    _cache.pop(_BALANCE_KEY, None)
    _cache.pop(_WALLET_KEY, None)
