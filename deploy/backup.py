#!/usr/bin/env python3
"""nat2 backup -- staging, snapshotting, and proving a restore actually restores.

There was no backup code at all before this, for an asset that cannot be
recreated: 22,736 parts and 4.6 GB of tape the venue serves no historical
replay for, plus a registry holding 12,596 realized liquidations that
`liqscan` cannot re-fetch because `userFills` is capped at 2,000 per wallet.

Three things here are not obvious and each one is the difference between a
backup and something that looks like one.

*   **The databases must be staged, never copied.** `registry.sqlite` is in WAL
    mode since the ledger-lock work, and a plain `cp` of the `.sqlite` without
    its `-wal` does not give you a stale database -- it gives you one with **no
    `positions` table at all**, because the schema itself is still in the write
    -ahead log. A restore from that reports zero positions as though that were
    the answer, which is precisely the failure `io/snapshot.py` was written to
    prevent. Staging uses `sqlite3.Connection.backup()`, which is stdlib and
    consistent against a live writer.

*   **The manifest is not a file list.** Twenty parts are on disk and absent
    from `_manifest.jsonl` -- dead-writer tails from unclean shutdowns -- and
    two of them alone hold 46,379 records. A selector that walked the manifest
    would silently drop them *and* the seq range `_resume_seq` needs, so a
    restored store would resume numbering over a hole. Walk the tree.

*   **A uniform spot-check checks almost nothing.** The three websocket streams
    are 4.7% of parts but **90.3% of the bytes**, so a uniform draw of twenty
    misses at least one of them 98.3% of the time. The sample is stratified: a
    floor per stream, the remainder by bytes.

`keep_daily` and `spot_check_n` are **policy, not repo constants** -- how long
you keep evidence and how hard you check it are decisions, not implementation
details -- so they are read from a ledgered `backup_v1` pre-registration and
this refuses without one.

Stdlib only, on `/usr/bin/python3`: a backup tool that needs the venv cannot
run on the day the venv is what broke.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
LEDGER = DATA / "ledger.jsonl"
STAGED = DATA / "ops" / "staged"
STATE = DATA / "ops" / "backup_state.json"
MANIFEST = "_manifest.jsonl"
SUFFIX = ".ndjson.zst"
NS = 1_000_000_000
GENESIS = "0" * 64

PREREG_NAME = "backup_v1"
POLICY_KEYS = ("keep_daily", "spot_check_n")

# Staged, because a raw copy of a WAL database is not a copy of the database.
STAGE_DATABASES = ("registry.sqlite",)
# Copied as-is. Append-only JSONL whose readers already tolerate a torn tail.
INCLUDE_FILES = ("ledger.jsonl", "actions.jsonl")
INCLUDE_TREES = ("raw", "events", "ops")
# Regenerable from the tape, and a 60-second sliding window of rate-limit spend.
EXCLUDE = ("parquet", "ratelimit.sqlite")
# At least this many parts per stream before the remainder is shared by bytes,
# so the sparse streams cannot be sampled away. Not a policy number: it is the
# smallest count for which "this stream was checked" is a true sentence.
FLOOR_PER_STREAM = 2


# --- policy ----------------------------------------------------------------

def preregistered_policy(path: Path = LEDGER) -> dict | None:
    """`keep_daily` and `spot_check_n` from the newest `backup_v1` entry.

    Refused rather than defaulted. How long evidence is kept and how hard a
    restore is checked are claims about the record, and a number chosen here
    would look measured while never having been decided.
    """
    if not path.exists():
        return None
    found = None
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = entry.get("payload") or {}
        if entry.get("kind") == "preregistration" and payload.get("name") == PREREG_NAME:
            if all(isinstance(payload.get(k), int) for k in POLICY_KEYS):
                found = {k: int(payload[k]) for k in POLICY_KEYS}
    return found


def refuse() -> int:
    print(
        f"refusing: no {PREREG_NAME} pre-registration carries {' and '.join(POLICY_KEYS)}.\n"
        "Retention and spot-check depth are policy, not repo constants -- they are\n"
        "claims about how the record is kept, so they go on the chain before use:\n"
        f"  nat2 log add --kind preregistration --json '{{\"name\": \"{PREREG_NAME}\", "
        "\"keep_daily\": 90, \"spot_check_n\": 20, \"rationale\": \"...\"}'",
        file=sys.stderr)
    return 2


# --- the store -------------------------------------------------------------

def _salvage(record: str) -> dict | None:
    decoder = json.JSONDecoder()
    for start in range(len(record)):
        if record[start] != "{":
            continue
        try:
            value, end = decoder.raw_decode(record[start:])
        except json.JSONDecodeError:
            continue
        if start + end == len(record) and isinstance(value, dict):
            return value
    return None


def read_manifest(root: Path) -> list[dict]:
    """Tolerating both torn shapes, as `io/worm.py` does. Copied, not imported:
    a broken venv is one of the things a backup exists to survive."""
    path = root / MANIFEST
    if not path.exists():
        return []
    out, lines = [], path.read_text(errors="replace").splitlines()
    for i, line in enumerate(lines):
        record = line.strip("\x00 \t\r\n�")
        if not record:
            continue
        try:
            out.append(json.loads(record))
        except json.JSONDecodeError:
            recovered = _salvage(record)
            if recovered is not None:
                out.append(recovered)
            elif "\x00" in line or "�" in line or i == len(lines) - 1:
                continue
            else:
                raise
    return out


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --- staging ---------------------------------------------------------------

def stage(data: Path = DATA, into: Path = STAGED) -> dict:
    """Consistent copies of the databases, ready for a snapshot to pick up.

    `Connection.backup()` rather than `cp`: it is stdlib, it works against a
    live writer, and what it produces is a single self-contained file with no
    sidecars to lose.
    """
    into.mkdir(parents=True, exist_ok=True)
    staged = {}
    for name in STAGE_DATABASES:
        source = data / name
        if not source.exists():
            continue
        target = into / name
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, \
                sqlite3.connect(target) as dst:
            src.backup(dst)
        staged[name] = {"bytes": target.stat().st_size, "sha256": sha256_of(target)}
    return staged


# --- the spot-check sample -------------------------------------------------

def stratified(entries: list[dict], n: int, rng: random.Random) -> list[dict]:
    """`n` parts, with every stream represented.

    A uniform draw is the wrong instrument: the websocket streams are 4.7% of
    parts and 90.3% of the bytes, so twenty uniform picks miss at least one of
    them 98.3% of the time. A floor per stream guarantees representation; the
    remainder goes by bytes, because that is where there is more to go wrong.
    """
    by_stream: dict[str, list[dict]] = {}
    for entry in entries:
        by_stream.setdefault(entry["stream"], []).append(entry)
    if not by_stream:
        return []

    quota = {s: min(FLOOR_PER_STREAM, len(v)) for s, v in by_stream.items()}
    remaining = n - sum(quota.values())
    if remaining > 0:
        weight = {s: sum(e.get("bytes", 0) for e in v) for s, v in by_stream.items()}
        total = sum(weight.values()) or 1
        for stream, w in sorted(weight.items(), key=lambda kv: -kv[1]):
            take = min(int(remaining * w / total), len(by_stream[stream]) - quota[stream])
            quota[stream] += max(take, 0)
    # Whatever rounding left over goes to the heaviest stream that can take it.
    short = n - sum(quota.values())
    for stream in sorted(by_stream, key=lambda s: -sum(e.get("bytes", 0) for e in by_stream[s])):
        while short > 0 and quota[stream] < len(by_stream[stream]):
            quota[stream] += 1
            short -= 1

    out = []
    for stream, count in quota.items():
        out.extend(rng.sample(by_stream[stream], min(count, len(by_stream[stream]))))
    return out


# --- the restore-verify ----------------------------------------------------

def verify_chain(path: Path) -> tuple[bool, str]:
    """The ledger's hash chain, re-derived. Mirrors `ledger/chain.py`; copied so
    a restore can be checked on a box with no venv and no nat2 installed."""
    if not path.exists():
        return False, "no ledger in the restored tree"
    prev = GENESIS
    for i, line in enumerate(path.read_text(errors="replace").splitlines()):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return False, f"entry {i}: unreadable"
        if entry["seq"] != i:
            return False, f"entry {i}: seq is {entry['seq']}"
        if entry["prev_hash"] != prev:
            return False, f"entry {i}: prev_hash does not match entry {i - 1}"
        body = json.dumps({"seq": entry["seq"], "ts": entry["ts"], "kind": entry["kind"],
                           "payload": entry["payload"], "prev_hash": entry["prev_hash"]},
                          sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(body.encode()).hexdigest() != entry["hash"]:
            return False, f"entry {i}: content does not match its hash"
        prev = entry["hash"]
    return True, "chain intact"


def restore_verify(restored: Path, n: int, seed: int | None = None) -> dict:
    """Prove a restored tree is the store, not a directory shaped like one.

    Works against anything: a restic restore, an rsync copy, a mounted
    snapshot. The plan's bar is two independently restorable copies, and only
    one of them will ever be restic.
    """
    raw = restored / "raw" if (restored / "raw").exists() else restored
    entries = read_manifest(raw)
    rng = random.Random(seed)
    sample = stratified(entries, n, rng)

    checked, mismatched, missing = [], [], []
    for entry in sample:
        part = raw / entry["path"]
        if not part.exists():
            missing.append(entry["path"])
            continue
        digest = sha256_of(part)
        (checked if digest == entry["sha256"] else mismatched).append(entry["path"])

    ledger = restored / "ledger.jsonl"
    chain_ok, chain_msg = verify_chain(ledger)
    per_stream: dict[str, int] = {}
    for entry in sample:
        per_stream[entry["stream"]] = per_stream.get(entry["stream"], 0) + 1

    return {
        "restored": str(restored), "manifest_entries": len(entries),
        "sampled": len(sample), "per_stream": per_stream,
        "matched": len(checked), "mismatched": mismatched, "missing": missing,
        "chain_ok": chain_ok, "chain": chain_msg,
        "ok": not mismatched and not missing and chain_ok and len(sample) > 0,
    }


# --- state -----------------------------------------------------------------

def write_state(result: dict, path: Path = STATE) -> None:
    """The sidecar the digest and the status page read, so `backup: none
    configured` can become a number with an age on it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {}
    if path.exists():
        try:
            blob = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            blob = {}
    blob.update(result)
    blob["t_ingest"] = int(datetime.now(timezone.utc).timestamp() * NS)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(blob, indent=2, sort_keys=True))
    tmp.replace(path)


def iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / NS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- snapshot --------------------------------------------------------------

def snapshot(keep_daily: int, dry_run: bool = False) -> dict:
    """A restic snapshot of the staged store. Refuses if restic is absent
    rather than pretending -- `which restic` is empty on this box today, and a
    backup tool that silently does nothing is the worst artefact in this repo."""
    binary = shutil.which("restic")
    if binary is None:
        return {"ok": False, "reason": "restic is not installed (task 15 installs it)"}
    targets = [str(DATA / f) for f in INCLUDE_FILES if (DATA / f).exists()]
    targets += [str(DATA / d) for d in INCLUDE_TREES if (DATA / d).exists()]
    targets += [str(STAGED)] if STAGED.exists() else []
    argv = [binary, "backup", *targets, "--exclude", *EXCLUDE]
    if dry_run:
        return {"ok": True, "dry_run": True, "argv": argv, "keep_daily": keep_daily}
    done = subprocess.run(argv, capture_output=True, text=True)
    return {"ok": done.returncode == 0, "returncode": done.returncode,
            "stderr": done.stderr[-400:], "keep_daily": keep_daily}


# --- cli -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="stage, snapshot and prove a restore")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stage", help="consistent copies of the databases")
    snap = sub.add_parser("snapshot", help="restic snapshot of the staged store")
    snap.add_argument("--dry-run", action="store_true")
    ver = sub.add_parser("verify", help="prove a restored tree is the store")
    ver.add_argument("--restored", required=True, type=Path)
    ver.add_argument("--seed", type=int, default=None)
    sub.add_parser("status", help="what the last backup was, and how old")
    args = parser.parse_args(argv)

    if args.command == "status":
        if not STATE.exists():
            print("no backup has ever run", file=sys.stderr)
            return 1
        blob = json.loads(STATE.read_text())
        age_h = (datetime.now(timezone.utc).timestamp() - blob.get("t_ingest", 0) / NS) / 3600
        print(f"last: {iso(blob.get('t_ingest', 0))}  ({age_h:.1f}h ago)  ok={blob.get('ok')}")
        return 0 if blob.get("ok") else 1

    policy = preregistered_policy()
    if policy is None:
        return refuse()

    if args.command == "stage":
        staged = stage()
        write_state({"staged": staged, "ok": bool(staged)})
        for name, info in staged.items():
            print(f"staged {name}: {info['bytes']:,} bytes  {info['sha256'][:16]}...")
        return 0 if staged else 1

    if args.command == "snapshot":
        stage()
        result = snapshot(policy["keep_daily"], dry_run=args.dry_run)
        write_state({"snapshot": result, "ok": result["ok"]})
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1

    result = restore_verify(args.restored, policy["spot_check_n"], seed=args.seed)
    write_state({"verify": result, "ok": result["ok"]})
    print(f"{result['matched']}/{result['sampled']} parts match their sha256 "
          f"across {len(result['per_stream'])} streams {result['per_stream']}")
    print(f"ledger: {result['chain']}")
    if result["mismatched"]:
        print(f"MISMATCHED: {result['mismatched']}", file=sys.stderr)
    if result["missing"]:
        print(f"MISSING: {result['missing']}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
