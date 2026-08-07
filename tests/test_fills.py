"""Direction from the public tape.

The buyer/seller convention is the single most invertible assumption in the
system: sign it wrong and every position flips, so the map puts long
liquidations above the mark instead of below.
"""

from __future__ import annotations

from nat2.features.fills import deltas, flatten, participation, signed_parties


def _trade(buyer="0xb", seller="0xs", coin="BTC", px="100", sz="2", side="A"):
    return {"coin": coin, "px": px, "sz": sz, "side": side, "users": [buyer, seller]}


def test_users_zero_is_the_buyer():
    assert signed_parties(_trade()) == [("0xb", 2.0), ("0xs", -2.0)]


def test_aggressor_side_does_not_set_direction():
    # trade.side is who crossed the spread, not who went long. Using it to
    # sign positions inverts every passive fill.
    assert signed_parties(_trade(side="A")) == signed_parties(_trade(side="B"))


def test_deltas_net_out_and_carry_notional():
    trades = [
        _trade(buyer="0xa", seller="0xb", sz="3"),
        _trade(buyer="0xb", seller="0xa", sz="1"),
    ]
    by_address = {d.address: d for d in deltas(trades)}
    assert by_address["0xa"].dsz == 2.0
    assert by_address["0xb"].dsz == -2.0
    assert by_address["0xa"].fills == 2
    assert by_address["0xa"].notional == 400.0


def test_deltas_can_restrict_to_the_registry():
    trades = [_trade(buyer="0xin", seller="0xout")]
    assert {d.address for d in deltas(trades, addresses={"0xin"})} == {"0xin"}
    # Without a filter the whole venue is reconstructable, not just the registry.
    assert {d.address for d in deltas(trades)} == {"0xin", "0xout"}


def test_flatten_unpacks_worm_records():
    records = [{"payload": [_trade(), _trade()]}, {"payload": {"not": "a list"}}]
    assert len(flatten(records)) == 2


def test_participation_measures_tape_share():
    trades = [_trade(buyer="0xa", sz="1"), _trade(buyer="0xz", seller="0xy", sz="3")]
    stats = participation(trades, {"0xa"})
    assert stats["trades"] == 2 and stats["registry_trades"] == 1
    assert stats["notional_frac"] == 100.0 / 400.0
