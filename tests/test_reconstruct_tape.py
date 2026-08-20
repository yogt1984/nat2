"""TASK_2/07: tape position series -- golden, signing property, drift, gaps, determinism."""

import random

import polars as pl

from nat2.core import reconstruct as rc
from nat2.io.worm import ManifestEntry

A, B, C = "0xaaa", "0xbbb", "0xccc"
MS = 1_000_000


def prt(i, buyer, seller, sz, side="A", coin="BTC"):
    """One print. `side` is the aggressor label and must be irrelevant to direction."""
    return {"coin": coin, "side": side, "px": "100", "sz": str(sz), "time": 1000 + i, "tid": i,
            "users": [buyer, seller]}


# 20 prints, 3 wallets, passive fills on both sides, print 9 is a liquidation
# counterparty fill (C is force-sold, B takes the other side). Hand-computed.
TAPE = [prt(0, A, B, 1), prt(1, B, A, 2, "B"), prt(2, C, A, 0.5), prt(3, A, C, 0.5, "B"),
        prt(4, B, C, 3), prt(5, C, B, 1, "B"), prt(6, A, B, 4), prt(7, B, A, 4, "B"),
        prt(8, C, A, 2), prt(9, B, C, 2.5, "A"), prt(10, A, B, 1), prt(11, A, C, 1, "B"),
        prt(12, C, B, 2), prt(13, B, A, 0.25), prt(14, A, B, 0.25, "B"), prt(15, C, A, 1),
        prt(16, B, C, 1, "B"), prt(17, A, B, 3), prt(18, B, A, 3, "B"), prt(19, C, A, 0.75),
        prt(20, A, B, 1, "A", coin="ETH")]
EXPECTED_FINAL = {A: -2.75, B: 3.5, C: -0.75}      # sums to zero, by hand


def test_golden_final_positions_and_path():
    anchors = [rc.Anchor(w, "BTC", 0.0, 0, "published") for w in (A, B, C)]
    f = rc.series(TAPE, "BTC", anchors, 0, 10**18)
    assert f.height == 40 and f["coin"].unique().to_list() == ["BTC"]
    last = f.sort(["ts_ns", "tid"]).group_by("address").last()
    assert {r["address"]: r["szi"] for r in last.iter_rows(named=True)} == EXPECTED_FINAL
    assert f["anchored"].all() and f["anchor_source"].unique().to_list() == ["published"]
    # path spot-checks: A after prints 0,1,2,3 = +1 -2 -0.5 +0.5 = -1
    a = f.filter(pl.col("address") == A).sort("tid")["szi"].to_list()
    assert a[:4] == [1.0, -1.0, -1.5, -1.0]
    # liquidation counterparty print 9: C force-sold 2.5 from flat (0.5-0.5-3+1+2 = 0)
    c9 = f.filter((pl.col("address") == C) & (pl.col("tid") == 9))["szi"][0]
    assert c9 == -2.5


def test_signing_ignores_side_and_conserves_size():
    rng = random.Random(7)
    wallets = [f"0x{i}" for i in range(5)]
    for _ in range(200):
        tape = [prt(i, *rng.sample(wallets, 2), rng.choice([0.1, 1, 2.5]), rng.choice("AB"))
                for i in range(rng.randint(1, 30))]
        flipped = [{**t, "side": "A" if t["side"] == "B" else "B"} for t in tape]
        anchors = [rc.Anchor(w, "BTC", 0.0, 0, "published") for w in wallets]
        f1, f2 = (rc.series(t, "BTC", anchors, 0, 10**18) for t in (tape, flipped))
        assert f1.equals(f2)
        last = f1.sort(["ts_ns", "tid"]).group_by("address").last()
        assert abs(last["szi"].sum()) < 1e-9


def test_unanchored_and_gap_refusal():
    anchors = [rc.Anchor(A, "BTC", 10.0, 0, "published")]
    f = rc.series(TAPE, "BTC", anchors, 0, 10**18)
    assert f.filter(pl.col("address") == A)["anchored"].all()
    assert not f.filter(pl.col("address") != A)["anchored"].any()
    assert f.filter((pl.col("address") == A) & (pl.col("tid") == 0))["szi"][0] == 11.0
    g = rc.series(TAPE, "BTC", anchors, 0, 10**18, gap_free=False)
    assert not g["anchored"].any() and g["anchor_source"].unique().to_list() == ["none"]
    # late published anchor (after window start) is no anchor at all
    late = rc.series(TAPE, "BTC", [rc.Anchor(A, "BTC", 10.0, 5000 * MS, "published")], 0, 10**18)
    assert not late["anchored"].any()


