# Running nat2 as a daemon

Capture accrues calendar time that no later code can recover, so it belongs under a supervisor
rather than in a terminal. These are user units — no root, and `linger` keeps them running across
logout.

```bash
cp packaging/systemd/nat2-*.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now nat2-capture nat2-cycle
```

Paths are absolute and assume `/home/onat/nat2` with `nat2` on `~/.local/bin`. Edit
`WorkingDirectory`, `Environment=NAT2_HOME` and `ExecStart` if yours differ.

```bash
systemctl --user status nat2-capture
journalctl --user -u nat2-capture -f
nat2 audit feed --window 24h        # is what accrued intact and causal?
```

## Two decisions worth knowing

**`RuntimeMaxSec=6h` on capture is a workaround, not a fix.** `FINDINGS.md` records that capture
degrades over long runs — 1,737 reconnects and a 49% failure rate on `metaAndAssetCtxs` after
19.5 hours, producing 846 context records where ~4,700 were expected. The cause is unknown and a
restart clears it, so the unit recycles well inside that window. This is safe only because a
restart opens a **new** WORM part: a closed file is checksummed into the manifest and never
reopened, so a recycle can never be mistaken for tampering. Remove the line once the cause is
found.

**No `--wallet-limit` on cycle.** A partial sweep silently shrinks the map — `replace_positions`
rewrites only what was swept, so a limit drops coverage with no error at all (observed 31.5% →
11.7% after a 400-wallet pass). The whole registry or nothing.

## Universe

Capture runs `--all --min-volume 5000000`, which is 18 coins as of 2026-08-13, against 177 with no
floor. Enough headroom over the ~10 coins `HYPOTHESIS_1.md` §8 expects to clear the coverage
floor, without opening 177 × 3 subscriptions against a per-connection limit that is still on the
verify-before-coding list.
