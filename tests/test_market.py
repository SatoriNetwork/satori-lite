"""Paid streams on Base (satorineuron/market.py), the browser neuron's
src/neuron/market.test.js in Python: a seller and a buyer, each its own market,
over a fake relay (what one sends is handed to the other) and a fake of the
channel contract with the real voucher and binding signatures: the listing,
the channel accepted from the chain, deliveries to paying buyers only,
vouchers checked, the daily settle, stop and the refund after the grace
period; forged channels, bindings and vouchers refused."""
import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("web3")

from eth_account import Account  # noqa: E402

from satorilib.chain.channels import (  # noqa: E402
    ADDRESS, CLOSE_GRACE_S, ChannelError, sign_binding, sign_voucher, verify_voucher)
from satorilib.satori_nostr.models import (  # noqa: E402
    DatastreamObservation, InboundPayment, InboundSubscription, PaymentNotification, SubscriptionAnnouncement)

_spec = importlib.util.spec_from_file_location(
    '_t_market', Path(__file__).parent.parent / 'neuron-lite' / 'satorineuron' / 'market.py')
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

CHAIN = 84532
TOKEN = '0x4dee947383d5C9c8E421F55Fe3518a29324aE469'
S_NOSTR, B_NOSTR, X_NOSTR = '5' * 64, '6' * 64, '7' * 64  # the pubkeys (x-only hex) as the markets see them
S_ID, B_ID, X_ID = '0x' + '11' * 32, '0x' + '22' * 32, '0x' + '33' * 32
S_VAULT, B_VAULT = '0x' + '44' * 32, '0x' + '55' * 32
addr = lambda key: Account.from_key(key).address
STREAM = 'walk_pred'
SAT = mk.SAT
PRICE = 1000 * SAT  # 1000 satoshis: 0.00001 SATORI


class FakeChannels:
    """The contract, held in memory, behind satorilib's PaymentChannels API, with the real voucher check."""

    def __init__(self, clock):
        self.map, self.calls, self.refuse, self.clock, self.n = {}, [], None, clock, 0

    def open_channel(self, vault_key, identity_address, payee, deposit, salt=None):
        self.calls.append('open')
        if self.refuse:
            raise self.refuse
        self.n += 1
        cid = '0x' + f'{self.n:064x}'
        self.map[cid] = {'id': cid, 'payer': addr(vault_key), 'payee': payee, 'token': TOKEN, 'signer': identity_address,
                         'deposit': deposit, 'settled': 0, 'close_requested_at': None, 'closed': False, 'grace_ends_at': None}
        return {'id': cid, 'hash': f'0xopen{self.n}', 'permit': True}

    def read_channel(self, cid):
        c = self.map.get(cid)
        return dict(c) if c else None

    def settle_many(self, identity_key, entries):
        self.calls.append(('settle_many', len(entries)))
        settled = []
        for e in entries:
            c = self.map[e['id']]
            ok = verify_voucher(c['signer'], e['signature'], CHAIN, e['id'], e['cumulative'])
            target = min(e['cumulative'], c['deposit'])
            if not ok or c['closed'] or target <= c['settled']:
                continue
            c['settled'] = target
            settled.append({'id': e['id'], 'cumulative': e['cumulative'], 'paid': 0, 'settled': target})
        return {'hash': '0xsettle', 'settled': settled, 'skipped': []}

    def request_close(self, identity_key, cid):
        self.calls.append('request_close')
        c = self.map[cid]
        c['close_requested_at'] = c['close_requested_at'] or self.clock[0]
        c['grace_ends_at'] = c['close_requested_at'] + CLOSE_GRACE_S

    def withdraw(self, identity_key, cid):
        c = self.map[cid]
        if self.clock[0] < c['grace_ends_at']:
            raise ChannelError("The 7-day grace period after the close request hasn't ended yet.", reason='CloseNotReady')
        self.calls.append('withdraw')
        c['closed'] = True

    def chain_time(self):
        return self.clock[0]


