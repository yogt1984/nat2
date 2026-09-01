# 15 — Storage Box and restic

**Effort** 1 h · **Depends on** 07, 14 · **Status** todo

## What
The off-host copy. Order the Storage Box **several days after** the server — a
large first order is a documented rejection trigger.

## How
BX11 (1 TB, €3.20 net) is ample for ~150 GB/year. SSH is on **port 23**; port 22
is SFTP/SCP with no shell.

```
cat ~/.ssh/id_ed25519.pub | ssh -p23 uNNN@uNNN.your-storagebox.de install-ssh-key
```

Install the **upstream restic binary**, not the apt package — 24.04 ships 0.16.4
and misses the index and memory work that matters at ~876k files/year. Use
`--pack-size 64` and `RESTIC_COMPRESSION=off`; the tape is already zstd.

Cap at `-o sftp.connections=8`: the box allows 10 in total, and backup plus check
concurrently is 10. Put `forget`/`prune` on a **separate timer at a different
hour** — prune locks the repository.

Enable the box's automatic snapshots as an immutability net; the restic client
holds delete rights over its own backup. They are not backups — a restore deletes
everything newer.

## Verify
```
restic snapshots && restic check --read-data-subset=1/20
```

## Done when
A snapshot exists and a restore has been rehearsed once.
