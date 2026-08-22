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


# --- v2: wider, finer, and invisible to v1 (TASK_2/17) ---------------------

def test_v2_carries_wide_bands_sparse_buckets_and_the_liquidity_fields():
    from nat2.features.liqmap import WIDE_BANDS, build
    from nat2.io.mapsnap import V2_BUCKET_PCT, V2_SPAN, summarise_wide

    # One cluster at -1% (inside v1's span) and one at +12% (outside it entirely).
    positions = [_position(liq=99.0, szi=2.0), _position(address="0xb", liq=112.0, szi=3.0)]
    wide = build(positions, "BTC", 100.0, 1000.0, bands=WIDE_BANDS,
                 bucket_pct=V2_BUCKET_PCT, span=V2_SPAN)
    row = summarise_wide(wide, day_volume=5e6)
    assert row["bands"] == ["0.005", "0.01", "0.02", "0.05", "0.1", "0.2", "0.3"]
    assert row["day_volume"] == 5e6 and row["oi_notional"] == 1000.0
    assert row["bucket_pct"] == V2_BUCKET_PCT and row["span"] == V2_SPAN
    assert row["down"]["0.01"] == 200.0 and row["up"]["0.05"] == 0.0    # v1 could not see the +12% cluster
    assert row["up"]["0.2"] == 300.0 and row["up_cross"]["0.2"] == 300.0
    assert row["outside_span"] == 0                                      # +-30% holds it; +-10% would not
    near_b, far_b = row["buckets"]
    assert len(row["buckets"]) == 2                                      # sparse: only what carries mass
    assert near_b == [-0.01, 200.0, 200.0, 1]                            # -1%: an exact bucket edge
    # +12% lands in the bucket below its own edge, because `build` places on
    # `low <= price < high` and 100*(1+0.12) is 112.00000000000001 in binary. The bucket
    # is a display choice (band totals come from exact prices), so this is pinned, not fixed.
    assert far_b[1:] == [300.0, 300.0, 1] and 0.12 - V2_BUCKET_PCT <= far_b[0] <= 0.12

    narrow = build(positions, "BTC", 100.0, 1000.0)                      # v1 parameters, unchanged
    assert narrow.outside_span == 1 and summarise(narrow)["up"]["0.05"] == 0.0


def test_v1_rows_are_untouched_and_v2_is_a_separate_stream(tmp_path):
    from nat2.io.mapsnap import STREAM_V2

    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([(_position(liq=99.0), "published"),
                                (_position(address="0xb", liq=112.0), "published")])
    _write_contexts(tmp_path, {"BTC": {"mark": 100.0}})
    result = snapshot(registry, tmp_path)
    assert result["coins"] == 1 and result["v2_coins"] == 1

    v1 = series(read_records(tmp_path, STREAM), "BTC")[0]
    v2 = series(read_records(tmp_path, STREAM_V2), "BTC")[0]
    assert set(v1) == {"t_ingest", "coin", "mark", "coverage", "notional", "cross_notional",
                       "published_frac", "positions", "skipped", "outside_span", "oi_notional",
                       "up", "down", "imb", "imb_cross", "near"}          # v1 gained nothing
    assert list(v1["up"]) == ["0.005", "0.01", "0.02", "0.05"] and v1["t_ingest"] == v2["t_ingest"]
    assert set(v2) - set(v1) == {"bands", "bucket_pct", "span", "buckets", "day_volume",
                                 "up_cross", "down_cross"}
    # The v1 reader cannot see v2, so `gate map` and `gate magnet` keep refusing on a v2-only store.
    (tmp_path / STREAM).rename(tmp_path / "moved-away")
    assert series(read_records(tmp_path, STREAM), "BTC") == []
    assert len(series(read_records(tmp_path, STREAM_V2), "BTC")) == 1