class MemoryDb:
    def __init__(self):
        self.kv = {}

    def get_meta(self, key):
        return self.kv.get(key)

    def set_meta(self, key, value):
        self.kv[key] = value


class Relay:
    """What each market sends, in order; deliver() answers whether a relay took it."""

    def __init__(self, author):
        self.author, self.sent, self.takes = author, [], True

    async def subscription(self, provider, stream, payment_channel):
        self.sent.append(('subscription', provider, stream, payment_channel))

    async def unsubscribe(self, provider, stream):
        self.sent.append(('unsubscribe', provider, stream))

    async def payment(self, provider, stream, seq, amount_sats, voucher):
        self.sent.append(('payment', provider, stream, seq, amount_sats, voucher))

    async def deliver(self, observation, buyer):
        self.sent.append(('deliver', observation, buyer))
        return self.takes


def run(coro):
    return asyncio.run(coro)


class World:
    def __init__(self):
        self.clock = [1_792_100_000]
        self.chain = FakeChannels(self.clock)
        self.prices = {}
        self.vault_open = True
        self.sdb, self.bdb = MemoryDb(), MemoryDb()
        self.srelay, self.brelay = Relay(S_NOSTR), Relay(B_NOSTR)
        self.seller, self.buyer = self.make('s'), self.make('b')

    def make(self, who, db=None):
        s = who == 's'
        return mk.Market(
            db=db or (self.sdb if s else self.bdb), nostr_pubkey=S_NOSTR if s else B_NOSTR,
            identity_key=lambda: S_ID if s else B_ID, vault_address=lambda: addr(S_VAULT if s else B_VAULT),
            chain=lambda: self.chain, send=self.srelay if s else self.brelay, chain_id=CHAIN, contract=ADDRESS,
            token=TOKEN, prices=lambda: dict(self.prices) if s else {}, now=lambda: self.clock[0])

    def entry(self, listing='default'):
        """The seller's listing as the buyer finds it: its catalog entry (Lite's discovered stream dict)."""
        listing = self.seller.listing(STREAM) if listing == 'default' else listing
        return {'stream_name': STREAM, 'nostr_pubkey': S_NOSTR, 'name': 'Predictions for Walk',
                'price_per_obs': (listing or {}).get('price_per_obs', 0),
                'metadata': {'source_stream_name': 'walk', 'market': (listing or {}).get('market')}}

    def last(self, relay, kind):
        return [x for x in relay.sent if x[0] == kind][-1]

    async def subscription_to_seller(self, item=None, author=B_NOSTR):
        _, provider, stream, pc = item or self.last(self.brelay, 'subscription')
        await self.seller.on_subscription(InboundSubscription(
            subscription=SubscriptionAnnouncement(subscriber_pubkey=author, stream_name=stream, nostr_pubkey=provider,
                                                  timestamp=self.clock[0], payment_channel=pc),
            unsubscribe=False, event_id='e'))

    async def payment_to_seller(self, item, author=B_NOSTR):
        _, provider, stream, seq, amount, voucher = item
        await self.seller.on_payment(InboundPayment(payment=PaymentNotification(
            from_pubkey=author, to_pubkey=provider, stream_name=stream, seq_num=seq, amount_sats=amount,
            timestamp=self.clock[0], voucher=voucher), event_id='p'))

    async def observe(self, seq, value=None, pay=True):
        """One observation of the seller's stream, delivered and (when the buyer gets it) paid; the payment
        handed to the seller. → (deliveries, payments)"""
        before_s, before_b = len(self.srelay.sent), len(self.brelay.sent)
        obs = DatastreamObservation(stream_name=STREAM, timestamp=self.clock[0], value=value or str(100 + seq), seq_num=seq)
        await self.seller.deliver(obs)
        out = [x for x in self.srelay.sent[before_s:] if x[0] == 'deliver' and self.srelay.takes]
        for _, o, buyer in out:
            if buyer == B_NOSTR:
                await self.buyer.received(S_NOSTR, o.stream_name, o.seq_num)
        payments = [x for x in self.brelay.sent[before_b:] if x[0] == 'payment']
        if pay:
            for p in payments:
                await self.payment_to_seller(p)
        self.clock[0] += 60
        return out, payments

    def sale(self):
        return list(json.loads(self.sdb.kv['market'])['sales'].values())[0]

    async def bought(self, deposit=5 * PRICE):
        self.prices[STREAM] = 1000
        v = await self.buyer.buy(self.entry(), deposit, vault_key=B_VAULT)
        await self.subscription_to_seller()
        return v


