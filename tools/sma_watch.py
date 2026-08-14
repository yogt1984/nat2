#!/usr/bin/env python3
"""Watch an SMA crossover and push it to your phone via ntfy.sh.

Trend-following, the conventional direction:

    price crosses ABOVE the SMA  ->  BUY
    price crosses BELOW the SMA  ->  SELL

This notifies. It does not trade. Wiring execution is a separate, deliberate
step -- see the note at the bottom of this docstring.

Three things here are less obvious than they look, and each one is a bug that a
naive version ships with.

**The forming bar is dropped.** The last candle HL returns is still open, so a
cross computed on it can un-happen. Signals are taken on closed bars only,
which means a `1d` watcher learns about a cross up to a day late -- that is the
honest cost of not acting on a bar that has not finished.

**A cross is a transition, so it needs memory.** The side we were last on is
persisted to disk. Without it, a restart either re-fires a cross from a week
ago or misses one entirely. On the very first run the side is recorded *without*
notifying, because "the state I happen to start in" is not an event.

**Hysteresis, or you will be notified constantly.** Price hugging the SMA
crosses back and forth all day. A cross only counts once price clears the line
by `--band` percent, so the notifier reports moves rather than jitter.

Usage:

    export NTFY_TOPIC=some-long-unguessable-string
    tools/sma_watch.py ETH --periods 31 --interval 1d          # loop
    tools/sma_watch.py ETH --once --dry-run                    # check it

The ntfy topic name IS the password -- there is no sign-up, so anyone who
guesses it reads your signals. Use something random.

Execution, when you want it: install `hyperliquid-python-sdk`, approve an agent
wallet (it can place and cancel orders but CANNOT withdraw), point it at
TESTNET first, and round prices to 5 significant figures and no more than
6 - szDecimals decimals. A rejected order is a bug, not a retry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nat2.hl.info import InfoClient           # noqa: E402
from nat2.hl.ratelimit import SharedWeightBudget  # noqa: E402

# Shared with the capture and cycle daemons. HL limits by IP, not by process,
# so a watcher that polls on its own account would quietly starve the tape.
BUDGET_DB = Path(os.environ.get("NAT2_HOME", ".")) / "data/ratelimit.sqlite"

ABOVE, BELOW = "above", "below"


@dataclass(frozen=True)
class Reading:
    price: float
    sma: float
    bars: int
    closed_at_ms: int

    @property
    def distance(self) -> float:
        return self.price / self.sma - 1.0

    @property
    def side(self) -> str:
        return ABOVE if self.price > self.sma else BELOW


async def read(coin: str, periods: int, interval: str, testnet: bool) -> Reading | None:
    """Latest closed bar and its SMA. None when HL returned too little history."""
    per_bar_ms = {
        "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
        "4h": 14400, "1d": 86400, "1w": 604800,
    }[interval] * 1000
    end = int(time.time() * 1000)
    # Ask for generous headroom: HL caps history per interval and returns what
    # it has, so the count is checked rather than the window trusted.
    start = end - int((periods + 10) * per_bar_ms)

    info = InfoClient(SharedWeightBudget(BUDGET_DB), testnet=testnet)
    try:
        rows = await info.candles(coin, interval, start, end)
    finally:
        await info.aclose()

    rows = sorted(rows or [], key=lambda r: r["t"])
    if len(rows) < periods + 1:
        return None
    closed = rows[:-1]                       # the last bar is still forming
    if len(closed) < periods:
        return None
    window = [float(r["c"]) for r in closed[-periods:]]
    return Reading(
        price=window[-1],
        sma=sum(window) / periods,
        bars=len(closed),
        closed_at_ms=closed[-1]["t"],
    )


def transition(previous: str | None, r: Reading, band: float) -> str | None:
    """The new side, if price has cleared the line by `band`. Else None.

    Hysteresis lives here: crossing is not `price > sma`, it is clearing the
    line by enough to be worth acting on.
    """
    if previous != ABOVE and r.price > r.sma * (1 + band):
        return ABOVE
    if previous != BELOW and r.price < r.sma * (1 - band):
        return BELOW
    return None


def _header(value: str) -> str:
    """HTTP headers are ASCII. Anything else is RFC 2047 encoded, not dropped.

    An em-dash in the title raised UnicodeEncodeError from httpx and killed the
    push -- and coin names on this venue are not guaranteed ASCII either.
    """
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        from email.header import Header
        return Header(value, "utf-8").encode()


def notify(topic: str, title: str, body: str, tags: str, priority: str = "high") -> bool:
    resp = httpx.post(
        f"https://ntfy.sh/{topic}",
        content=body.encode(),           # body is UTF-8 bytes; only headers are ASCII
        headers={"Title": _header(title), "Priority": priority, "Tags": tags},
        timeout=15.0,
    )
    return resp.is_success


def load(path: Path) -> str | None:
    try:
        return json.loads(path.read_text()).get("side")
    except (OSError, ValueError):
        return None


def save(path: Path, side: str, r: Reading) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "side": side, "price": r.price, "sma": r.sma,
        "closed_at_ms": r.closed_at_ms, "written_ms": int(time.time() * 1000),
    }))


async def check(args, topic: str | None) -> int:
    r = await read(args.coin, args.periods, args.interval, args.testnet)
    if r is None:
        print(f"{args.coin}: too little history for a {args.periods}-period "
              f"{args.interval} SMA -- refusing to guess", file=sys.stderr)
        return 2

    state = Path(args.state or f"data/sma_watch/{args.coin}-{args.interval}-{args.periods}.json")
    previous = load(state)
    moved = transition(previous, r, args.band / 100.0)

    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r.closed_at_ms / 1000))
    print(f"{args.coin} {stamp}Z  px {r.price:,.4g}  sma{args.periods} {r.sma:,.4g}  "
          f"{r.distance:+.2%}  side={r.side}  held={previous or '-'}  "
          f"{'CROSS ' + moved.upper() if moved else 'no cross'}")

    if moved is None:
        return 0

    if previous is None:
        # First run. The side we happen to start on is a state, not an event --
        # notifying here would fire a spurious signal on every fresh install.
        save(state, moved, r)
        print(f"  first run: recorded side={moved} without notifying")
        return 0

    action = "BUY" if moved == ABOVE else "SELL"
    title = f"{action} {args.coin}: crossed {moved} SMA{args.periods}"
    body = (f"{args.coin} {r.price:,.4g} vs SMA{args.periods} {r.sma:,.4g} "
            f"({r.distance:+.2%})\nbar closed {stamp}Z on {args.interval} candles")
    tags = "chart_with_upwards_trend" if moved == ABOVE else "chart_with_downwards_trend"

    if args.dry_run or not topic:
        print(f"  [dry-run] {title}\n  {body}")
    elif notify(topic, title, body, tags):
        print(f"  notified ntfy.sh/{topic}: {title}")
    else:
        print("  ntfy push FAILED -- state not advanced, will retry", file=sys.stderr)
        return 1        # do not save: an unsent signal must fire again

    save(state, moved, r)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("coin")
    p.add_argument("--periods", type=int, default=31)
    p.add_argument("--interval", default="1d",
                   choices=["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"])
    p.add_argument("--band", type=float, default=0.3,
                   help="percent past the SMA before a cross counts (anti-whipsaw)")
    p.add_argument("--poll", type=float, default=900, help="seconds between checks")
    p.add_argument("--once", action="store_true", help="check once and exit, for a timer")
    p.add_argument("--dry-run", action="store_true", help="print, never push")
    p.add_argument("--state", help="where the held side is persisted")
    p.add_argument("--testnet", action="store_true")
    args = p.parse_args()

    topic = os.environ.get("NTFY_TOPIC")
    if not topic and not args.dry_run:
        print("NTFY_TOPIC is not set. Export one (long and unguessable -- the "
              "topic name is the password) or pass --dry-run.", file=sys.stderr)
        return 2

    if args.once:
        return asyncio.run(check(args, topic))

    while True:
        try:
            asyncio.run(check(args, topic))
        except Exception as exc:                      # noqa: BLE001
            # A watcher that dies on one bad response stops watching, which is
            # the one thing it must not do.
            print(f"check failed: {exc!r}", file=sys.stderr)
        time.sleep(args.poll)


if __name__ == "__main__":
    raise SystemExit(main())
