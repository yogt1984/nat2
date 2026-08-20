"""nat2 statuspage — one generator, one self-contained HTML file (TASK_2/06, C0).

READS FILES ONLY: the ledger, gapwatch's state JSON (which carries the unit
states and gap-minutes it already computes), the capture manifest, directory
mtimes. It never calls HL, never runs nat2 commands, never queries systemd: if
the page needs data no file provides, the producer grows the file. Zero JS —
the chart is an inline SVG polyline. Every number carries its as-of stamp
(C5: a number without its status is a bug); the golden test counts them.

CLI: ``statuspage.py --out PATH [--now EPOCH]``. Stdlib only, system python3.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEEK_BUDGET_MIN = 60.0  # REDUCED_SPECS §7.2, same constant as gapwatch
STALE_GEN_S = 30 * 60  # 3 missed 10-min timer ticks: the page says so itself
MANIFEST_TAIL_BYTES = 65536
OBS_NAME = "liq_population"
OBS_SERIES = ("notional_frac", "mapped_notional_frac", "wallet_frac", "mapped_wallet_frac")
PALETTE = ("#1f77b4", "#d62728", "#2ca02c", "#ff7f0e")


@dataclass(frozen=True)
class Paths:
    ledger: Path = ROOT / "data" / "ledger.jsonl"
    gapwatch_state: Path = ROOT / "data" / "ops" / "gapwatch_state.json"
    manifest: Path = ROOT / "data" / "raw" / "_manifest.jsonl"
    events_dir: Path = ROOT / "data" / "events"
    nat_data: Path = Path.home() / "nat" / "data"
    nat_results: Path = Path.home() / "nat" / "experiment_results"


# ---------------------------------------------------------------- collectors

def _iso(ts_s: float | None) -> str:
    if ts_s is None:
        return "never"
    return datetime.fromtimestamp(ts_s, timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _age(now_s: float, ts_s: float | None) -> str:
    if ts_s is None:
        return "n/a"
    s = max(0.0, now_s - ts_s)
    return f"{s / 86400:.1f}d" if s >= 86400 else f"{s / 3600:.1f}h" if s >= 3600 else f"{s / 60:.0f}m"


def _newest_mtime(base: Path, patterns: tuple[str, ...]) -> float | None:
    newest = None
    if base.exists():
        for pat in patterns:
            for f in base.rglob(pat):
                try:
                    mt = f.stat().st_mtime
                except OSError:
                    continue
                newest = mt if newest is None else max(newest, mt)
    return newest


def read_ledger(path: Path) -> tuple[dict, dict, list]:
    """(latest gate per name, latest preregistration per name, observation rows)."""
    gates: dict[str, dict] = {}
    prereg: dict[str, dict] = {}
    obs: list[dict] = []
    if not path.exists():
        return gates, prereg, obs
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind, p = e.get("kind"), e.get("payload", {})
        if kind == "gate":
            gates[p.get("gate", "?")] = e
        elif kind == "preregistration":
            prereg[p.get("name", "?")] = e
        elif kind == "observation" and p.get("name") == OBS_NAME:
            obs.append({"ts": e["ts"] / 1e9, **{k: p.get(k) for k in OBS_SERIES}})
    return gates, prereg, obs


def read_manifest_ages(path: Path) -> dict[str, float]:
    """{stream: newest last_ingest epoch s} from the manifest tail (gapwatch idiom)."""
    out: dict[str, float] = {}
    if not path.exists():
        return out
    with path.open("rb") as fh:
        fh.seek(max(0, path.stat().st_size - MANIFEST_TAIL_BYTES))
        lines = fh.read().decode(errors="replace").splitlines()
    for line in lines[1:] if path.stat().st_size > MANIFEST_TAIL_BYTES else lines:
        try:
            e = json.loads(line)
            out[e["stream"]] = max(out.get(e["stream"], 0.0), e["last_ingest"] / 1e9)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return out


def collect(now_s: float, paths: Paths = Paths()) -> dict:
    """Pure-ish snapshot of every displayed fact (reads files, no side effects)."""
    gates, prereg, obs = read_ledger(paths.ledger)
    try:
        gw = json.loads(paths.gapwatch_state.read_text())
        gw_asof = paths.gapwatch_state.stat().st_mtime
    except (OSError, json.JSONDecodeError):
        gw, gw_asof = {}, None
    results = sorted(paths.nat_results.glob("result__*.md")) if paths.nat_results.exists() else []
    last_result = results[-1].name.split("__")[1] if results else None
    month = datetime.fromtimestamp(now_s, timezone.utc).strftime("%Y-%m")
    return {
        "now_s": now_s,
        "gates": gates,
        "prereg": prereg,
        "obs": obs,
        "gapwatch": gw,
        "gapwatch_asof": gw_asof,
        "stream_ingest": read_manifest_ages(paths.manifest),
        "nat_parquet_mtime": _newest_mtime(paths.nat_data, ("*.parquet", "*.parquet.tmp")),
        "nat_last_result": last_result,
        "nat_results_this_month": sum(1 for r in results if f"__{month}" in r.name),
        "events_mtime": _newest_mtime(paths.events_dir, ("*.ndjson",)),
    }


# ----------------------------------------------------------------- rendering

def _short(v) -> str:
    return json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else str(v)


def metric(value: str, asof: str, cls: str = "") -> str:
    """The only way a number reaches the page: value + visible as-of, paired."""
    return (f'<span class="m {cls}">{html.escape(str(value))}</span>'
            f'<span class="asof">{html.escape(asof)}</span>')


def svg_lines(rows: list[dict], keys: tuple[str, ...], w: int = 720, h: int = 180) -> str:
    """Inline SVG polylines, y in [0, 1], x over the rows' time span. No JS."""
    pts = [r for r in rows if all(isinstance(r.get(k), (int, float)) for k in keys)]
    if len(pts) < 2:
        return '<p class="warn">observation series: fewer than 2 points</p>'
    t0, t1 = pts[0]["ts"], pts[-1]["ts"]
    span = max(t1 - t0, 1.0)
    pad = 28
    def x(t): return pad + (t - t0) / span * (w - 2 * pad)
    def y(v): return h - pad - v * (h - 2 * pad)
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="observation series">']
    for frac in (0.0, 0.5, 1.0):
        out.append(f'<line x1="{pad}" y1="{y(frac):.1f}" x2="{w - pad}" y2="{y(frac):.1f}" class="grid"/>'
                   f'<text x="2" y="{y(frac) + 4:.1f}" class="tick">{frac:.1f}</text>')
    for k, color in zip(keys, PALETTE):
        poly = " ".join(f"{x(r['ts']):.1f},{y(min(max(r[k], 0.0), 1.0)):.1f}" for r in pts)
        out.append(f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.5"/>')
    out.append(f'<text x="{pad}" y="{h - 8}" class="tick">{_iso(t0)}</text>'
               f'<text x="{w - pad}" y="{h - 8}" class="tick" text-anchor="end">{_iso(t1)}</text></svg>')
    legend = " ".join(f'<span style="color:{c}">■ {k}</span>' for k, c in zip(keys, PALETTE))
    return "".join(out) + f'<p class="legend">{legend}</p>'


def render(d: dict) -> str:
    now = d["now_s"]
    rows: list[str] = []
    # 1. gate ladder
    for name, e in sorted(d["gates"].items()):
        p = e["payload"]
        verdict = "PASS" if p.get("passed") else "FAIL"
        failed = ", ".join(p.get("detail", {}).get("failed", [])) or "—"
        cites = p.get("detail", {}).get("judged_against", [])
        rows.append(f"<tr><td>{html.escape(name)}</td><td>{metric(verdict, _iso(e['ts'] / 1e9), verdict.lower())}"
                    f"</td><td>seq {e['seq']}</td><td>{html.escape(failed)}</td>"
                    f"<td>{html.escape(', '.join(map(str, cites))) or '—'}</td></tr>")
    gate_table = ("<table><tr><th>gate</th><th>verdict</th><th>entry</th><th>failed checks</th>"
                  "<th>judged against</th></tr>" + "".join(rows) + "</table>")
    pre = "".join(
        f"<li>seq {e['seq']} <code>{html.escape(n)}</code> "
        f"{metric(_short(e['payload'].get('pass_if') or e['payload'].get('min_scoreable_events', '—')), _iso(e['ts'] / 1e9))}</li>"
        for n, e in sorted(d["prereg"].items(), key=lambda kv: kv[1]["seq"]))
    # 2. observation series
    last_obs = d["obs"][-1] if d["obs"] else None
    obs_now = "".join(
        f"<li>{k}: {metric(f'{last_obs[k]:.3f}' if isinstance(last_obs.get(k), (int, float)) else 'n/a', _iso(last_obs['ts']))}</li>"
        for k in OBS_SERIES) if last_obs else '<li class="warn">no observations in ledger</li>'
    # 3. capture & feed health
    gw = d["gapwatch"]
    gw_asof = _iso(d["gapwatch_asof"])
    streams = "".join(
        f"<tr><td>{html.escape(s)}</td><td>{metric(_age(now, t), _iso(t), 'bad' if now - t > 7200 else '')}</td>"
        f"<td>{metric(f'{gw.get('gap_minutes', {}).get(f'stream:{s}', 0.0):.1f} / {WEEK_BUDGET_MIN:.0f}', gw_asof, 'bad' if gw.get('gap_minutes', {}).get(f'stream:{s}', 0.0) > WEEK_BUDGET_MIN else '')}</td></tr>"
        for s, t in sorted(d["stream_ingest"].items()))
    units = "".join(
        f"<li>{html.escape(u)}: {metric(st, gw_asof, '' if st == 'active' else 'bad')}</li>"
        for u, st in sorted(gw.get("units", {}).items())) or '<li class="warn">gapwatch state carries no unit states yet</li>'
    open_conds = ", ".join(sorted(gw.get("open", {}))) or "none"
    # 4. nat health + 5. events
    pq = d["nat_parquet_mtime"]
    ev = d["events_mtime"]
    gen_stale = (f'<p class="warn">If "generated" is older than {STALE_GEN_S // 60} min, nat2-statuspage.timer is dead '
                 f'(gapwatch alerts on it); every other number below is at least that stale.</p>')
    return f"""<!doctype html>
<meta charset="utf-8"><meta http-equiv="refresh" content="600">
<title>nat2 status</title>
<style>
body{{font:14px/1.4 system-ui,sans-serif;max-width:760px;margin:2em auto;padding:0 1em;color:#222}}
h1,h2{{font-weight:600}} h2{{margin-top:1.6em;border-bottom:1px solid #ddd}}
table{{border-collapse:collapse;width:100%}} td,th{{text-align:left;padding:.25em .5em;border-bottom:1px solid #eee}}
.m{{font-weight:600}} .asof{{color:#888;font-size:.8em;margin-left:.4em}} .asof::before{{content:"as of "}}
.pass{{color:#2a7}} .fail,.bad{{color:#c22}} .warn{{color:#b70}} .grid{{stroke:#ddd}} .tick{{font-size:10px;fill:#888}}
.legend{{font-size:.85em}} code{{background:#f4f4f4;padding:0 .2em}}
</style>
<h1>nat2 status</h1>
<p>generated {metric(_iso(now), _iso(now))} · week {html.escape(str(gw.get('week', '?')))} · open gapwatch conditions: {metric(open_conds, gw_asof, '' if open_conds == 'none' else 'bad')}</p>
{gen_stale}
<h2>1. Gate ladder</h2>{gate_table}
<h3>Pre-registrations</h3><ul>{pre or '<li class="warn">none</li>'}</ul>
<h2>2. Observation series (<code>{OBS_NAME}</code>, {len(d['obs'])} rows)</h2>
{svg_lines(d['obs'], OBS_SERIES)}<ul>{obs_now}</ul>
<h2>3. Capture &amp; feed health</h2>
<table><tr><th>stream</th><th>last ingest age</th><th>gap-min this week / budget</th></tr>{streams}</table>
<h3>Units (recorded by gapwatch)</h3><ul>{units}</ul>
<h2>4. nat health</h2><ul>
<li>newest feature parquet: {metric(_age(now, pq), _iso(pq), 'bad' if pq is None or now - pq > 7200 else '')}</li>
<li>last experiment result: {metric(d['nat_last_result'] or 'none', _iso(now))}</li>
<li>results this month: {metric(d['nat_results_this_month'], _iso(now), 'bad' if d['nat_results_this_month'] == 0 else '')} (B4 cadence)</li>
</ul>
<h2>5. Event log</h2><ul>
<li>newest event record: {metric(_age(now, ev), _iso(ev), 'bad' if ev is None or now - ev > 1800 else '')}</li>
</ul>
<p class="legend">read-only · no scripts · regenerated every 10 min by nat2-statuspage.timer</p>
"""


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path.home() / "www" / "status" / "status.html")
    ap.add_argument("--now", type=float, default=None, help="epoch seconds (tests)")
    a = ap.parse_args()
    write_atomic(a.out, render(collect(a.now if a.now is not None else time.time())))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