@pytest.fixture
def w():
    world = World()
    run(world.seller.load())
    run(world.buyer.load())
    return world


# ── pieces ─────────────────────────────────────────────────────────────

def test_a_listing_is_lites_price_per_obs_on_this_chains_satori_contract():
    m = {'chain': CHAIN, 'contract': ADDRESS, 'token': TOKEN, 'payee': addr(S_VAULT)}
    of = lambda e: mk.listing_of(e, CHAIN, ADDRESS, TOKEN)
    assert of({'price_per_obs': 1000, 'metadata': {'market': m}}) == {**m, 'price': 1000 * SAT}
    for bad in [{'chain': 8453}, {'contract': '0x' + '1' * 40}, {'token': '0x' + '2' * 40}, {'payee': 'nope'}]:
        assert of({'price_per_obs': 1000, 'metadata': {'market': {**m, **bad}}}) is None, bad
    for sats in [0, -5, 1.5, '1000', 2 ** 53, None, True]:
        assert of({'price_per_obs': sats, 'metadata': {'market': m}}) is None, sats
    assert of({'price_per_obs': 1000, 'metadata': None}) is None


def test_deliverable_the_first_free_then_while_the_voucher_covers_every_one_delivered_and_the_deposit_one_more():
    s = {'base': 0, 'delivered': 0, 'price': 10, 'cumulative': 0, 'deposit': 30, 'closing': False}
    assert mk.deliverable(s)
    assert not mk.deliverable({**s, 'delivered': 1})
    assert mk.deliverable({**s, 'delivered': 1, 'cumulative': 10})
    assert not mk.deliverable({**s, 'delivered': 3, 'cumulative': 30})  # the deposit is spent
    assert not mk.deliverable({**s, 'closing': True})
    assert mk.deliverable({**s, 'base': 50, 'cumulative': 50, 'deposit': 60})  # accepted with 50 settled


# ── selling and buying ─────────────────────────────────────────────────

def test_the_listing_the_vault_paid_satori_the_satori_contract_free_again_none(w):
    w.prices[STREAM] = 1000
    assert w.seller.listing(STREAM) == {'price_per_obs': 1000, 'market': {
        'chain': CHAIN, 'contract': ADDRESS, 'token': TOKEN, 'payee': addr(S_VAULT)}}
    assert w.seller.status()['selling'] == [{'stream': STREAM, 'price': '0.00001', 'buyers': []}]
    w.prices[STREAM] = 0
    assert w.seller.listing(STREAM) is None


def test_buy_the_vault_opens_a_channel_the_34602_names_it_with_the_binding_the_seller_accepts_it_from_the_chain(w):
    v = run(w.bought())
    assert {k: v[k] for k in ('pubkey', 'stream', 'price', 'deposit', 'paid', 'status')} == {
        'pubkey': S_NOSTR, 'stream': STREAM, 'price': '0.00001', 'deposit': '0.00005', 'paid': '0', 'status': 'open'}
    ch = w.chain.map[v['channel']]
    assert (ch['payer'], ch['payee'], ch['signer'], ch['deposit']) == (addr(B_VAULT), addr(S_VAULT), addr(B_ID), 5 * PRICE)
    _, provider, stream, pc = w.last(w.brelay, 'subscription')
    assert (provider, stream) == (S_NOSTR, STREAM)
    assert {k: pc[k] for k in ('chain', 'contract', 'id')} == {'chain': CHAIN, 'contract': ADDRESS, 'id': v['channel']}
    assert pc['binding'] == sign_binding(B_ID, v['channel'], B_NOSTR)
    assert w.seller.status()['selling'][0]['buyers'] == [{'pubkey': B_NOSTR, 'channel': v['channel'], 'delivered': 0,
                                                          'paid': '0', 'settled': '0', 'deposit': '0.00005', 'closing': False}]
    assert w.buyer.payment_channel(S_NOSTR, STREAM) == pc  # the same, for Lite's re-subscribe
    # again while it is open: the same channel, nothing opened
    assert run(w.buyer.buy(w.entry(), 5 * PRICE, vault_key=B_VAULT))['channel'] == v['channel']
    assert w.chain.calls.count('open') == 1


