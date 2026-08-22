"""nat2 report — the daily / weekly digest (TASK_2/14, OBSERVATORY_DESIGN §4).

One script, one HTML file per day (`~/www/reports/<date>.html`) and one per ISO week on
Mondays. Same rules as the status page: every number carries its as-of time, an empty
home says "no data" rather than zeros, no scripts on the page. Unlike the status page
this runs under the repo venv (the per-pair table reads the zstd tape), so it also
leaves `data/ops/report_state.json` behind -- the stdlib status page renders the pair
table and accrual bars from that sidecar and keeps its files-only guarantee.

Sections: system · gate ladder with accrual bars · pairs (roster) · action log since the
last digest, by level · incidents. Weekly adds the observation series and next dates.

CLI: ``report.py [--weekly] [--out DIR] [--now NS] [--home PATH]``; ntfy gets one line.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "deploy"))
import statuspage as sp  # noqa: E402  (same directory; metric/_iso/_age/svg_lines/write_atomic)
from gapwatch import notify  # noqa: E402
from nat2.core.clock import NS  # noqa: E402
from nat2.core.guard import latest as latest_verdict  # noqa: E402
from nat2.core.roster import KIND as ROSTER_KIND  # noqa: E402
from nat2.core.roster import RosterSpec, evaluate  # noqa: E402
from nat2.features.bars import bars, iter_prints  # noqa: E402
from nat2.features.context import iter_contexts, latest  # noqa: E402
from nat2.io import actions  # noqa: E402
from nat2.io.mapsnap import STREAM, iter_snapshots  # noqa: E402
from nat2.io.worm import read_records  # noqa: E402
from nat2.ledger.chain import Ledger  # noqa: E402

HOUR = 3600 * NS
SIGMA_WINDOW_NS = 6 * HOUR          # one-minute sigma over the last six hours
MIN_BARS_FOR_SIGMA = 60
LEVELS = {"L0": "ops", "L1": "observation", "L2": "research", "L3": "signal (shadow)"}


# ---------------------------------------------------------------- collectors

def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    return (sum((v - m) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def pairs(home: Path, now_ns: int) -> dict:
    """Per-pair rows for the roster in force (ledgered, else evaluated read-only and said so)."""
    root, ledger = home / "data" / "raw", Ledger(home / "data" / "ledger.jsonl")
    contexts = latest(iter_contexts(read_records(root, "hl.assetctxs", since_ns=now_ns - 3 * HOUR)))
    if not contexts:
        return {"rows": [], "source": "no cross-section in the last 3h", "asof": None}
    verdict = latest_verdict(ledger, "map")
    coverage = verdict.detail.get("coverage", {}) if verdict else {}
    entry = ledger.latest(ROSTER_KIND, name=ROSTER_KIND)
    if entry:
        observed, source = list(entry.payload["observed"]) + list(entry.payload["b_roster"]), f"roster seq {entry.seq}"
    else:
        r = evaluate(RosterSpec.load(home / "pairs.toml"), {c: x.day_volume for c, x in contexts.items()}, coverage)
        observed, source = list(r.captured), "roster evaluated read-only (none ledgered yet)"
    snaps: dict[str, dict] = {}
    for row in iter_snapshots(read_records(root, STREAM, since_ns=now_ns - 12 * HOUR)):
        snaps[row["coin"]] = row
    prints = iter_prints(read_records(root, "hl.trades", since_ns=now_ns - SIGMA_WINDOW_NS))
    liqs = _liquidations(home / "data" / "registry.sqlite", now_ns - 24 * HOUR)
    rows = []
    for coin in observed:
        ctx, snap, liq = contexts.get(coin), snaps.get(coin), liqs.get(coin, {})
        rets = [b.ret for b in bars([p for p in prints if p.coin == coin], 60 * NS, coin=coin)]
        sigma = _stdev(rets) if len(rets) >= MIN_BARS_FOR_SIGMA else None
        rows.append({
            "coin": coin,
            "mark": ctx.mark if ctx else None, "day_volume": ctx.day_volume if ctx else None,
            "funding": ctx.funding if ctx else None, "oi_notional": ctx.oi_notional if ctx else None,
            "ctx_asof": ctx.t_ingest / NS if ctx else None,
            "sigma_1m": sigma, "sigma_1h": sigma * 60 ** 0.5 if sigma else None, "bars": len(rets),
            "coverage": coverage.get(coin),
            "imb_002": (snap or {}).get("imb", {}).get("0.02"), "imb_005": (snap or {}).get("imb", {}).get("0.05"),
            "near_up": (snap or {}).get("near", {}).get("up_dist"), "near_dn": (snap or {}).get("near", {}).get("down_dist"),
            "map_asof": snap["t_ingest"] / NS if snap else None,
            "liq_n": liq.get("n", 0), "liq_notional": liq.get("notional", 0.0), "liq_max_minute": liq.get("max_minute", 0.0),
        })
    return {"rows": rows, "source": source, "asof": now_ns / NS}


def _liquidations(db: Path, since_ns: int) -> dict[str, dict]:
    if not db.exists():
        return {}
    out: dict[str, dict] = {}
    minutes: dict[tuple[str, int], float] = {}
    with sqlite3.connect(db) as con:
        for coin, t_event, px, sz in con.execute(
                "SELECT coin, t_event, px, sz FROM liquidations WHERE t_event > ?", (since_ns,)):
            notional = float(px) * float(sz)
            agg = out.setdefault(coin, {"n": 0, "notional": 0.0, "max_minute": 0.0})
            agg["n"] += 1
            agg["notional"] += notional
            key = (coin, int(t_event // (60 * NS)))
            minutes[key] = minutes.get(key, 0.0) + notional
            agg["max_minute"] = max(agg["max_minute"], minutes[key])
    return out


def accrual(gates: dict) -> list[dict]:
    """Progress toward each forward window, from the latest gate entries' own `window` details."""
    out = []
    for gate, needs in (("map", (("scored", "need"),)), ("magnet", (("scored", "need"), ("days", "need_days")))):
        e = gates.get(gate)
        window = ((e or {}).get("payload") or {}).get("detail", {}).get("window") or {}
        bars_ = [{"label": k, "have": window.get(k), "need": window.get(n)} for k, n in needs if window.get(n)]
        out.append({"gate": gate, "bars": bars_, "asof": e["ts"] / 1e9 if e else None,
                    "note": None if bars_ else ("never run" if not e else "refused before counting")})
    return out


