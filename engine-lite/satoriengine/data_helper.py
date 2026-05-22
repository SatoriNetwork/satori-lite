"""Shared helpers for turning Central-server observations into engine-ready
data.

Central's ``/api/v1/observations/batch`` endpoint returns a list of observation
objects; ``satorilib``'s ``getObservationsBatch`` copies ``stream.uuid`` onto
each as a top-level ``stream_uuid``. One observation looks like:

    {
      "id": 201570,
      "value": "0.00083",                    # string
      "observed_at": "1779426055.9544094",   # string unix epoch (fractional)
      "ts": "2026-05-22T05:00:55.955559",    # ISO-8601, no timezone
      "hash": "",
      "stream_id": 66,
      "stream_uuid": "b4bf6ce9-...",
      "stream": {"id": 66, "uuid": "...", "name": "safetrade_xtm", ...}
    }

This module is the single source of truth for that conversion.

Efficiency rule: the timestamp is parsed to a float unix epoch exactly ONCE,
here, at ingest. It is kept and stored as an epoch — no datetime objects, no
string round-trips. The epoch -> datetime64 conversion happens later, exactly
once per stream, when the engine frame is built (see ``stream_store.py``).
"""

from __future__ import annotations

import pandas as pd

# Plausible unix-epoch range: year 2000 .. year 2100. Used to tell an epoch
# apart from a small bare number that merely parses as a float.
_EPOCH_MIN = 946684800
_EPOCH_MAX = 4102444800

# Storage-shaped columns: what gets persisted (see StreamStore).
STORE_COLUMNS = ['epoch', 'value', 'id']


def _parse_epoch(raw) -> float | None:
    """Parse a Central timestamp into a unix epoch (float seconds).

    Accepts a unix epoch (string or number, possibly fractional) or an
    ISO-8601 string. Returns ``None`` if it cannot be parsed.
    """
    if raw is None:
        return None
    try:
        epoch = float(raw)
        if _EPOCH_MIN < epoch < _EPOCH_MAX:
            return epoch
    except (TypeError, ValueError):
        pass
    try:
        return pd.to_datetime(raw, utc=True).timestamp()
    except (ValueError, TypeError):
        return None


def normalize_central_observation(obs: dict) -> dict | None:
    """Normalize one raw Central observation.

    Returns a dict with keys ``stream_uuid``, ``stream_name``, ``epoch``,
    ``value``, ``id`` — or ``None`` if the observation has no usable numeric
    value or no parseable timestamp.

    - ``value`` is parsed to ``float`` (Central sends it as a string).
    - ``epoch`` comes from ``observed_at``, falling back to ``ts``.
    - ``id`` comes from ``hash``, falling back to Central's row ``id``
      (``hash`` is empty in practice, so this is almost always the row id).
    """
    raw_value = obs.get('value')
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None

    epoch = _parse_epoch(obs.get('observed_at') or obs.get('ts'))
    if epoch is None:
        return None

    hash_val = obs.get('hash') or obs.get('id')
    stream = obs.get('stream') or {}

    return {
        'stream_uuid': obs.get('stream_uuid') or stream.get('uuid'),
        'stream_name': stream.get('name', 'unknown'),
        'epoch': epoch,
        'value': value,
        'id': str(hash_val) if hash_val is not None else None,
    }


def batch_to_stream_frames(observations: list[dict]) -> dict[str, pd.DataFrame]:
    """Convert a raw Central batch into one storage-shaped DataFrame per stream.

    Returns ``{stream_uuid: DataFrame[epoch, value, id]}`` — each sorted
    oldest-first, ready to hand to ``StreamStore.append``. Observations with no
    usable value or no ``stream_uuid`` are dropped.
    """
    rows_by_stream: dict[str, list[dict]] = {}
    for obs in observations:
        norm = normalize_central_observation(obs)
        if norm is None or not norm['stream_uuid']:
            continue
        rows_by_stream.setdefault(norm['stream_uuid'], []).append(
            {c: norm[c] for c in STORE_COLUMNS})

    frames: dict[str, pd.DataFrame] = {}
    for stream_uuid, rows in rows_by_stream.items():
        df = pd.DataFrame(rows, columns=STORE_COLUMNS)
        frames[stream_uuid] = df.sort_values('epoch').reset_index(drop=True)
    return frames