def test_each_observation_to_the_buyer_the_first_free_each_one_received_paid_with_the_next_cumulative_voucher(w):
    v = run(w.bought())
    out, payments = run(w.observe(1, '101.5'))
    assert [(x[1].value, x[2]) for x in out] == [('101.5', B_NOSTR)]
    [(_, provider, stream, seq, amount, voucher)] = payments
    assert (provider, stream, seq, amount) == (S_NOSTR, STREAM, 2, 1000)  # Lite's 34603 for seq + 1, the price in sats
    assert voucher['channel'] == v['channel'] and voucher['cumulative'] == str(PRICE)
    assert verify_voucher(addr(B_ID), voucher['signature'], CHAIN, v['channel'], PRICE)
    for k in range(2, 5):
        assert len(run(w.observe(k))[0]) == 1
    b = w.seller.status()['selling'][0]['buyers'][0]
    assert (b['delivered'], b['paid']) == (4, '0.00004')
    bought = w.buyer.status()['bought'][0]
    assert (bought['paid'], bought['left'], bought['received']) == ('0.00004', '0.00001', 4)
    # a delivery seen again (a relay replays it) is not paid twice
    assert run(w.buyer.received(S_NOSTR, STREAM, 1)) is None
    assert w.buyer.status()['bought'][0]['paid'] == '0.00004'


def test_a_buyer_that_doesnt_pay_gets_nothing_more_the_deposits_end_stops_the_seller_and_the_buyer_alike(w):
    run(w.bought(3 * PRICE))
    run(w.observe(1, '1', pay=False))
    assert run(w.observe(2))[0] == []  # the voucher for 1 never came
    run(w.payment_to_seller(w.last(w.brelay, 'payment')))  # it comes late
    assert len(run(w.observe(3))[0]) == 1
    assert len(run(w.observe(4))[0]) == 1
    assert w.buyer.status()['bought'][0]['left'] == '0'
    assert run(w.observe(5))[0] == []  # 3 delivered and paid: the deposit holds no fourth


def test_a_relay_that_took_no_delivery_not_owed_so_the_next_one_still_goes(w):
    run(w.bought())
    w.srelay.takes = False
    run(w.observe(1))
    assert w.sale()['delivered'] == 0
    w.srelay.takes = True
    assert len(run(w.observe(2))[0]) == 1