def events_ahead(home: Path, now_ns: int, horizon_ns: int = 48 * HOUR) -> list[dict]:
    out = []
    for path in sorted((home / "data" / "events").glob("*.ndjson")):
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("source_ts")
            if rec.get("class") == "scheduled" and isinstance(ts, int) and now_ns <= ts <= now_ns + horizon_ns:
                out.append({"event_id": rec.get("event_id"), "source": rec.get("source"), "ts": ts / NS})
    return sorted({e["event_id"]: e for e in out}.values(), key=lambda e: e["ts"])


def collect(home: Path, now_ns: int, weekly: bool) -> dict:
    now_s = now_ns / NS
    base = sp.collect(now_s, sp.Paths(ledger=home / "data" / "ledger.jsonl",
                                      gapwatch_state=home / "data" / "ops" / "gapwatch_state.json",
                                      manifest=home / "data" / "raw" / "_manifest.jsonl",
                                      events_dir=home / "data" / "events"))
    ledger = Ledger(home / "data" / "ledger.jsonl")
    ok, chain_msg = ledger.verify()
    since = now_ns - (7 * 24 if weekly else 24) * HOUR
    acts = actions.read(home, since_ns=since)
    usage = shutil.disk_usage(home)
    incidents = [e for e in ledger.entries() if e.kind == "incident" and e.ts >= now_ns - 7 * 24 * HOUR]
    return {
        **base, "now_ns": now_ns, "weekly": weekly, "home": str(home),
        "ledger_seq": (len(ledger.entries()) - 1) if ledger.entries() else None, "chain_ok": ok, "chain_msg": chain_msg,
        "disk_free_gb": usage.free / 1e9, "disk_total_gb": usage.total / 1e9,
        "accrual": accrual(base["gates"]), "pairs": pairs(home, now_ns), "events_ahead": events_ahead(home, now_ns),
        "actions": acts, "since_s": since / NS, "incidents": incidents,
        "holes": (base["gapwatch"].get("holes") or []),
    }


