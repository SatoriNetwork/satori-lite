"""Paid streams over the Satori relays, paid on Base through the PaymentChannels
contract: the browser neuron's market (Satori/satori src/neuron/market.js;
docs/design/market.md "Selling and buying over the relays") on Lite's kinds
and fields.

  sell: a publication priced with price_per_obs (satoshis, 10^-8 SATORI) is
  announced encrypted, with metadata.market = {chain, contract, token, payee:
  the wallet}; each observation goes to each buyer in good standing.
  buy: the wallet opens a channel (deposit 100 × the price by default), the
  34602 names it with the alias's binding; each delivery received → the
  alias's next cumulative voucher in a 34603 for seq + 1.
  The seller checks the channel on chain and each voucher, settles at most
  once a day (settleMany), and stops on an unsubscribe or a close request;
  a buyer's Stop asks to close (requestClose) and withdraws after the grace
  period.

Everything runs on the network loop (chain calls in a thread). Nostr goes out
through `send` (start.py: every connected relay). Kept in network_db's meta
key 'market': {sales, buys, settled_at}, amounts int wei.
"""
import asyncio
import json
import logging
import re
import time
from decimal import Decimal

from eth_account import Account
from eth_utils import is_address

from satorilib.chain.channels import (
    binding_signer, channel_error_text, same, sign_binding, sign_voucher, verify_voucher)

logger = logging.getLogger(__name__)

DEPOSIT_OBS = 100  # a Buy's deposit unless one is given: 100 observations
SAT = 10 ** 10  # wei per satoshi: price_per_obs is in satoshis (10^-8 SATORI)
SETTLE_EVERY_S = 86_400  # the seller settles at most once a day: each settle costs gas
_HEX32 = re.compile(r'^0x[0-9a-fA-F]{64}$')
_SAFE = 2 ** 53 - 1  # price_per_obs is a JSON number: a safe integer


class MarketError(Exception):
    """A market action refused, in plain words."""


def sat(wei) -> str:
    """Wei as SATORI, '0.00001' (no trailing zeros)."""
    return format((Decimal(int(wei)) / Decimal(10 ** 18)).normalize(), 'f')


def listing_of(entry, chain_id, contract, token):
    """A catalog entry for sale on this chain's Satori contract: its price_per_obs (satoshis, above 0) and
    metadata.market (chain, contract, token SATORI, a payee address) → {chain, contract, token, payee,
    price (wei)}, or None."""
    m = ((entry or {}).get('metadata') or {}).get('market')
    sats = (entry or {}).get('price_per_obs')
    if (not isinstance(sats, int) or isinstance(sats, bool) or not 0 < sats <= _SAFE or not isinstance(m, dict)
            or not same(m.get('contract'), contract) or not same(m.get('token'), token)
            or not isinstance(m.get('payee'), str) or not is_address(m['payee'])):
        return None
    try:
        if int(m.get('chain')) != chain_id:
            return None
    except (TypeError, ValueError):
        return None
    return {'chain': chain_id, 'contract': contract, 'token': token, 'payee': m['payee'], 'price': sats * SAT}


def _owed(s) -> int:
    """What a sale's buyer owes for what it was delivered: from the channel's settled amount when accepted."""
    return s['base'] + s['delivered'] * s['price']


def deliverable(s) -> bool:
    """A buyer gets the next observation while its close isn't asked, its newest voucher covers every one
    delivered (the first is free), and its deposit one more."""
    return not s['closing'] and s['cumulative'] >= _owed(s) and s['deposit'] >= _owed(s) + s['price']


