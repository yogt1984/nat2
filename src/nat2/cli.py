"""nat2 command line."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from nat2.core.clock import NS, parse_window, to_dt
from nat2.core.guard import latest as latest_verdict
from nat2.core.paths import home, resolved
from nat2.hl.info import InfoClient
from nat2.hl.ratelimit import SharedWeightBudget
from nat2.hl.schemas import STREAMS
from nat2.ledger.chain import Ledger

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Hyperliquid research engine")
capture_app = typer.Typer(no_args_is_help=True, help="Capture daemons")
audit_app = typer.Typer(no_args_is_help=True, help="Data integrity audits")
gate_app = typer.Typer(no_args_is_help=True, help="Falsification gates")
log_app = typer.Typer(no_args_is_help=True, help="Hash-chained ledger")
wallets_app = typer.Typer(no_args_is_help=True, help="Wallet registry")
liq_app = typer.Typer(no_args_is_help=True, help="Realized liquidations")
app.add_typer(wallets_app, name="wallets")
app.add_typer(liq_app, name="liq")
app.add_typer(capture_app, name="capture")
app.add_typer(audit_app, name="audit")
app.add_typer(gate_app, name="gate")
app.add_typer(log_app, name="log")

console = Console()

# Absolute, resolved once at import. Relative defaults would silently point at
# whatever directory the command happened to be run from.
HOME = home()
RAW = HOME / "data/raw"
PARQUET = HOME / "data/parquet"
LEDGER = HOME / "data/ledger.jsonl"
BUDGET = HOME / "data/ratelimit.sqlite"
DEFAULT_STREAMS = "hl.trades,hl.l2book,hl.assetctxs"

RootOpt = Annotated[Path, typer.Option("--root", help="WORM store root")]
StreamsOpt = Annotated[str, typer.Option("--streams", help="comma-separated stream names")]
LedgerOpt = Annotated[Path, typer.Option("--ledger", help="hash-chained ledger path")]


def _budget() -> SharedWeightBudget:
    """One weight account per machine -- HL limits by IP, not by process."""
    return SharedWeightBudget(BUDGET)


def command_summaries() -> list[tuple[str, str]]:
    """(command path, one-line summary) for every leaf command.

    Walked out of the CLI itself rather than maintained by hand, so it cannot
    drift: a command added without a docstring shows up as an empty summary,
    and the test suite fails on it.
    """
    found: list[tuple[str, str]] = []

    def walk(instance: typer.Typer, prefix: list[str]) -> None:
        for command in instance.registered_commands:
            name = command.name or command.callback.__name__.replace("_", "-")
            text = (command.help or command.callback.__doc__ or "").strip()
            found.append((
                " ".join(prefix + [name]),
                text.splitlines()[0].strip() if text else "",
            ))
        for group in instance.registered_groups:
            walk(group.typer_instance, prefix + [group.name])

    walk(app, [])
    return sorted(found)


@app.command("help")
def help_command(
    paths: Annotated[bool, typer.Option("--paths", help="bare command paths, for scripts")] = False,
) -> None:
    """Every command and what it does, in one line each."""
    summaries = command_summaries()
    if paths:
        for path, _ in summaries:
            print(path)
        return
    groups: dict[str, list[tuple[str, str]]] = {}
    for path, summary in summaries:
        groups.setdefault(path.split(" ")[0] if " " in path else "", []).append((path, summary))
    width = max((len(p) for p, _ in summaries), default=0)
    console.print("[bold]nat2[/bold] — Hyperliquid research and execution engine\n")
    for group in sorted(groups):
        for path, summary in groups[group]:
            # soft_wrap: a one-liner that wraps mid-word is not a one-liner.
            console.print(f"  [bold cyan]{path:<{width}}[/bold cyan]  {summary}", soft_wrap=True)
        console.print("")
    path, how = resolved()
    console.print(
        "[dim]gates before models: every command downstream of a gate refuses to run "
        "while that gate is missing, stale or FAIL[/dim]"
    )
    console.print(f"[dim]data home: {path}  ({how}; override with NAT2_HOME)[/dim]")


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
            info = InfoClient(_budget(), testnet=testnet)
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
    why = capture.why()
    if why:
        console.print(f"[yellow]  why: {why}[/yellow]")


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
        info = InfoClient(_budget(), testnet=testnet)
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


REGISTRY = HOME / "data/registry.sqlite"
RegistryOpt = Annotated[Path, typer.Option("--registry", help="wallet registry database")]


async def _marks_and_oi(testnet: bool = False) -> tuple[dict, dict]:
    """Mark price and OI notional per coin, from one request."""
    info = InfoClient(_budget(), testnet=testnet)
    meta, ctxs = await info.meta_and_asset_ctxs()
    await info.aclose()
    marks, oi = {}, {}
    for asset, ctx in zip(meta["universe"], ctxs):
        mark = float(ctx.get("markPx") or 0)
        if not mark:
            continue
        marks[asset["name"]] = mark
        oi[asset["name"]] = float(ctx.get("openInterest") or 0) * mark
    return marks, oi


@wallets_app.command("seed")
def wallets_seed(
    top_equity: Annotated[int, typer.Option(help="seeds the liquidation map")] = 2000,
    top_volume: Annotated[int, typer.Option(help="seeds the skill cohort")] = 2000,
    registry: RegistryOpt = REGISTRY,
) -> None:
    """Seed the registry from HL's leaderboard.

    Two seeds, because they select different populations: equity finds whoever
    holds size, volume finds whoever trades.
    """
    from nat2.core.registry import Registry
    from nat2.hl import leaderboard

    async def _run() -> None:
        console.print("fetching leaderboard …")
        rows = await leaderboard.fetch()
        tags = leaderboard.seed(rows, top_equity, top_volume)
        stored = Registry(registry).seed_wallets(rows, tags)
        counts: dict[str, int] = {}
        for tag in tags.values():
            counts[tag] = counts.get(tag, 0) + 1
        console.print(
            f"leaderboard {len(rows)} wallets -> registry {stored} "
            f"({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))})"
        )

    asyncio.run(_run())


@wallets_app.command("snapshot")
def wallets_snapshot(
    limit: Annotated[int, typer.Option(help="0 = whole registry")] = 0,
    registry: RegistryOpt = REGISTRY,
    testnet: bool = False,
) -> None:
    """Reconcile positions from clearinghouseState. Costs ~5 min for a full registry."""
    from nat2.core.registry import Registry
    from nat2.io.snapshot import sweep

    async def _run() -> None:
        reg = Registry(registry)
        addresses = reg.addresses(limit=limit or None)
        if not addresses:
            console.print("[red]registry is empty -- run `nat2 wallets seed` first[/red]")
            raise typer.Exit(1)
        info = InfoClient(_budget(), testnet=testnet)
        console.print(f"sweeping {len(addresses)} wallets …")
        result = await sweep(
            reg, info, addresses,
            on_progress=lambda d, n: console.print(f"[dim]  … {d}/{n}[/dim]"),
        )
        await info.aclose()
        console.print(
            f"snapshot {result['id']}: {result['positions']} positions from "
            f"{result['holders']}/{result['wallets']} wallets, {result['errors']} error(s), "
            f"{result['elapsed_s']:.0f}s"
        )

    asyncio.run(_run())


@wallets_app.command("status")
def wallets_status(registry: RegistryOpt = REGISTRY) -> None:
    """Registry size, seeds, and snapshot age."""
    from nat2.core.registry import Registry

    reg = Registry(registry)
    counts = reg.wallet_count()
    snapshot = reg.last_snapshot()
    age = reg.position_age_ns()
    path, how = resolved()
    console.print(f"[dim]data home: {path} ({how})[/dim]")
    console.print(f"wallets: {sum(counts.values())} ({counts})")
    if snapshot:
        console.print(
            f"last snapshot #{snapshot['id']}: {snapshot['positions']} positions from "
            f"{snapshot['holders']}/{snapshot['wallets']} wallets, "
            f"{snapshot['errors']} error(s)"
        )
    console.print(f"positions age: {age / NS / 60:.1f}m" if age else "positions: none")


@liq_app.command("scan")
def liquidations_scan(
    observers: Annotated[int, typer.Option(help="highest-volume wallets to read")] = 60,
    registry: RegistryOpt = REGISTRY,
    testnet: bool = False,
) -> None:
    """Collect realized liquidations from observer wallets' fills.

    Liquidations are seen through whoever took the other side, so a handful of
    high-volume wallets observes far more of them than the whole registry would.
    """
    from nat2.core.registry import Registry
    from nat2.io.liqscan import candidate_observers, scan

    async def _run() -> None:
        reg = Registry(registry)
        addresses = candidate_observers(reg, observers)
        if not addresses:
            console.print("[red]registry is empty -- run `nat2 wallets seed` first[/red]")
            raise typer.Exit(1)
        info = InfoClient(_budget(), testnet=testnet)
        console.print(f"scanning {len(addresses)} observers …")
        result = await scan(
            reg, info, addresses,
            on_progress=lambda d, n: console.print(f"[dim]  … {d}/{n}[/dim]"),
        )
        await info.aclose()
        console.print(
            f"{result['unique_events']} unique liquidation(s) "
            f"({result['new_events']} new) from "
            f"{result['productive_observers']}/{result['observers']} productive observers, "
            f"{result['errors']} error(s)"
        )

    asyncio.run(_run())


@liq_app.command("list")
def liquidations_list(
    limit: int = 20,
    registry: RegistryOpt = REGISTRY,
) -> None:
    """Recent liquidations, and how the observed notional splits by method."""
    from nat2.core.registry import Registry
    from nat2.features.liquidations import method_notional

    events = Registry(registry).liquidations()
    if not events:
        console.print("[dim]no liquidations recorded -- run `nat2 liq scan`[/dim]")
        return
    table = Table(title=f"liquidations ({len(events)} recorded, newest {limit})")
    for col in ("when", "coin", "liquidated", "mark", "notional", "method", "src"):
        table.add_column(col, justify="right")
    for event in events[-limit:]:
        table.add_row(
            to_dt(event.t_event).strftime("%m-%d %H:%M:%S"),
            event.coin,
            event.liquidated_user[:10],
            f"{event.mark_px:g}",
            f"${event.notional:,.0f}",
            event.method,
            event.source[:4],
        )
    console.print(table)
    split = method_notional(events)
    total = sum(split.values()) or 1.0
    console.print(
        " · ".join(f"{k} ${v / 1e6:.2f}M ({v / total:.0%})" for k, v in sorted(split.items()))
        + "   [dim](backstop = what the book could not absorb)[/dim]"
    )


@liq_app.command("coverage")
def liquidations_coverage(
    registry: RegistryOpt = REGISTRY,
    ledger: LedgerOpt = LEDGER,
) -> None:
    """Do the wallets that get liquidated overlap the wallets we map?

    The measurement that decides whether per-position scoring can ever work,
    or whether `gate map` has to fall back to cluster-level scoring. Each run
    is appended to the ledger, so the fraction is tracked over time rather
    than judged from one scan.
    """
    from nat2.core.registry import Registry
    from nat2.features.liqmath import effective
    from nat2.features.liquidations import population_overlap

    reg = Registry(registry)
    events = reg.liquidations()
    if not events:
        console.print("[dim]no liquidations recorded -- run `nat2 liq scan`[/dim]")
        raise typer.Exit(1)

    addresses = set(reg.addresses())
    mapped = {
        (p.address, p.coin) for p in reg.positions() if (effective(p)[0] or 0) > 0
    }
    overlap = population_overlap(events, addresses, mapped)

    table = Table(title="liquidated population vs registry")
    for col in ("measure", "by wallet", "by notional"):
        table.add_column(col, justify="right")
    table.add_row("in registry", f"{overlap.wallet_frac:.1%}", f"{overlap.notional_frac:.1%}")
    table.add_row("mapped (had a claimed price)",
                  f"{overlap.mapped_wallet_frac:.1%}", f"{overlap.mapped_notional_frac:.1%}")
    console.print(table)
    console.print(
        f"{overlap.events} liquidation(s), {overlap.wallets} distinct wallet(s), "
        f"${overlap.notional:,.0f} notional"
    )
    console.print(
        "[dim]per-position scoring lives on the notional number, not the wallet count[/dim]"
    )
    Ledger(ledger).append("observation", {"name": "liq_population", **overlap.summary()})


@wallets_app.command("replay")
def wallets_replay(
    reset: Annotated[bool, typer.Option("--reset", help="replay the whole store")] = False,
    root: RootOpt = RAW,
    registry: RegistryOpt = REGISTRY,
) -> None:
    """Fold captured tape into the registry, so the map is not six hours stale.

    Only tape newer than the watermark is applied, so running this twice is a
    no-op rather than a double count.
    """
    from nat2.core.registry import Registry
    from nat2.io.replay import replay, reset_watermark

    reg = Registry(registry)
    if reset:
        reset_watermark(reg)
    result = replay(reg, root)
    console.print(json.dumps(result, default=str))
    console.print(f"positions by source: {reg.source_counts()}")


@app.command("bars")
def bars_show(
    coin: str,
    interval: Annotated[str, typer.Option(help="bar width, e.g. 1m, 5m, 1h")] = "1m",
    limit: Annotated[int, typer.Option(help="most recent bars to show")] = 12,
    root: RootOpt = RAW,
) -> None:
    """Bars from the captured tape, with how late each one became usable."""
    from nat2.features.bars import bars, iter_prints
    from nat2.features.context import by_coin, features, iter_contexts
    from nat2.io.worm import read_records

    interval_ns = parse_window(interval)
    prints = iter_prints(read_records(root, "hl.trades"))
    built = bars(prints, interval_ns, coin=coin.upper())
    if not built:
        console.print(f"[red]no captured prints for {coin.upper()}[/red]")
        raise typer.Exit(1)

    table = Table(title=f"{coin.upper()} {interval} bars ({len(built)} captured)")
    for col in ("close", "open", "high", "low", "volume", "prints", "usable after close"):
        table.add_column(col, justify="right")
    for bar in built[-limit:]:
        # Negative would mean a bar usable before it finished forming; the
        # builder forbids it, so this column is a check as well as a report.
        lag = (bar.available_at - bar.t_close) / NS
        table.add_row(
            to_dt(bar.t_close).strftime("%H:%M:%S"), f"{bar.open:g}", f"{bar.high:g}",
            f"{bar.low:g}", f"{bar.volume:,.3f}", str(bar.prints), f"{lag:+.1f}s",
        )
    console.print(table)

    rows = features(by_coin(iter_contexts(read_records(root, "hl.assetctxs"))).get(coin.upper(), []))
    if rows:
        last = rows[-1]
        z = last["premium_z"]
        console.print(
            f"premium {last['premium'] * 1e4:+.2f}bp"
            f" (z {'n/a' if z is None else f'{z:+.2f}'}) · "
            f"funding {last['funding'] * 1e4:+.3f}bp/hr · "
            f"OI ${last['oi_notional'] / 1e6:,.0f}M · mark {last['mark']:g}"
        )


@app.command("features")
def features_build(
    coin: str,
    interval: Annotated[str, typer.Option(help="bar width, e.g. 1m, 5m")] = "1m",
    out: Annotated[Path, typer.Option("--out", help="write Parquet here")] = None,
    limit: Annotated[int, typer.Option(help="rows to display")] = 8,
    root: RootOpt = RAW,
    registry: RegistryOpt = REGISTRY,
) -> None:
    """Build the L0 feature frame for one coin, joined as-of each bar."""
    from nat2.core.registry import Registry
    from nat2.features.bars import bars, iter_prints
    from nat2.features.context import by_coin, iter_contexts
    from nat2.features.frame import build as build_frame
    from nat2.io.mapsnap import STREAM, series
    from nat2.io.worm import read_records

    name = coin.upper()
    built = bars(iter_prints(read_records(root, "hl.trades")), parse_window(interval), coin=name)
    if not built:
        console.print(f"[red]no captured prints for {name}[/red]")
        raise typer.Exit(1)

    contexts = by_coin(iter_contexts(read_records(root, "hl.assetctxs"))).get(name, [])
    maps = series(read_records(root, STREAM), name)
    events = [e for e in Registry(registry).liquidations() if e.coin == name]
    rows, stats = build_frame(built, contexts, maps, liquidations=events, coin=name)

    table = Table(title=f"{name} L0 frame — {stats.rows} rows, {interval} bars")
    for col in ("close", "ret", "sigma", "premium_z", "imb_002", "d_near_dn", "map_age_s", "tau"):
        table.add_column(col, justify="right")
    for row in rows[-limit:]:
        table.add_row(
            to_dt(row["t_close"]).strftime("%H:%M"),
            f"{row['ret'] * 1e4:+.1f}bp",
            _cell(row["sigma"], "{:.5f}"), _cell(row["premium_z"], "{:+.2f}"),
            _cell(row["imb_002"], "{:+.2f}"), _cell(row["d_near_dn"], "{:+.1f}"),
            _cell(row["map_age_s"], "{:.0f}s"), _cell(row["tau"], "{:.0f}"),
        )
    console.print(table)
    console.print(
        f"populated: map {stats.map_frac:.0%}, context {stats.context_frac:.0%}"
        + ("  [yellow](map history only starts when snapshots began)[/yellow]"
           if stats.map_frac < 1 else "")
    )

    if out:
        import polars as pl

        out.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(rows, infer_schema_length=None).write_parquet(out)
        console.print(f"wrote {stats.rows} rows -> {out}")


def _cell(value, fmt: str) -> str:
    """Missing renders as a dash, never as zero."""
    return "-" if value is None else fmt.format(value)


@app.command("eval")
def eval_expert(
    coin: str,
    horizon: Annotated[str, typer.Option(help="label horizon, e.g. 30m, 4h")] = "1h",
    interval: Annotated[str, typer.Option(help="bar width")] = "1m",
    splits: Annotated[int, typer.Option(help="walk-forward folds")] = 5,
    min_rows: Annotated[int, typer.Option(help="minimum labelled rows to fit")] = 200,
    k: Annotated[float, typer.Option("--k", help="barrier half-width, in sigma over the horizon")] = 1.0,
    include_timeouts: Annotated[bool, typer.Option("--include-timeouts",
        help="count an unfinished race as a miss")] = False,
    placebo: Annotated[int, typer.Option(help="permutation replications; 0 to skip")] = 0,
    seed: Annotated[int, typer.Option(help="placebo seed")] = 0,
    root: RootOpt = RAW,
    registry: RegistryOpt = REGISTRY,
) -> None:
    """Score an expert against its baseline, purged walk-forward, net of costs."""
    from nat2.core.costs import Costs
    from nat2.core.registry import Registry
    from nat2.experts.magnet_a import MagnetA, build_dataset
    from nat2.features.bars import bars, iter_prints, path
    from nat2.features.context import by_coin, iter_contexts
    from nat2.features.frame import build as build_frame
    from nat2.io.mapsnap import STREAM, series
    from nat2.io.worm import read_records
    from nat2.validate.evaluate import evaluate

    name = coin.upper()
    horizon_ns = parse_window(horizon)
    prints = iter_prints(read_records(root, "hl.trades"))
    built = bars(prints, parse_window(interval), coin=name)
    if not built:
        console.print(f"[red]no captured prints for {name}[/red]")
        raise typer.Exit(1)

    contexts = by_coin(iter_contexts(read_records(root, "hl.assetctxs"))).get(name, [])
    maps = series(read_records(root, STREAM), name)
    events = [e for e in Registry(registry).liquidations() if e.coin == name]
    rows, stats = build_frame(built, contexts, maps, liquidations=events, coin=name)

    expert = MagnetA(horizon_ns=horizon_ns, min_rows=min_rows)
    _PATHS[name] = path(prints, name)
    data, labels = build_dataset(
        rows, {name: _PATHS[name]}, horizon_ns, expert.features,
        bar_ns=parse_window(interval), k=k, include_timeouts=include_timeouts,
    )
    console.print(
        f"frame {stats.rows} rows (map {stats.map_frac:.0%}) -> "
        f"{len(data)} labelled, positive rate {data.positive_rate:.1%}"
    )
    console.print(
        f"[dim]dropped: {labels.no_sigma} no sigma, "
        f"{labels.unresolved} unresolved, {labels.timeouts} timeout"
        + ("" if include_timeouts else " (excluded)") + "[/dim]"
    )
    if not len(data):
        console.print(
            "[yellow]nothing labelled[/yellow] -- the magnet features need map "
            "snapshots, which only start accumulating once `nat2 cycle` runs"
        )
        raise typer.Exit(1)

    result = evaluate(expert, data, horizon_ns, Costs(), n_splits=splits)
    verdict = result.verdict()

    table = Table(title=f"{name} eval — {horizon} horizon, k={k:g}σ, {splits} folds")
    for col in ("", "n", "base rate", "log loss", "brier", "decisions", "hit rate"):
        table.add_column(col, justify="right")
    for side in ("expert", "baseline"):
        row = verdict[side]
        table.add_row(
            row["name"], str(row["n"]), f"{row['base_rate']:.1%}",
            f"{row['log_loss']:.4f}", f"{row['brier']:.4f}",
            str(row["decision_n"]),
            "-" if row["decision_hit_rate"] is None else f"{row['decision_hit_rate']:.1%}",
        )
    console.print(table)
    console.print(
        f"folds {verdict['folds']['folds']} · tested {verdict['folds']['tested_frac']:.0%} · "
        f"purged {verdict['folds']['purged']} · skipped {verdict['skipped_folds']} · "
        f"leaked {verdict['leaked']}"
    )
    console.print(
        f"costs {verdict['costs']['round_trip_bps']:.1f}bp round trip "
        f"(hash {verdict['costs']['hash']}) · threshold {verdict['threshold']:.3f}"
    )
    console.print(
        f"log-loss delta {verdict['delta']:+.4f} (z {verdict['delta_z']:+.2f}, "
        f"need {verdict['min_delta_z']:+.1f}) · constant floor "
        f"{verdict['constant_log_loss']:.4f} "
        + ("[green]cleared[/green]" if verdict["beats_constant"]
           else "[red]NOT cleared[/red]")
    )
    if placebo:
        _run_placebo(
            placebo, seed, rows, built, contexts, maps, events, name,
            expert, horizon_ns, parse_window(interval), k, include_timeouts,
            splits, result.delta_z,
        )

    if result.beats_baseline:
        console.print("[bold green]beats its baseline[/bold green] out of sample")
    else:
        console.print(
            "[bold red]does not beat its baseline[/bold red] -- "
            "the machinery is not earning its keep, so it does not enter the pool"
        )
    raise typer.Exit(0 if result.beats_baseline else 1)


def _run_placebo(replications, seed, rows, built, contexts, maps, events, coin,
                 expert, horizon_ns, bar_ns, k, include_timeouts, splits, real_z):
    """Re-run the pipeline with map masses shuffled across their own locations.

    Labels are invariant -- barriers are placed from volatility alone -- so only
    the features are rebuilt. Anything that survives was geometry, which is the
    confound `HYPOTHESIS_1.md` §5 exists to remove.
    """
    from nat2.core.costs import Costs
    from nat2.experts.magnet_a import MagnetA, build_dataset
    from nat2.features.bars import path
    from nat2.features.frame import build as build_frame
    from nat2.validate.evaluate import evaluate
    from nat2.validate.placebo import PlaceboResult, permute_series

    zs = []
    console.print(f"[dim]placebo: {replications} replication(s) …[/dim]")
    for i in range(replications):
        shuffled = permute_series({coin: maps}, seed + i)[coin]
        fake_rows, _ = build_frame(built, contexts, shuffled, liquidations=events, coin=coin)
        fake_data, _ = build_dataset(
            fake_rows, {coin: _PATHS[coin]}, horizon_ns, expert.features,
            bar_ns=bar_ns, k=k, include_timeouts=include_timeouts,
        )
        if not len(fake_data):
            continue
        fake = evaluate(MagnetA(horizon_ns=horizon_ns, min_rows=expert.min_rows),
                        fake_data, horizon_ns, Costs(), n_splits=splits)
        zs.append(fake.delta_z)

    outcome = PlaceboResult(real_z=real_z, placebo_z=zs)
    summary = outcome.summary()
    console.print(
        f"placebo z: mean {summary['mean_placebo_z']:+.2f}, max "
        f"{summary['max_placebo_z']:+.2f} over {summary['replications']} run(s) · "
        f"{summary['exceeded']} matched or beat the real {real_z:+.2f} · "
        f"p {summary['p_value']:.3f}"
    )
    if outcome.collapses():
        console.print("[green]effect collapses under permutation[/green] — mass, not geometry")
    else:
        console.print(
            "[bold red]effect survives permutation[/bold red] — indistinguishable "
            "from geometry, which is the null, not the hypothesis"
        )


_PATHS: dict = {}


@app.command("cycle")
def cycle(
    snapshot_every: Annotated[str, typer.Option(help="registry sweep interval")] = "6h",
    scan_every: Annotated[str, typer.Option(help="liquidation scan interval")] = "1h",
    observers: int = 40,
    wallet_limit: Annotated[int, typer.Option(help="0 = whole registry")] = 0,
    once: Annotated[bool, typer.Option("--once", help="run due jobs and exit")] = False,
    force: Annotated[bool, typer.Option("--force", help="with --once, ignore intervals")] = False,
    root: RootOpt = RAW,
    registry: RegistryOpt = REGISTRY,
    ledger: LedgerOpt = LEDGER,
    testnet: bool = False,
) -> None:
    """Snapshot, then observe. The cycle that makes `gate map` answerable.

    A liquidation only tests the map if the snapshot preceded it, so this runs
    forever: sweep the registry, scan for liquidations, and record the overlap
    each pass so `mapped_notional_frac` becomes a series rather than one
    confounded reading.
    """
    from nat2.io.cycle import Cycle, CycleConfig

    config = CycleConfig(
        registry_path=registry,
        ledger_path=ledger,
        snapshot_interval_ns=parse_window(snapshot_every),
        scan_interval_ns=parse_window(scan_every),
        raw_root=root,
        observers=observers,
        wallet_limit=wallet_limit,
        testnet=testnet,
    )

    def report(name: str, result: dict) -> None:
        console.print(f"[dim]{name}:[/dim] {json.dumps(result, default=str)[:200]}")

    async def _run() -> None:
        runner = Cycle(config, _budget(), on_event=report)
        console.print(f"[bold]cycle[/bold] {runner.status()}")
        if once:
            results = await runner.run_once(force=force)
            if not results:
                console.print("[dim]nothing due[/dim]")
            for name, result in results.items():
                report(name, result)
            return
        console.print("[dim]running until interrupted[/dim]")
        await runner.run()
        console.print(f"[dim]stopped -- {runner.status()}[/dim]")

    asyncio.run(_run())


@app.command("map")
def map_show(
    coin: str,
    registry: RegistryOpt = REGISTRY,
    buckets: Annotated[int, typer.Option(help="max rows to display each side")] = 12,
    min_share: Annotated[float, typer.Option(help="hide buckets under this %% of mapped notional")] = 1.0,
    resolution: Annotated[float, typer.Option(help="bucket width, percent")] = 0.125,
    span: Annotated[float, typer.Option(help="how far from mark to look, percent")] = 10.0,
) -> None:
    """Liquidation map for one coin, with the coverage number that qualifies it."""
    from nat2.core.registry import Registry
    from nat2.features.liqmap import OI_SIDES, build

    async def _run() -> None:
        marks, oi = await _marks_and_oi()
        if coin not in marks:
            console.print(f"[red]{coin} is not a listed perp[/red]")
            raise typer.Exit(1)
        positions = Registry(registry).positions(coin)
        liqmap = build(
            positions, coin, marks[coin], oi[coin],
            bucket_pct=resolution / 100.0, span=span / 100.0,
        )
        if not liqmap.total_notional:
            console.print(f"[red]no registry positions in {coin}[/red] -- snapshot first")
            raise typer.Exit(1)

        # Filter by mass, not by adjacency. Showing the nearest buckets fills
        # the screen with noise and pushes the clusters that actually carry the
        # notional off the bottom -- on BTC the two largest were nearly cut
        # while 81 near-empty buckets were being considered for display.
        # Relative to what is actually ON the map, not to total mapped
        # notional: most positions sit beyond the span and are in no bucket at
        # all, so a floor taken from the total emptied the table entirely for
        # every coin except BTC.
        in_span = sum(b.notional for b in liqmap.buckets)
        floor = in_span * min_share / 100.0
        rows = [b for b in liqmap.buckets if b.notional > floor]
        hidden_mass = sum(b.notional for b in liqmap.buckets if 0 < b.notional <= floor)
        above = [b for b in rows if b.low >= liqmap.mark][:buckets]
        below = [b for b in rows if b.high < liqmap.mark][-buckets:]
        peak = max((b.notional for b in above + below), default=1.0)

        table = Table(
            title=f"{coin} liquidation map   mark {liqmap.mark:g}   "
                  f"{resolution:g}% buckets, +/-{span:g}%"
        )
        for col in ("price", "%", "notional", "", "cross"):
            table.add_column(col, justify="right")
        for bucket in reversed(above):
            _map_row(table, bucket, liqmap.mark, peak)
        table.add_row("[bold]mark[/bold]", "", "", f"[bold]{liqmap.mark:g}[/bold]", "")
        for bucket in reversed(below):
            _map_row(table, bucket, liqmap.mark, peak)
        console.print(table)

        bands = ", ".join(f"{b:.1%} imb {liqmap.imbalance(b):+.2f}" for b in sorted(liqmap.up))
        shown = len(above) + len(below)
        console.print(
            f"coverage [bold]{liqmap.coverage:.1%}[/bold] of venue position notional "
            f"(OI x{OI_SIDES:g}) · {liqmap.positions} positions "
            f"({liqmap.published_frac:.0%} published, {liqmap.skipped} unplaceable, "
            f"{liqmap.outside_span} beyond +/-{span:g}%)"
        )
        hidden = len(rows) - shown
        if hidden > 0:
            console.print(
                f"[yellow]{hidden} cluster(s) over the floor not shown[/yellow] -- "
                "raise --buckets; bars scale to the largest bucket displayed"
            )
        if hidden_mass > 0:
            console.print(
                f"[dim]below the {min_share:g}% floor: ${hidden_mass / 1e6:,.1f}M "
                f"({hidden_mass / liqmap.total_notional:.0%} of mapped notional), "
                "spread thin[/dim]"
            )
        console.print(f"{bands}")

    asyncio.run(_run())


def _map_row(table, bucket, mark: float, peak: float) -> None:
    mid = (bucket.low + bucket.high) / 2
    distance = (mid - mark) / mark
    bar = "█" * max(1, int(24 * bucket.notional / peak))
    cross = bucket.cross_notional / bucket.notional if bucket.notional else 0
    table.add_row(
        f"{mid:g}", f"{distance:+.2%}", f"${bucket.notional / 1e6:,.1f}M", bar, f"{cross:.0%}"
    )


@gate_app.command("map")
def gate_map(
    coins: Annotated[str, typer.Option("--coins")] = "BTC,ETH,SOL",
    min_coverage: float = 0.25,
    registry: RegistryOpt = REGISTRY,
    ledger: LedgerOpt = LEDGER,
) -> None:
    """Run gate `map`: coverage, derivation fidelity, and predictive power."""
    from nat2.core.registry import Registry
    from nat2.features.liqmap import build
    from nat2.gates import map as gate
    from nat2.io.mapsnap import STREAM, iter_snapshots
    from nat2.io.worm import read_records

    async def _run() -> None:
        marks, oi = await _marks_and_oi()
        reg = Registry(registry)
        maps = [
            build(reg.positions(c), c, marks[c], oi[c])
            for c in (x.strip() for x in coins.split(","))
            if c in marks
        ]
        history: dict[str, list[dict]] = {}
        for row in iter_snapshots(read_records(RAW, STREAM)):
            history.setdefault(row["coin"], []).append(row)
        verdict, checks = gate.run(
            reg, maps, Ledger(ledger), min_coverage=min_coverage, map_series=history
        )
        table = Table(title="gate map")
        for col in ("", "coin", "check", "detail"):
            table.add_column(col, overflow="fold")
        for check in checks:
            mark = "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]"
            table.add_row(mark, check.stream, check.name, check.detail)
        console.print(table)
        console.print(
            "[bold green]gate map PASS[/bold green]"
            if verdict.passed
            else "[bold red]gate map FAIL[/bold red] -- recorded; downstream will refuse"
        )
        raise typer.Exit(0 if verdict.passed else 1)

    asyncio.run(_run())


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
