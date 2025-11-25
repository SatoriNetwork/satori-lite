"""
Satori Lite Library
Minimal version of satorilib for lightweight Satori neuron deployment
"""

__version__ = '0.1.0'

# Import key components for easy access
from satorilib.concepts.structs import StreamId, Stream, StreamPairs, StreamOverview
from satorilib.concepts import constants
from satorilib.wallet import EvrmoreWallet
from satorilib.wallet.evrmore.identity import EvrmoreIdentity
from satorilib.server import SatoriServerClient
from satorilib.server.api import CheckinDetails
from satorilib.asynchronous import AsyncThread

# Optional centrifugo support
try:
    from satorilib.centrifugo import publish_to_stream_rest
    CENTRIFUGO_AVAILABLE = True
except ImportError:
    CENTRIFUGO_AVAILABLE = False
    publish_to_stream_rest = None

__all__ = [
    'StreamId',
    'Stream',
    'StreamPairs',
    'StreamOverview',
    'constants',
    'EvrmoreWallet',
    'EvrmoreIdentity',
    'SatoriServerClient',
    'CheckinDetails',
    'AsyncThread',
    'publish_to_stream_rest',
    'CENTRIFUGO_AVAILABLE',
]
