# 07 — Remote: check and fetch from the Hetzner box

**Effort** 2 h · **Depends on** 06 · **Status** todo

## What
The same picture, from the box that is actually capturing — without copying a
4.6 GB store to look at six hours of it.

## How
Two modes, because they fail differently and you want both.

**`--host nat2@<box>` — render remotely, stream the frame back.**
```
ssh <host> 'python3 /home/onat/nat2/deploy/liqview.py --coin BTC --since 6h --ascii'
```
Nothing is copied. Fastest, and it works when your laptop has no store at all.
It is also the honest default for "what does the box see right now", because
the box's answer is not filtered through a stale local copy.

**`--host <box> --fetch` — pull the slice, render locally.**
Copy only the `nat2.liqmap2` parts overlapping the window, plus
`_manifest.jsonl`, into a local cache. Sizes make this cheap: the whole v2
stream is **0.29 GB across 9,608 parts**, so six hours is a few MB. Do not
fetch `data/raw/hl.*` — those three streams are 90.3% of the bytes.

**Order matters, and it is the same rule the backup task found**: fetch
`_manifest.jsonl` **first**, then the parts. A part copied while still open
hashes differently from its manifest entry; taken in this order, a part that
closes mid-fetch simply arrives unmanifested, which the reader already
tolerates. Taken in the other order it arrives *manifested and wrong*.

**Preflight `--check`.** Before either mode, verify and print: ssh reachable,
`/usr/bin/python3` present and ≥3.12, the store path exists, the newest
`nat2.liqmap2` part's age, `capture` and `cycle` unit states, and free disk.
A stale newest-part age is the one number that explains a confusing picture, so
print it whether or not it looks fine.

Assume a **read-only** SSH identity where possible. This tool never needs to
write on the box, and an instrument that cannot write cannot be the thing that
broke the record.

## Verify
```
python3 deploy/liqview.py --host <box> --check
python3 deploy/liqview.py --host <box> --coin BTC --since 6h
python3 deploy/liqview.py --host <box> --coin BTC --since 6h --fetch
```
The remote-rendered frame and the fetch-then-render frame are byte-identical
for a closed window. Fetch transfers only `nat2.liqmap2` parts — assert against
`du -sh` of the cache and a `--dry-run` file list.

## Done when
`--check` reports every field above, both modes agree byte-for-byte on a closed
window, and no `hl.*` part is ever transferred.