# ---------------------------------------------------------------- render

def _num(v, fmt: str = "{:,.0f}") -> str:
    return "—" if v is None else fmt.format(v)


def _bar(have, need) -> str:
    frac = 0.0 if not need else min(1.0, float(have or 0) / float(need))
    return (f'<span class="bar"><span class="fill" style="width:{frac * 100:.0f}%"></span></span> '
            f'{_num(have)} / {_num(need)}')


def render(d: dict) -> str:
    now = d["now_s"]
    iso, age, metric = sp._iso, sp._age, sp.metric
    gw = d["gapwatch"]
    gap_min = gw.get("gap_minutes", {}) if isinstance(gw.get("gap_minutes"), dict) else {}
    gw_asof = iso(d["gapwatch_asof"])
    system = "".join([
        f"<li>ledger seq {metric('—' if d['ledger_seq'] is None else d['ledger_seq'], iso(now))} · {metric(d['chain_msg'], iso(now), '' if d['chain_ok'] else 'bad')}</li>",
        f"<li>disk free {metric(f'{d['disk_free_gb']:.0f} / {d['disk_total_gb']:.0f} GB', iso(now), 'bad' if d['disk_free_gb'] < 20 else '')}</li>",
        f"<li>gap-minutes this week: {metric(', '.join(f'{k} {v:.0f}' for k, v in sorted(gap_min.items())) or 'none', gw_asof)} (budget {sp.WEEK_BUDGET_MIN:.0f}/stream)</li>",
        f"<li>open gapwatch conditions: {metric(', '.join(sorted(gw.get('open') or {})) or 'none', gw_asof)}</li>",
        "".join(f"<li>{html.escape(u)}: {metric(st, gw_asof, '' if st == 'active' else 'bad')}</li>"
                for u, st in sorted((gw.get("units") or {}).items())),
        "<li>backup: " + metric("none configured (TASK_2/12)", iso(now), "warn") + "</li>",
    ])
    gates = "".join(
        f"<tr><td>{html.escape(g)}</td><td>{metric('PASS' if (e['payload'] or {}).get('passed') else ((e['payload'] or {}).get('detail') or {}).get('verdict', 'FAIL').upper(), iso(e['ts'] / 1e9))}</td>"
        f"<td>{html.escape(str(((e['payload'] or {}).get('detail') or {}).get('reason') or '—'))}</td><td>seq {e['seq']}</td></tr>"
        for g, e in sorted(d["gates"].items())) or '<tr><td colspan=4 class="warn">no gate has run</td></tr>'
    accr = "".join(
        f"<li>{html.escape(a['gate'])}: " + (" · ".join(f"{b['label']} {_bar(b['have'], b['need'])}" for b in a["bars"])
                                               if a["bars"] else f'<span class="warn">{a["note"]}</span>')
        + (f" {metric('', iso(a['asof']))}" if a["asof"] else "") + "</li>" for a in d["accrual"])
    p = d["pairs"]
    prow = []
    for r in p["rows"]:
        ctx_asof, map_asof = iso(r["ctx_asof"]), iso(r["map_asof"])
        prow.append(
            f"<tr><td>{html.escape(r['coin'])}</td><td>{metric(_num(r['mark'], '{:,.6g}'), ctx_asof)}</td>"
            f"<td>{metric(_num(r['day_volume'] and r['day_volume'] / 1e6, '{:,.1f}M'), ctx_asof)}</td>"
            f"<td>{metric(_num(r['funding'] and r['funding'] * 1e4, '{:+.2f}bp/h'), ctx_asof)}</td>"
            f"<td>{metric(_num(r['oi_notional'] and r['oi_notional'] / 1e6, '{:,.0f}M'), ctx_asof)}</td>"
            f"<td>{metric(_num(r['sigma_1m'] and r['sigma_1m'] * 1e4, '{:.1f}bp') + ' / ' + _num(r['sigma_1h'] and r['sigma_1h'] * 100, '{:.2f}%'), iso(now) + f' ({r["bars"]} bars)')}</td>"
            f"<td>{metric(_num(r['coverage'], '{:.0%}'), map_asof)}</td>"
            f"<td>{metric(_num(r['imb_002'], '{:+.2f}') + ' / ' + _num(r['imb_005'], '{:+.2f}'), map_asof)}</td>"
            f"<td>{metric(_num(r['near_up'], '{:+.2%}') + ' / ' + _num(r['near_dn'], '{:+.2%}'), map_asof)}</td>"
            f"<td>{metric(f'{r['liq_n']} · ${r['liq_notional'] / 1e6:.2f}M · max-min ${r['liq_max_minute'] / 1e3:.0f}k', iso(now))}</td></tr>")
    pairs_table = ("<table><tr><th>pair</th><th>mark</th><th>24h vol</th><th>funding</th><th>OI</th><th>σ 1m / 1h</th>"
                   "<th>coverage</th><th>imb 2% / 5%</th><th>nearest ↑ / ↓</th><th>liquidations 24h</th></tr>"
                   + "".join(prow) + "</table>") if p["rows"] else f'<p class="warn">no pairs: {html.escape(p["source"])}</p>'
    by_level: dict[str, list[dict]] = {}
    for a in d["actions"]:
        by_level.setdefault(a["level"], []).append(a)
    acts = "".join(
        f"<h3>{lvl} {LEVELS[lvl]} — {len(by_level.get(lvl, []))}</h3><ul>" + "".join(
            f"<li>{iso(a['t_ingest'] / 1e9)} <code>{html.escape(a['kind'])}</code> "
            f"{html.escape(json.dumps(a['payload'], default=str)[:160])}</li>" for a in by_level.get(lvl, [])[-12:])
        + ("" if by_level.get(lvl) else '<li class="warn">none</li>') + "</ul>" for lvl in LEVELS)
    incidents = "".join(
        f"<li>seq {e.seq} {iso(e.ts / 1e9)} <code>{html.escape(str(e.payload.get('name')))}</code> "
        f"{html.escape(str(e.payload.get('cause') or ''))[:160]}</li>" for e in d["incidents"]) + "".join(
        f"<li>hole {h['minutes']:.0f}m {iso(h['from_s'])} → {iso(h['to_s'])} (gapwatch)</li>" for h in d["holes"]) \
        or '<li>none in the last 7 days</li>'
    events = "".join(f"<li>{iso(e['ts'])} <code>{html.escape(str(e['source']))}</code> {html.escape(str(e['event_id']))}</li>"
                     for e in d["events_ahead"]) or "<li>none scheduled in the next 48h</li>"
    weekly = ""
    if d["weekly"]:
        weekly = (f"<h2>6. Observation series ({len(d['obs'])} rows)</h2>{sp.svg_lines(d['obs'], sp.OBS_SERIES)}"
                  "<h2>7. Next dates</h2><ul>"
                  + "".join(f"<li>{html.escape(a['gate'])}: " + (", ".join(f"{b['label']} {_num(b['have'])}/{_num(b['need'])}" for b in a["bars"]) or a["note"]) + "</li>" for a in d["accrual"])
                  + "</ul>")
    kind = "weekly" if d["weekly"] else "daily"
    return f"""<!doctype html>
<meta charset="utf-8"><title>nat2 {kind} digest {iso(now)[:10]}</title>
<style>
body{{font:14px/1.4 system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em;color:#222}}
h1,h2{{font-weight:600}} h2{{margin-top:1.6em;border-bottom:1px solid #ddd}} h3{{font-weight:600;margin:.8em 0 .2em}}
table{{border-collapse:collapse;width:100%;font-size:.92em}} td,th{{text-align:left;padding:.2em .4em;border-bottom:1px solid #eee;white-space:nowrap}}
.m{{font-weight:600}} .asof{{color:#888;font-size:.75em;margin-left:.3em}} .asof::before{{content:"as of "}}
.bad{{color:#c22}} .warn{{color:#b70}} code{{background:#f4f4f4;padding:0 .2em}}
.bar{{display:inline-block;width:120px;height:.7em;background:#eee;vertical-align:middle}} .fill{{display:block;height:100%;background:#1f77b4}}
.grid{{stroke:#ddd}} .tick{{font-size:10px;fill:#888}} .legend{{font-size:.85em}} .wrap{{overflow-x:auto}}
</style>
<h1>nat2 {kind} digest — {iso(now)}</h1>
<p>window since {iso(d['since_s'])} · home <code>{html.escape(d['home'])}</code> · every number carries its as-of time; a missing number is shown as —, never as 0.</p>
<h2>1. System</h2><ul>{system}</ul>
<h2>2. Gate ladder</h2><table><tr><th>gate</th><th>verdict</th><th>reason</th><th>entry</th></tr>{gates}</table>
<h3>Accrual toward the forward windows</h3><ul>{accr}</ul>
<h2>3. Pairs ({len(p['rows'])}) — {html.escape(p['source'])}</h2><div class="wrap">{pairs_table}</div>
<h3>Scheduled events, next 48h</h3><ul>{events}</ul>
<h2>4. Actions since the last digest ({len(d['actions'])})</h2>{acts}
<h2>5. Incidents and holes</h2><ul>{incidents}</ul>
{weekly}
<p class="legend">read-only · no scripts · written by nat2-report.timer</p>
"""


