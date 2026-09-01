# 00 — Decisions and scope

**Effort** 30 min · **Blocks** everything · **Status** todo

## What
Settle four things before any code, because each one silently changes what the
picture means.

## How
Write the answers here, then proceed.

1. **Which stream.** `nat2.liqmap2` (v2), not v1. v2 carries `buckets` at
   `bucket_pct = 0.0025` over `span = 0.30`, which is the resolution the
   question needs; v1 carries only four bands to ±5%. Measured on a live row:
   175 non-empty buckets for BTC. **v1 is a prefix of v2**, so nothing is lost.
   Consequence: history starts when v2 started, not when capture did.

2. **Which price.** The `mark` on the *same snapshot row*, not a join against
   `hl.trades`. One read, no as-of join, and no possibility of pairing a map
   with a price from a different instant. A trade-level price is a later
   refinement (task 04), never the default.

3. **What the tool may do.** Render only. It does not write the store, does not
   append to the ledger, does not record a verdict, and has no thresholds. It
   is an instrument, not a detector — the moment it decides something, it needs
   a pre-registration and it stops being this tool.

4. **Coverage disclosure.** Every panel prints `coverage` and `published_frac`.
   Live values today: BTC 0.382, ETH 0.419, HYPE 0.375, SOL 0.408. The map is
   built from the wallets we can see, which is under half of open interest, and
   a heatmap that omits that number invites exactly the conclusion it cannot
   support.

## Verify
```
python3 -c "import sys; print(sys.version)"        # 3.12 on /usr/bin/python3
ls data/raw/nat2.liqmap2 | head -3                 # the stream exists
```

## Done when
All four answers are written above and the v2 stream is present locally.