def test_forgeries_refused(w):
    w.prices[STREAM] = 1000
    v = run(w.buyer.buy(w.entry(), 5 * PRICE, vault_key=B_VAULT))
    sub = w.last(w.brelay, 'subscription')
    pc = sub[3]
    run(w.subscription_to_seller(sub, author=X_NOSTR))  # the buyer's channel claimed by another Nostr key
    run(w.subscription_to_seller(('subscription', S_NOSTR, STREAM,
                                  {**pc, 'binding': sign_binding(X_ID, v['channel'], X_NOSTR)}), author=X_NOSTR))
    assert w.seller.status()['selling'][0]['buyers'] == []
    # a channel paying someone else
    other = w.chain.open_channel(B_VAULT, addr(B_ID), addr(X_ID), 5 * PRICE)
    run(w.subscription_to_seller(('subscription', S_NOSTR, STREAM,
                                  {**pc, 'id': other['id'], 'binding': sign_binding(B_ID, other['id'], B_NOSTR)})))
    # another chain or contract
    run(w.subscription_to_seller(('subscription', S_NOSTR, STREAM, {**pc, 'chain': 8453})))
    run(w.subscription_to_seller(('subscription', S_NOSTR, STREAM, {**pc, 'contract': '0x' + '9' * 40})))
    assert w.seller.status()['selling'][0]['buyers'] == []
    # a closing channel
    w.chain.map[v['channel']]['close_requested_at'] = w.clock[0]
    run(w.subscription_to_seller(sub))
    assert w.seller.status()['selling'][0]['buyers'] == []
    w.chain.map[v['channel']]['close_requested_at'] = None
    # the real one
    run(w.subscription_to_seller(sub))
    assert len(w.seller.status()['selling'][0]['buyers']) == 1
    run(w.observe(1, '1', pay=False))

    def pay(voucher, author=B_NOSTR):
        run(w.payment_to_seller(('payment', S_NOSTR, STREAM, 2, 1000, voucher), author=author))

    def voucher(key, cumulative):
        return {'channel': v['channel'], 'cumulative': str(cumulative),
                'signature': sign_voucher(key, CHAIN, v['channel'], cumulative)}

    pay(voucher(X_ID, PRICE))  # not the channel's signer
    pay(voucher(B_ID, 9 * PRICE))  # past the deposit
    pay(voucher(B_ID, PRICE), author=X_NOSTR)  # from another Nostr key
    pay({**voucher(B_ID, PRICE), 'cumulative': 'lots'})
    assert w.sale()['cumulative'] == 0
    pay(voucher(B_ID, PRICE))
    assert w.sale()['cumulative'] == PRICE
    pay(voucher(B_ID, PRICE))  # no rise
    pay({**voucher(B_ID, 2 * PRICE), 'signature': '0x' + '1' * 130})
    assert w.sale()['cumulative'] == PRICE


def test_the_pass_settles_at_most_once_a_day_a_close_request_stops_deliveries_settled_in_the_next_daily_settle(w):
    settles = lambda: len([c for c in w.chain.calls if c[0] == 'settle_many'])
    v = run(w.bought())
    run(w.observe(1))
    run(w.observe(2))
    run(w.seller.pass_())
    assert w.chain.map[v['channel']]['settled'] == 2 * PRICE
    assert w.seller.status()['selling'][0]['buyers'][0]['settled'] == '0.00002'
    run(w.observe(3))
    w.clock[0] += 3600  # an hour later: something due, but no transaction until a day has passed
    run(w.seller.pass_())
    assert (settles(), w.chain.map[v['channel']]['settled']) == (1, 2 * PRICE)
    w.chain.map[v['channel']]['close_requested_at'] = w.clock[0]  # asked on chain: no more deliveries, still owed
    run(w.seller.pass_())
    assert run(w.observe(4))[0] == []
    assert len(w.seller.status()['selling'][0]['buyers']) == 1
    w.clock[0] += mk.SETTLE_EVERY_S
    run(w.seller.pass_())
    assert (settles(), w.chain.map[v['channel']]['settled']) == (2, 3 * PRICE)
    assert w.seller.status()['selling'][0]['buyers'] == []  # nothing more due: it goes
    w.clock[0] += mk.SETTLE_EVERY_S
    run(w.seller.pass_())
    assert settles() == 2  # nothing due: no transaction