def sidecar(d: dict) -> dict:
    """What the stdlib status page renders from this run: pairs and accrual, with their as-of times."""
    return {"generated_s": d["now_s"], "pairs": d["pairs"], "accrual": d["accrual"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--out", type=Path, default=Path.home() / "www" / "reports")
    ap.add_argument("--home", type=Path, default=ROOT)
    ap.add_argument("--now", type=int, default=None, help="epoch ns (tests)")
    ap.add_argument("--url", default="", help="public base URL for the ntfy line")
    a = ap.parse_args()
    now_ns = a.now if a.now is not None else time.time_ns()
    weekly = a.weekly or datetime.fromtimestamp(now_ns / NS, timezone.utc).weekday() == 0
    d = collect(a.home, now_ns, weekly)
    day = datetime.fromtimestamp(now_ns / NS, timezone.utc)
    name = f"week-{day.strftime('%G-W%V')}.html" if weekly else f"{day.strftime('%Y-%m-%d')}.html"
    sp.write_atomic(a.out / name, render(d))
    sp.write_atomic(a.home / "data" / "ops" / "report_state.json", json.dumps(sidecar(d), indent=1, default=str))
    line = (f"nat2 {'weekly' if weekly else 'daily'} digest: {len(d['pairs']['rows'])} pairs, {len(d['actions'])} actions, "
            f"ledger seq {'—' if d['ledger_seq'] is None else d['ledger_seq']} {'intact' if d['chain_ok'] else 'BROKEN'}"
            + (f" · {a.url}/{name}" if a.url else ""))
    notify(line, priority="default" if d["chain_ok"] else "high")
    print(f"wrote {a.out / name}")


if __name__ == "__main__":
    main()