def test_tape_gaps_from_manifest():
    def e(first, last):
        return ManifestEntry("hl.trades", "p", 1, 1, "h", 0, 0, first, last, last)
    hour = int(3600e9)
    entries = [e(0, hour), e(hour + 10, 2 * hour), e(5 * hour, 6 * hour)]
    assert rc.tape_gaps(entries, 0, 6 * hour, 3600.0) == [(2 * hour, 5 * hour)]
    assert rc.tape_gaps(entries, 0, hour, 3600.0) == []
    recs = [{"t_ingest": t * MS} for t in (1000, 1001, 1002, 1008, 1009)]
    assert rc.ingest_silences(recs, 0, 10**18, 0.003) == [(1002 * MS, 1008 * MS)]
    assert not rc.ingest_silences(recs, 0, 10**18, 0.01) and not rc.ingest_silences(recs, 1008 * MS, 10**18, 0.003)


def test_userfills_block_checkpoints_anchor_and_drift():
    # Block at t=1004 holds prints 4 and 5 for B, listed in the WRONG tid order
    # on purpose: startPosition chains 1.0 -(+3)-> 4.0 -(-1)-> 3.0, root is 1.0.
    fills = [{"coin": "BTC", "time": 1004, "tid": 5, "startPosition": "4.0", "sz": "1", "side": "A"},
             {"coin": "BTC", "time": 1004, "tid": 4, "startPosition": "1.0", "sz": "3", "side": "B"},
             {"coin": "BTC", "time": 1013, "tid": 13, "startPosition": "2.5", "sz": "0.25", "side": "B"},
             {"coin": "ETH", "time": 1020, "tid": 20, "startPosition": "9", "sz": "1", "side": "B"}]
    tape = [{**t, "time": 1004} if t["tid"] == 5 else t for t in TAPE]
    cps = rc.checkpoints(fills, B, "BTC")
    assert [(c.ts_ns, c.tid, c.szi_before) for c in cps] == [(1004 * MS, 0, 1.0), (1013 * MS, 0, 2.5)]
    capped = fills + [{"coin": "ETH", "time": 1, "tid": -i, "startPosition": "0", "sz": "1", "side": "B"} for i in range(rc.USERFILLS_CAP)]
    assert [c.ts_ns for c in rc.checkpoints(capped, B, "BTC")] == [1004 * MS, 1013 * MS]   # oldest BTC block isn't the oldest block
    capped = fills + [{"coin": "ETH", "time": 5000, "tid": i, "startPosition": "0", "sz": "1", "side": "B"} for i in range(rc.USERFILLS_CAP)]
    assert [c.ts_ns for c in rc.checkpoints(capped, B, "BTC")] == [1013 * MS]   # oldest block of a capped response is cut
    assert rc.anchors_from_checkpoints(cps) == [rc.Anchor(B, "BTC", 1.0, 1004 * MS, "userfills", 0)]
    assert rc.anchors_from_checkpoints(cps, from_ns=1010 * MS) == [rc.Anchor(B, "BTC", 2.5, 1013 * MS, "userfills", 0)]
    f = rc.series(tape, "BTC", rc.anchors_from_checkpoints(cps), 0, 10**18)
    b = f.filter(pl.col("address") == B).sort("tid")
    assert b.filter(pl.col("tid") < 4)["anchored"].to_list() == [False, False]
    assert b.filter(pl.col("tid") == 5)["szi"][0] == 3.0 and b.filter(pl.col("tid") >= 4)["anchored"].all()
    d = rc.drift_audit(f, cps)
    assert d["compared"] == 1 and d["skipped"] == 1 and d["exact_frac"] == 1.0 and d["max"] == 0.0
    assert rc.drift_audit(f, [rc.Checkpoint(B, "BTC", 1013 * MS, 0, 2.0)])["max"] == 0.5


def test_parquet_bytes_deterministic(tmp_path):
    anchors = [rc.Anchor(w, "BTC", 0.0, 0, "published") for w in (A, B, C)]
    outs = []
    for i in range(2):
        p = tmp_path / f"{i}.parquet"
        rc.series(list(reversed(TAPE)) if i else TAPE, "BTC", anchors, 0, 10**18).write_parquet(p)
        outs.append(p.read_bytes())
    assert outs[0] == outs[1]
