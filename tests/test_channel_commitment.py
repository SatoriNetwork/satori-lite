"""A channel commitment is kept, and claimed, only when its signed transaction
pays what it states (a report of 2026-11-19): an honest commitment, built by
the real sendChannelPayment, is kept; one whose transaction pays the receiver
nothing, or less than it states, is refused although the sender's signature
on it is good; and a claim refuses such a commitment an older version kept.
The real StartupDag channel code with real keys; no network."""
import asyncio
import time

import pytest

pytest.importorskip('evrmore')

from evrmore.core import CMutableTransaction, CMutableTxIn, COutPoint, lx  # noqa: E402
from satorilib.satori_nostr.models import (  # noqa: E402
    ChannelCommitment, ChannelOpen, InboundChannelOpen)
from satorilib.wallet.evrmore.identity import EvrmoreIdentity  # noqa: E402
from satorilib.wallet.evrmore.scripts.channels.lock import paymentChannel  # noqa: E402
from satorineuron.network_db import NetworkDB  # noqa: E402

from tests.neuron_start import start  # noqa: E402

LOCKED = 100_000
PAY = 1_000
FUNDING = 'ab' * 32
MINUTES = 44_640
STREAM = 'paid.stream'


class Manager:
    """The walletManager a channel needs: its alias, already connected."""

    def __init__(self, alias):
        self.alias = self.wallet = alias

    def isConnected(self):
        return True


class Relay:
    """Keeps what is published instead of sending it."""

    def __init__(self):
        self.commitments = []

    async def publish_commitment(self, commitment, receiver_nostr_pubkey):
        self.commitments.append(commitment)


def neuron(alias, db, relay=None):
    n = object.__new__(start.StartupDag)
    n.walletManager = Manager(alias)
    n.networkDB = db
    n.nostrPubkey = '7' * 64
    n._networkClients = {'relay': relay} if relay else {}
    return n


@pytest.fixture
def channel(tmp_path):
    """A channel of LOCKED sats from a sender to a receiver, each side's own
    record of it, the receiver's from the sender's open announcement."""
    sender, receiver = (  # as the neuron's alias: a wallet of its own identity
        start.EvrmoreWallet(identity=EvrmoreIdentity(walletPath=str(tmp_path / name)))
        for name in ('sender.yaml', 'receiver.yaml'))
    receiver.getUnspents = lambda: None  # a claim's refresh, offline
    redeem = paymentChannel(sender.pubkey, receiver.pubkey, minutes=MINUTES)
    p2sh = start.EvrmoreWallet.generateP2SHAddress(redeem)
    now = int(time.time())
    relay = Relay()
    payer = neuron(sender, NetworkDB(str(tmp_path / 'sender.db')), relay)
    payer.networkDB.save_channel(
        p2sh, sender.pubkey, receiver.pubkey, bytes(redeem).hex(), FUNDING, 0,
        LOCKED, LOCKED, True, None, MINUTES, created_at=now)
    payer.networkDB.update_utxo_checked_at(p2sh)  # no Electrum check
    payee = neuron(receiver, NetworkDB(str(tmp_path / 'receiver.db')))
    asyncio.run(payee._channelHandleOpen(InboundChannelOpen(ChannelOpen(
        p2sh_address=p2sh, sender_pubkey=sender.pubkey,
        receiver_pubkey=receiver.pubkey, redeem_script=bytes(redeem).hex(),
        funding_txid=FUNDING, funding_vout=0, locked_sats=LOCKED, blocks=None,
        minutes=MINUTES, timestamp=now, sender_nostr_pubkey='5' * 64),
        event_id='open')))
    return dict(sender=sender, receiver=receiver, redeem=redeem, p2sh=p2sh,
                relay=relay, payer=payer, payee=payee)


def forged(ch, pays):
    """A commitment stating PAY sats paid, LOCKED − PAY left, whose transaction,
    signed by the real sender, pays the receiver only `pays` (the rest goes
    back to the channel)."""
    sender = ch['sender']
    change = sender._compileSatoriChangeOutput(
        satoriSats=pays, gatheredSatoriSats=LOCKED, changeAddress=ch['p2sh'])
    paid = sender._compileSatoriOutputs({
        start.EvrmoreWallet.generateAddress(ch['receiver'].pubkey): pays}) if pays else []
    tx = CMutableTransaction([CMutableTxIn(COutPoint(lx(FUNDING), 0))], paid + [change])
    sig = sender.paymentChannelMultisigTransactionMiddle(tx, ch['redeem'], 0, None)
    return ChannelCommitment(
        p2sh_address=ch['p2sh'], sender_pubkey=sender.pubkey,
        receiver_pubkey=ch['receiver'].pubkey, partial_tx_hex=tx.serialize().hex(),
        sender_sigs=[sig.hex()], pay_amount_sats=PAY, remainder_sats=LOCKED - PAY,
        fee=0, timestamp=int(time.time()), stream_name=STREAM)


def kept(ch):
    return ch['payee'].networkDB.get_channel(ch['p2sh']).get('pending_commitment')


def test_an_honest_commitment_is_kept(channel):
    asyncio.run(channel['payer'].sendChannelPayment(channel['p2sh'], PAY, STREAM))
    commitment = channel['relay'].commitments[-1]
    asyncio.run(channel['payee']._channelProcessCommitment(commitment))
    assert kept(channel) == commitment.to_json()


@pytest.mark.parametrize('pays', [0, PAY // 2])
def test_a_commitment_that_does_not_pay_what_it_states_is_refused(channel, pays):
    commitment = forged(channel, pays)
    record = channel['payee'].networkDB.get_channel(channel['p2sh'])
    assert start.StartupDag._channelVerifySenderSig(commitment, record)
    asyncio.run(channel['payee']._channelProcessCommitment(commitment))
    assert not kept(channel)


def test_a_claim_refuses_a_kept_commitment_that_does_not_pay_what_it_states(channel):
    channel['payee'].networkDB.store_pending_commitment(
        channel['p2sh'], forged(channel, 0).to_json())
    with pytest.raises(ValueError, match='does not pay what it states'):
        asyncio.run(channel['payee'].claimChannel(channel['p2sh']))