def test_stop_the_identity_asks_to_close_lites_unsubscribe_the_refund_after_the_grace_period_in_the_pass(w):
    v = run(w.bought())
    run(w.observe(1))
    run(w.observe(2))
    s = run(w.buyer.stop(S_NOSTR, STREAM))
    assert (s['status'], s['close_at'], s['paid']) == ('closing', w.clock[0] + CLOSE_GRACE_S, '0.00002')
    assert w.last(w.brelay, 'unsubscribe') == ('unsubscribe', S_NOSTR, STREAM)
    assert w.buyer.payment_channel(S_NOSTR, STREAM) is None
    run(w.seller.on_subscription(InboundSubscription(subscription=SubscriptionAnnouncement(
        subscriber_pubkey=B_NOSTR, stream_name=STREAM, nostr_pubkey=S_NOSTR, timestamp=w.clock[0]),
        unsubscribe=True, event_id='u')))
    assert w.seller.status()['selling'][0]['buyers'][0]['closing'] is True
    assert run(w.observe(3))[0] == []
    run(w.seller.pass_())  # the first settle: due at once
    assert w.chain.map[v['channel']]['settled'] == 2 * PRICE
    assert w.seller.status()['selling'][0]['buyers'] == []
    with pytest.raises(mk.MarketError, match='Not buying'):
        run(w.buyer.stop(S_NOSTR, STREAM))
    run(w.buyer.pass_())  # the grace period runs: nothing
    assert 'withdraw' not in w.chain.calls
    assert w.buyer.status()['bought'][0]['status'] == 'closing'
    w.clock[0] += CLOSE_GRACE_S
    run(w.buyer.pass_())
    assert 'withdraw' in w.chain.calls
    assert w.buyer.status()['bought'] == []  # refunded to the vault


def test_buy_again_after_a_stop_a_new_channel_the_seller_closes_the_old_one_and_serves_the_new(w):
    v1 = run(w.bought())
    run(w.observe(1))
    run(w.buyer.stop(S_NOSTR, STREAM))
    v2 = run(w.buyer.buy(w.entry(), 5 * PRICE, vault_key=B_VAULT))
    assert v2['channel'] != v1['channel']
    run(w.subscription_to_seller())
    assert [(b['channel'], b['closing']) for b in w.seller.status()['selling'][0]['buyers']] == [
        (v1['channel'], True), (v2['channel'], False)]
    run(w.seller.pass_())
    assert [b['channel'] for b in w.seller.status()['selling'][0]['buyers']] == [v2['channel']]
    assert w.chain.map[v1['channel']]['settled'] == PRICE
    assert [b['status'] for b in w.buyer.status()['bought']] == ['closing', 'open']


def test_what_cant_buy_in_plain_words_nothing_kept(w):
    w.prices[STREAM] = 1000
    with pytest.raises(mk.MarketError, match='Unlock the vault first'):
        run(w.buyer.buy(w.entry(), 5 * PRICE, vault_key=None))
    with pytest.raises(mk.MarketError, match='not for sale on the Satori contract'):
        run(w.buyer.buy(w.entry(None), 5 * PRICE, vault_key=B_VAULT))
    with pytest.raises(mk.MarketError, match='at least one observation'):
        run(w.buyer.buy(w.entry(), PRICE - 1, vault_key=B_VAULT))
    w.chain.refuse = ChannelError("Your vault is staked with this neuron, so it can't send SATORI: "
                                  "a channel needs SATORI the vault can send.", reason='Locked')
    with pytest.raises(mk.MarketError, match="Your vault is staked with this neuron, so it can't send SATORI"):
        run(w.buyer.buy(w.entry(), 5 * PRICE, vault_key=B_VAULT))
    assert w.buyer.status()['bought'] == []
    assert [x for x in w.brelay.sent if x[0] == 'subscription'] == []
    # the default deposit: 100 observations
    w.chain.refuse = None
    assert mk.DEPOSIT_OBS == 100
    assert run(w.buyer.buy(w.entry(), vault_key=B_VAULT))['deposit'] == '0.001'  # 100 × 1000 satoshis


def test_kept_a_reload_has_the_buyers_and_what_each_paid_and_what_we_buy(w):
    v = run(w.bought())
    run(w.observe(1))
    s2, b2 = w.make('s'), w.make('b')
    run(s2.load())
    run(b2.load())
    b = s2.status()['selling'][0]['buyers'][0]
    assert (b['channel'], b['delivered'], b['paid']) == (v['channel'], 1, '0.00001')
    bought = b2.status()['bought'][0]
    assert (bought['channel'], bought['paid'], bought['received']) == (v['channel'], '0.00001', 1)
