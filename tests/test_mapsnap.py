"""Persisting the map as it was believed at the time.

The registry keeps only the present, so without this a model would train on
liquidation features that never existed when the decision was made, and
`gate map` would score liquidations against a map built after they fired.

Two rules carry the weight: a snapshot of nothing must never be written, and
reading the history back must never return a map from the future.
"""

from __future__ import annotations

from nat2.core.registry import Registry
from nat2.features.liqmap import build, nearest
from nat2.features.liqmath import Position
from nat2.io.mapsnap import STREAM, as_of, iter_snapshots, series, snapshot, summarise
from nat2.io.worm import WormWriter, read_records


def _position(address="0xa", coin="BTC", liq=95.0, szi=1.0) -> Position:
    return Position(address=address, coin=coin, szi=szi, mark=100.0, max_leverage=40,
                    margin_type="cross", account_value=50.0, maint_margin=1.0,
                    liquidation_px=liq)


def _ctx_record(t_ingest, coins):
    return {"t_ingest": t_ingest, "payload": [
        {"universe": [{"name": c} for c in coins]},
        [{"markPx": str(v["mark"]), "oraclePx": str(v["mark"]),
          "openInterest": str(v.get("oi", 100)), "funding": "0", "dayNtlVlm": "0"}
         for v in coins.values()],
    ]}


def _write_contexts(root, coins, t_ingest=1):
    with WormWriter(root, "hl.assetctxs") as writer:
        writer.write(_ctx_record(t_ingest, coins)["payload"], None, t_ingest)


# --- nearest cluster -------------------------------------------------------

def test_nearest_finds_the_closest_cluster_each_side():
    positions = [_position(address="0xu", szi=-1.0, liq=101.0),
                 _position(address="0xd", liq=99.0)]
    near = nearest(build(positions, "BTC", 100.0, 1000.0))
    assert near["up_px"] > 100.0 > near["down_px"]
    assert near["up_dist"] > 0 > near["down_dist"]


def test_nearest_ignores_mass_below_the_threshold():
    # A lone small position is not a cluster; d_near would otherwise measure
    # noise rather than a magnet.
    near = nearest(build([_position(liq=99.0)], "BTC", 100.0, 1000.0), min_notional=1e9)
    assert near["down_px"] is None and near["down_dist"] is None


def test_nearest_on_an_empty_map_is_all_none():
    near = nearest(build([], "BTC", 100.0, 1000.0))
    assert near["up_px"] is None and near["down_px"] is None


# --- the summary -----------------------------------------------------------

def test_summary_carries_the_named_features():
    liqmap = build([_position(liq=99.0)], "BTC", 100.0, 1000.0)
    row = summarise(liqmap, min_notional=0.0)
    assert row["coin"] == "BTC"
    assert set(row) >= {"mark", "coverage", "up", "down", "imb", "near", "published_frac"}
    assert row["imb"]["0.02"] == 1.0


# --- writing ---------------------------------------------------------------

def test_a_snapshot_is_written_per_coin(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([
        (_position(coin="BTC", liq=99.0), "published"),
        (_position(coin="ETH", liq=99.0), "published"),
    ])
    _write_contexts(tmp_path, {"BTC": {"mark": 100}, "ETH": {"mark": 100}})

    result = snapshot(registry, tmp_path)
    assert result["coins"] == 2

    rows = iter_snapshots(read_records(tmp_path, STREAM))
    assert {r["coin"] for r in rows} == {"BTC", "ETH"}
    assert all(r["t_ingest"] == result["t_ingest"] for r in rows)


def test_a_map_of_nothing_is_never_written(tmp_path):
    # An empty snapshot would enter the history as a genuine observation that
    # no clusters existed -- the same lie the sweep guard exists to stop.
    registry = Registry(tmp_path / "r.sqlite")
    _write_contexts(tmp_path, {"BTC": {"mark": 100}})
    assert snapshot(registry, tmp_path)["skipped"] == "registry has no positions"
    assert list(read_records(tmp_path, STREAM)) == []


def test_no_contexts_means_no_snapshot(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([(_position(), "published")])
    assert "skipped" in snapshot(registry, tmp_path)
    assert list(read_records(tmp_path, STREAM)) == []


def test_a_coin_without_a_mark_is_skipped_not_guessed(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([
        (_position(coin="BTC", liq=99.0), "published"),
        (_position(coin="NOMARK", liq=99.0), "published"),
    ])
    _write_contexts(tmp_path, {"BTC": {"mark": 100}})
    assert snapshot(registry, tmp_path)["coins"] == 1
    assert {r["coin"] for r in iter_snapshots(read_records(tmp_path, STREAM))} == {"BTC"}


def test_snapshots_accumulate_rather_than_overwrite(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([(_position(liq=99.0), "published")])
    _write_contexts(tmp_path, {"BTC": {"mark": 100}})

    snapshot(registry, tmp_path)
    registry.replace_positions([(_position(liq=90.0), "published")])
    snapshot(registry, tmp_path)

    rows = series(read_records(tmp_path, STREAM), "BTC")
    assert len(rows) == 2, "history is the point; a snapshot must not replace its predecessor"
    assert rows[0]["t_ingest"] <= rows[1]["t_ingest"]


# --- reading back ----------------------------------------------------------

def test_as_of_never_returns_a_map_from_the_future():
    rows = [{"t_ingest": 10, "coin": "BTC"}, {"t_ingest": 30, "coin": "BTC"}]
    assert as_of(rows, 20)["t_ingest"] == 10
    assert as_of(rows, 30)["t_ingest"] == 30
    assert as_of(rows, 9) is None


def test_as_of_on_an_empty_history_is_none():
    assert as_of([], 100) is None


def test_iter_snapshots_ignores_malformed_records():
    assert iter_snapshots([{"t_ingest": 1, "payload": "junk"}]) == []
    assert iter_snapshots([{"payload": {"coins": [{"coin": "BTC"}]}}]) == []
    assert iter_snapshots([{"t_ingest": 1, "payload": {"coins": ["junk", {}]}}]) == []


def test_iter_snapshots_orders_by_arrival():
    records = [
        {"t_ingest": 30, "payload": {"coins": [{"coin": "BTC"}]}},
        {"t_ingest": 10, "payload": {"coins": [{"coin": "BTC"}]}},
    ]
    assert [r["t_ingest"] for r in iter_snapshots(records)] == [10, 30]