class Market:
    """
    db: get_meta(key) / set_meta(key, text) (network_db)
    nostr_pubkey: our Nostr key (hex)
    alias_key: () → the alias's 0x key: vouchers, the binding, settle, close, withdraw
    wallet_address: () → the wallet's address (the seller's payee), or None
    chain: () → satorilib's PaymentChannels on the current chain
    send: async subscription(provider, stream, payment_channel), unsubscribe(provider, stream),
          payment(provider, stream, seq, amount_sats, voucher), deliver(observation, buyer) → a relay took it
    prices: () → {stream: price_per_obs (satoshis)} of our priced publications
    """

    def __init__(self, db, nostr_pubkey, alias_key, wallet_address, chain, send, chain_id, contract, token,
                 prices, now=time.time):
        self.db, self.pubkey, self.send = db, nostr_pubkey, send
        self._alias, self._wallet_address, self._chain, self._prices = alias_key, wallet_address, chain, prices
        self.chain_id, self.contract, self.token = int(chain_id), contract, token
        self._now = now
        self.m = {'sales': {}, 'buys': {}, 'settled_at': 0}
        self.last_error = None  # the hourly pass's last failure

    def _t(self) -> int:
        return int(self._now())

    def _price(self, stream) -> int:
        """Our stream's price_per_obs in satoshis (0 = free)."""
        return int(self._prices().get(stream) or 0)

    async def load(self):
        raw = await asyncio.to_thread(self.db.get_meta, 'market')
        self.m = {'sales': {}, 'buys': {}, 'settled_at': 0, **(json.loads(raw) if raw else {})}

    async def _save(self):
        await asyncio.to_thread(self.db.set_meta, 'market', json.dumps(self.m))

    def _need_alias(self):
        key = self._alias()
        if not key:
            raise MarketError('No alias to sign with.')
        return key

    def _sales_of(self, stream):
        return [s for s in self.m['sales'].values() if s['stream'] == stream]

    def _open_buy(self, provider, stream):
        return next((b for b in self.m['buys'].values()
                     if b['pubkey'] == provider and b['stream'] == stream and b['status'] == 'open'), None)

    # ── selling ──────────────────────────────────────────────────────────

    def listing(self, stream):
        """Its listing for the announcement: price_per_obs (satoshis) and the channel's market, or None when free."""
        sats = self._price(stream)
        payee = self._wallet_address()
        if sats <= 0 or not payee:
            return None
        return {'price_per_obs': sats,
                'market': {'chain': self.chain_id, 'contract': self.contract, 'token': self.token, 'payee': payee}}

    async def deliver(self, observation) -> int:
        """A new observation of our priced stream (after its heartbeat): to each buyer in good standing, one
        event each, counted as delivered once a relay took it. → how many buyers got it"""
        out = [s for s in self._sales_of(observation.stream_name) if deliverable(s)]
        if not out:
            return 0
        for s in out:
            s['delivered'] += 1
        await self._save()
        took = 0
        for s in out:
            try:
                ok = await self.send.deliver(observation, s['buyer'])
            except Exception as e:
                logger.warning('market: delivery to %s… failed: %s', s['buyer'][:8], e)
                ok = False
            if ok:
                took += 1
            else:
                s['delivered'] -= 1  # no relay took it: it isn't owed
        await self._save()
        return took

    async def on_subscription(self, inbound):
        """A buyer's 34602 to one of our streams (satorilib's InboundSubscription, its author the subscriber):
        its channel read on chain and accepted, or its unsubscribe (no more deliveries)."""
        sub = inbound.subscription
        if sub.nostr_pubkey != self.pubkey:
            return
        buyer = sub.subscriber_pubkey
        # the buyer's channels on this stream still served: an unsubscribe, or a new channel, closes them
        # (their last vouchers collected in the daily settle, well inside the 7-day grace period)
        was = [s for s in self.m['sales'].values() if s['stream'] == sub.stream_name and s['buyer'] == buyer
               and not s['closing']]
        if inbound.unsubscribe:
            for s in was:
                s['closing'] = True
            if was:
                await self._save()
            return
        price = self._price(sub.stream_name) * SAT
        c = sub.payment_channel
        if not price or not isinstance(c, dict) or not _HEX32.match(str(c.get('id'))):
            return
        try:
            if int(c.get('chain')) != self.chain_id or not same(c.get('contract'), self.contract):
                return
        except (TypeError, ValueError):
            return
        cid = c['id'].lower()
        if cid in self.m['sales']:
            return  # seen (a relay sends it again on each connect)
        ch = await asyncio.to_thread(self._chain().read_channel, cid)
        signer = binding_signer(cid, buyer, c.get('binding'))
        if (not ch or not same(ch['payee'], self._wallet_address()) or not same(ch['token'], self.token)
                or not same(ch['signer'], signer) or ch['closed'] or ch['close_requested_at']
                or ch['deposit'] - ch['settled'] < price or cid in self.m['sales']):
            return
        for s in was:
            s['closing'] = True
        self.m['sales'][cid] = {
            'stream': sub.stream_name, 'buyer': buyer, 'channel': cid, 'signer': ch['signer'], 'price': price,
            'base': ch['settled'], 'deposit': ch['deposit'], 'settled': ch['settled'], 'cumulative': ch['settled'],
            'signature': None, 'delivered': 0, 'closing': False}
        await self._save()

    async def on_payment(self, inbound):
        """A buyer's 34603 (satorilib's InboundPayment, from_pubkey its author): its voucher kept when it is the
        channel's signer's, more than the last, and within the deposit."""
        p = inbound.payment
        v = p.voucher
        if not isinstance(v, dict):
            return
        s = self.m['sales'].get(str(v.get('channel')).lower())
        if not s or s['buyer'] != p.from_pubkey or s['stream'] != p.stream_name:
            return
        try:
            cumulative = int(str(v.get('cumulative')))
        except ValueError:
            return
        if cumulative <= s['cumulative'] or cumulative > s['deposit']:
            return
        if not verify_voucher(s['signer'], v.get('signature'), self.chain_id, s['channel'], cumulative, self.contract):
            return
        s['cumulative'] = cumulative
        s['signature'] = v['signature']
        await self._save()

    async def _collect(self):
        """Once a day at most: every sale's newest voucher above what is settled, in one transaction (the
        seller's alias, into the wallet). A closing sale goes once nothing is due."""
        sales = list(self.m['sales'].values())
        due = [s for s in sales if s['signature'] and s['cumulative'] > s['settled']]
        if due and self._t() - self.m.get('settled_at', 0) >= SETTLE_EVERY_S:
            r = await asyncio.to_thread(self._chain().settle_many, self._need_alias(),
                                        [{'id': s['channel'], 'cumulative': s['cumulative'], 'signature': s['signature']}
                                         for s in due])
            for x in r['settled']:
                for s in due:
                    if same(s['channel'], x['id']):
                        s['settled'] = x['settled']
            self.m['settled_at'] = self._t()
        for s in sales:
            if s['closing'] and s['cumulative'] <= s['settled']:
                del self.m['sales'][s['channel']]
        await self._save()

    # ── buying ───────────────────────────────────────────────────────────

    def _view(self, channel):
        b = self.m['buys'].get(channel)
        return b and {'pubkey': b['pubkey'], 'stream': b['stream'], 'name': b['name'], 'channel': b['channel'],
                      'price': sat(b['price']), 'deposit': sat(b['deposit']), 'paid': sat(b['cumulative']),
                      'left': sat(b['deposit'] - b['cumulative']), 'received': b['seq'], 'status': b['status'],
                      'close_at': b['close_at']}

    def _payment_channel(self, b):
        return {'chain': self.chain_id, 'contract': self.contract, 'id': b['channel'],
                'binding': sign_binding(self._need_alias(), b['channel'], self.pubkey)}

    def payment_channel(self, provider, stream):
        """The 34602's payment_channel for a stream we buy (Lite re-sends its subscription), or None."""
        b = self._open_buy(provider, stream)
        return self._payment_channel(b) if b else None

    async def buy(self, entry, deposit=None, wallet_key=None):
        """Buy a listed stream (`entry`: its discovered catalog entry): the wallet (`wallet_key`, unlocked) opens a
        channel to the payee with `deposit` (wei; 100 observations by default), then the 34602 names it with the
        alias's binding to our Nostr key. → its view"""
        listing = listing_of(entry, self.chain_id, self.contract, self.token)
        if not listing:
            raise MarketError('That stream is not for sale on the Satori contract.')
        provider, stream = entry['nostr_pubkey'], entry['stream_name']
        was = self._open_buy(provider, stream)
        if was:
            return self._view(was['channel'])
        amount = deposit if deposit is not None else listing['price'] * DEPOSIT_OBS
        if amount < listing['price']:
            raise MarketError(f'The deposit must cover at least one observation ({sat(listing["price"])} SATORI).')
        if not wallet_key:
            raise MarketError('Unlock the wallet first')
        alias = self._need_alias()
        try:
            r = await asyncio.to_thread(self._chain().open_channel, wallet_key, Account.from_key(alias).address,
                                        listing['payee'], amount)
        except Exception as e:
            raise MarketError(channel_error_text(e)) from e
        cid = r['id'].lower()
        self.m['buys'][cid] = {'pubkey': provider, 'stream': stream, 'name': entry.get('name') or '', 'channel': cid,
                               'hash': r['hash'], 'price': listing['price'], 'deposit': amount, 'cumulative': 0,
                               'seq': None, 'status': 'open', 'close_at': None, 'at': self._t()}
        await self._save()
        await self.send.subscription(provider, stream, self._payment_channel(self.m['buys'][cid]))
        return self._view(cid)

    async def received(self, provider, stream, seq):
        """A delivery to us of a stream we buy (already decrypted by satorilib): paid with the alias's next
        voucher (never past the deposit), sent in a 34603 for seq + 1. → the voucher, or None"""
        b = self._open_buy(provider, stream)
        if not b or (b['seq'] is not None and seq <= b['seq']):
            return None
        b['seq'] = seq
        nxt = b['cumulative'] + b['price']
        if nxt > b['deposit']:
            await self._save()
            return None
        signature = sign_voucher(self._need_alias(), self.chain_id, b['channel'], nxt, self.contract)
        b['cumulative'] = nxt
        await self._save()  # kept before it is sent: a restart never signs it again
        voucher = {'channel': b['channel'], 'cumulative': str(nxt), 'signature': signature}
        try:
            await self.send.payment(provider, stream, seq + 1, b['price'] // SAT, voucher)
        except Exception as e:
            logger.warning('market: payment for %s failed: %s', stream, e)
        return voucher

    async def stop(self, provider, stream):
        """Stop: the alias asks to close the channel (no Unlock), then Lite's unsubscribe; the refund comes
        after the 7-day grace period. → its view"""
        b = self._open_buy(provider, stream)
        if not b:
            raise MarketError(f'Not buying {stream} from {provider[:8]}….')
        chans = self._chain()
        try:
            await asyncio.to_thread(chans.request_close, self._need_alias(), b['channel'])
        except Exception as e:
            raise MarketError(channel_error_text(e)) from e
        ch = await asyncio.to_thread(chans.read_channel, b['channel'])
        b['status'], b['close_at'] = 'closing', (ch or {}).get('grace_ends_at')
        await self._save()
        await self.send.unsubscribe(provider, stream)
        return self._view(b['channel'])

    def buying(self, provider, stream) -> bool:
        return self._open_buy(provider, stream) is not None

    # ── the hourly pass ──────────────────────────────────────────────────

    async def pass_(self):
        """Each sale's channel read again (its deposit, what is settled, a close asked: no more deliveries,
        closed: gone), then at most once a day one settleMany for every voucher above what is settled; each
        bought channel whose grace period has ended is withdrawn (the refund to the wallet)."""
        try:
            sales = list(self.m['sales'].values())
            closing = [b for b in self.m['buys'].values() if b['status'] == 'closing']
            if not sales and not closing:
                return
            chans = self._chain()
            for s in sales:
                ch = await asyncio.to_thread(chans.read_channel, s['channel'])
                if not ch or ch['closed']:
                    del self.m['sales'][s['channel']]
                    continue
                s['deposit'], s['settled'] = ch['deposit'], ch['settled']
                s['closing'] = (s['closing'] or bool(ch['close_requested_at'])
                                or not self._price(s['stream']))
            await self._collect()
            at = await asyncio.to_thread(chans.chain_time) if closing else 0
            for b in closing:
                ch = await asyncio.to_thread(chans.read_channel, b['channel'])
                if ch and ch['grace_ends_at'] is not None:
                    b['close_at'] = ch['grace_ends_at']
                if ch and not ch['closed'] and not (ch['grace_ends_at'] is not None and ch['grace_ends_at'] <= at):
                    continue  # its grace period runs
                if ch and not ch['closed']:
                    await asyncio.to_thread(chans.withdraw, self._need_alias(), b['channel'])
                del self.m['buys'][b['channel']]  # refunded to the wallet
            await self._save()
            self.last_error = None
        except Exception as e:
            self.last_error = channel_error_text(e)
            logger.warning('market: pass failed: %s', self.last_error)

    def status(self):
        """What we sell (each buyer's channel, delivered, paid, settled) and what we buy, amounts in SATORI.
        Call it on the network loop."""
        prices = self._prices()
        streams = sorted({s['stream'] for s in self.m['sales'].values()} | {st for st, p in prices.items() if p})
        return {
            'selling': [{'stream': st, 'price': sat(int(prices.get(st) or 0) * SAT),
                         'buyers': [{'pubkey': s['buyer'], 'channel': s['channel'], 'delivered': s['delivered'],
                                     'paid': sat(s['cumulative'] - s['base']), 'settled': sat(s['settled']),
                                     'deposit': sat(s['deposit']), 'closing': s['closing']}
                                    for s in self._sales_of(st)]} for st in streams],
            'bought': [self._view(k) for k in self.m['buys']],
            'error': self.last_error,
        }
