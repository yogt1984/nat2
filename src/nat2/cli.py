"""nat2 command line."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from nat2.core.clock import NS, parse_window
from nat2.core.guard import latest as latest_verdict
from nat2.hl.info import InfoClient
from nat2.hl.ratelimit import WeightBudget
from nat2.hl.schemas import STREAMS
from nat2.ledger.chain import Ledger

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Hyperliquid research engine")
capture_app = typer.Typer(no_args_is_help=True, help="Capture daemons")
audit_app = typer.Typer(no_args_is_help=True, help="Data integrity audits")
gate_app = typer.Typer(no_args_is_help=True, help="Falsification gates")
log_app = typer.Typer(no_args_is_help=True, help="Hash-chained ledger")
app.add_typer(capture_app, name="capture")
app.add_typer(audit_app, name="audit")
app.add_typer(gate_app, name="gate")
app.add_typer(log_app, name="log")

console = Console()

RAW = Path("data/raw")
PARQUET = Path("data/parquet")
LEDGER = Path("data/ledger.jsonl")
DEFAULT_STREAMS = "hl.trades,hl.l2book,hl.assetctxs"

RootOpt = Annotated[Path, typer.Option("--root", help="WORM store root")]
StreamsOpt = Annotated[str, typer.Option("--streams", help="comma-separated stream names")]
LedgerOpt = Annotated[Path, typer.Option("--ledger", help="hash-chained ledger path")]


def _streams(spec: str) -> list[str]:
    names = [s.strip() for s in spec.split(",") if s.strip()]
    unknown = [n for n in names if n not in STREAMS]
    if unknown:
        raise typer.BadParameter(f"unknown stream(s) {unknown}; known: {sorted(STREAMS)}")
    return names


@capture_app.command("hl")
def capture_hl(
    coins: Annotated[str, typer.Option("--coins", help="comma-separated, or --all")] = "BTC,ETH",
    all_coins: Annotated[bool, typer.Option("--all", help="every non-delisted perp")] = False,
    min_volume: Annotated[float, typer.Option(help="--all: min 24h notional volume")] = 0.0,
    streams: StreamsOpt = DEFAULT_STREAMS,
    root: RootOpt = RAW,
    testnet: bool = False,
    poll_interval: Annotated[float, typer.Option(help="seconds between asset-ctx polls")] = 10.0,
) -> None:
    """Run the capture daemon until interrupted.

    This is the project's real start date: point-in-time series cannot be
    recovered later, so nothing downstream can be validated on data you do
    not yet own.
    """
    from nat2.io.capture import Capture, CaptureConfig

    async def _run() -> None:
        selected = [c.strip() for c in coins.split(",") if c.strip()]
        if all_coins:
            info = InfoClient(WeightBudget(), testnet=testnet)
            selected = await info.universe(min_day_volume=min_volume)
            await info.aclose()
        config = CaptureConfig(
            root=root,
            coins=selected,
            streams=_streams(streams),
            testnet=testnet,
            poll_interval_s=poll_interval,
        )
        console.print(
            f"[bold]capture[/bold] {len(selected)} coin(s) x {len(config.streams)} stream(s) "
            f"-> {root}  (ctrl-c to stop)"
        )
        capture = Capture(config, on_status=_print_status)
        await capture.run()
        _print_status(capture)
        console.print("[dim]writers closed; manifest updated[/dim]")

    asyncio.run(_run())


def _print_status(capture) -> None:
    counts = ", ".join(f"{k.split('.')[-1]} {v}" for k, v in sorted(capture.stats.written.items()))
    ws = capture.ws.stats if capture.ws else None
    extra = f" | reconnects {ws.reconnects}" if ws else ""
    console.print(
        f"[dim]{capture.uptime_s / 60:6.1f}m | {counts or 'no records yet'}"
        f" | poll err {capture.stats.poll_errors}{extra}[/dim]"
    )


@app.command()
def compact(
    root: RootOpt = RAW,
    out: Annotated[Path, typer.Option("--out", help="parquet root")] = PARQUET,
    streams: StreamsOpt = DEFAULT_STREAMS,
) -> None:
    """Compact closed WORM files into Parquet. The open file is never touched."""
    from nat2.io.compact import compact as run_compact

    written = run_compact(root, out, _streams(streams))
    total = sum(w["rows"] for w in written)
    console.print(f"compacted {len(written)} file(s), {total} record(s) -> {out}")


@app.command()
def universe(
    min_volume: Annotated[float, typer.Option(help="min 24h notional volume")] = 0.0,
    testnet: bool = False,
    limit: int = 20,
) -> None:
    """List HL perps, rebuilt from `meta` -- never a hardcoded coin list."""

    async def _run() -> None:
        info = InfoClient(WeightBudget(), testnet=testnet)
        meta, ctxs = await info.meta_and_asset_ctxs()
        await info.aclose()
        rows = [
            (a["name"], float(c.get("dayNtlVlm", 0) or 0), c.get("markPx"), c.get("oraclePx"), c.get("funding"), a.get("maxLeverage"))
            for a, c in zip(meta["universe"], ctxs)
            if not a.get("isDelisted") and float(c.get("dayNtlVlm", 0) or 0) >= min_volume
        ]
        rows.sort(key=lambda r: -r[1])
        table = Table(title=f"HL perps ({len(rows)} listed, top {min(limit, len(rows))})")
        for col in ("coin", "24h notional", "mark", "oracle", "premium bp", "fund/hr", "maxLev"):
            table.add_column(col, justify="right")
        for name, vol, mark, oracle, funding, lev in rows[:limit]:
            prem = ""
            if mark and oracle and float(oracle):
                prem = f"{(float(mark) / float(oracle) - 1) * 1e4:+.1f}"
            table.add_row(name, f"{vol / 1e6:,.1f}M", str(mark), str(oracle), prem,
                          f"{float(funding) * 1e4:+.2f}bp" if funding else "", str(lev))
        console.print(table)

    asyncio.run(_run())


@audit_app.command("feed")
def audit_feed(
    window: Annotated[str, typer.Option("--window", help="e.g. 30m, 24h, 7d")] = "24h",
    root: RootOpt = RAW,
    streams: StreamsOpt = DEFAULT_STREAMS,
) -> None:
    """Audit store integrity without recording a verdict."""
    from nat2.validate.audit_feed import audit as run_audit

    result = run_audit(root, _streams(streams), parse_window(window))
    _print_audit(result, window)
    raise typer.Exit(0 if result.passed else 1)


def _print_audit(result, window: str) -> None:
    table = Table(title=f"feed audit  window={window}")
    table.add_column("")
    table.add_column("stream")
    table.add_column("check")
    table.add_column("detail", overflow="fold")
    for check in result.checks:
        mark = "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]"
        table.add_row(mark, check.stream, check.name, check.detail)
    console.print(table)


@gate_app.command("feed")
def gate_feed(
    window: Annotated[str, typer.Option("--window")] = "24h",
    root: RootOpt = RAW,
    streams: StreamsOpt = DEFAULT_STREAMS,
    ledger: LedgerOpt = LEDGER,
) -> None:
    """Run gate `feed` and record its verdict. Everything downstream depends on it."""
    from nat2.gates import feed as gate

    verdict, result = gate.run(root, _streams(streams), parse_window(window), Ledger(ledger))
    _print_audit(result, window)
    if verdict.passed:
        console.print("[bold green]gate feed PASS[/bold green] -- recorded to ledger")
    else:
        console.print(
            f"[bold red]gate feed FAIL[/bold red] ({len(result.failures)} check(s)) -- "
            "recorded to ledger; downstream commands will refuse to run"
        )
    raise typer.Exit(0 if verdict.passed else 1)


@gate_app.command("status")
def gate_status(ledger: LedgerOpt = LEDGER) -> None:
    """Show the latest verdict for every gate."""
    chain = Ledger(ledger)
    table = Table(title="gates")
    for col in ("gate", "verdict", "age", "detail"):
        table.add_column(col, overflow="fold")
    for name in ("feed", "map", "magnet", "persistence", "decay"):
        verdict = latest_verdict(chain, name)
        if verdict is None:
            table.add_row(name, "[dim]never run[/dim]", "-", "-")
            continue
        mark = "[green]PASS[/green]" if verdict.passed else "[red]FAIL[/red]"
        failed = verdict.detail.get("failed") or []
        table.add_row(name, mark, f"{verdict.age_s / 3600:.1f}h", ", ".join(failed) or "-")
    console.print(table)


@log_app.command("verify")
def log_verify(ledger: LedgerOpt = LEDGER) -> None:
    """Verify the ledger's hash chain."""
    ok, message = Ledger(ledger).verify()
    console.print(f"[{'green' if ok else 'red'}]{message}[/]")
    raise typer.Exit(0 if ok else 1)


@log_app.command("query")
def log_query(
    kind: Annotated[str, typer.Option("--kind")] = "",
    limit: int = 20,
    ledger: LedgerOpt = LEDGER,
) -> None:
    """Print ledger entries, newest last."""
    entries = Ledger(ledger).entries()
    if kind:
        entries = [e for e in entries if e.kind == kind]
    for entry in entries[-limit:]:
        console.print(
            f"[dim]{entry.seq:>4} {entry.ts / NS:.0f}[/dim] {entry.kind} "
            f"{json.dumps(entry.payload)[:160]}"
        )


if __name__ == "__main__":
    app()
