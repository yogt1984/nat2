"""Event sources for evlog: (name, poll_fn) pairs, nothing else.

Each poll_fn takes a fetch callable ``fetch(url) -> str`` and returns a list of
partial records ``{"class", "event_id", "source_ts", "payload"}``; evlog.py adds
``schema``, ``source`` and ``receipt_ts`` and owns dedup. Parsers use ``re`` and
``json`` only. A source that needs a browser is dropped and documented in
``data/events/README.md`` (OPEC: Cloudflare challenge on every page, 2026-08-20).

Times: ET wall-clock from the publishers is converted with zoneinfo; every
``source_ts`` is epoch nanoseconds UTC, the same unit as ``receipt_ts``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BLS_SCHEDULE_URL = "https://www.bls.gov/schedule/news_release/cpi.htm"
BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0?latest=true"
TRUTH_URL = "https://truthsocial.com/api/v1/accounts/107780257626128497/statuses?limit=20"


def _ns(dt: datetime) -> int:
    return int(dt.timestamp()) * 1_000_000_000 + dt.microsecond * 1000


def et_ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return _ns(datetime(year, month, day, hour, minute, tzinfo=ET))


def iso_ns(text: str) -> int:
    return _ns(datetime.fromisoformat(text.replace("Z", "+00:00")))


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def fomc(fetch) -> list[dict]:
    """One scheduled record per meeting (statement 14:00 ET on the last day),
    plus a released record once the statement link is on the page."""
    html = fetch(FOMC_URL)
    out = []
    for ym in re.finditer(r'id="\d+">(\d{4}) FOMC Meetings</a>(.*?)(?=<div class="panel panel-default">|$)', html, re.S):
        year = int(ym.group(1))
        for m in re.finditer(r'fomc-meeting__month[^>]*>\s*<strong>(\w+)(?:/(\w+))?</strong>.*?fomc-meeting__date[^>]*>\s*([^<]+?)\s*<(.*?)(?=<div class="row fomc-meeting|$)', ym.group(2), re.S):
            month_a, month_b, days, rest = m.groups()
            nums = re.findall(r"\d+", days)
            if not nums or month_a not in MONTHS:
                continue
            # "Jan/Feb 31-1": the last day belongs to the second month.
            month = MONTHS.get(month_b, MONTHS[month_a]) if month_b else MONTHS[month_a]
            last = int(nums[-1])
            ts = et_ns(year, month, last, 14, 0)
            date = f"{year:04d}-{month:02d}-{last:02d}"
            payload = {"meeting_days": days.strip(), "unscheduled": "unscheduled" in days.lower()}
            out.append({"class": "scheduled", "event_id": f"fomc:{date}", "source_ts": ts, "payload": payload})
            stmt = re.search(r'href="(/newsevents/pressreleases/monetary\d+a\.htm)"', rest)
            if stmt:
                out.append({"class": "scheduled", "event_id": f"fomc:{date}:released", "source_ts": ts,
                            "payload": {"status": "released", "statement_url": "https://www.federalreserve.gov" + stmt.group(1)}})
    return out


def bls_cpi(fetch) -> list[dict]:
    """Scheduled CPI releases from the BLS schedule page, and a released record
    when the public API's latest period advances."""
    out, by_ref = [], {}
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", fetch(BLS_SCHEDULE_URL), re.S):
        cells = [_strip(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)]
        if len(cells) < 3:
            continue
        ref, date, time_ = cells[0], cells[1], cells[2]
        d = re.match(r"(\w+)\.? (\d+), (\d{4})", date)
        t = re.match(r"(\d+):(\d+) (AM|PM)", time_)
        if not d or not t:
            continue
        month = next((v for k, v in MONTHS.items() if k.startswith(d.group(1)[:3])), None)
        hour = int(t.group(1)) % 12 + (12 if t.group(3) == "PM" else 0)
        ts = et_ns(int(d.group(3)), month, int(d.group(2)), hour, int(t.group(2)))
        by_ref[ref] = ts
        out.append({"class": "scheduled", "event_id": f"cpi:{int(d.group(3)):04d}-{month:02d}-{int(d.group(2)):02d}",
                    "source_ts": ts, "payload": {"reference_month": ref, "release_et": f"{date} {time_}"}})
    latest = json.loads(fetch(BLS_API_URL))["Results"]["series"][0]["data"][0]
    ref = f"{latest['periodName']} {latest['year']}"
    # source_ts is the scheduled release instant for that reference month; the
    # API carries no publication time of its own, so receipt_ts is the evidence.
    out.append({"class": "scheduled", "event_id": f"cpi:{latest['year']}-{latest['period']}:released",
                "source_ts": by_ref.get(ref),
                "payload": {"status": "released", "reference": ref,
                            "series": "CUUR0000SA0", "value": latest["value"]}})
    return out


def truth_social(fetch) -> list[dict]:
    """Public statuses of @realDonaldTrump via the instance's Mastodon-style API."""
    out = []
    for s in json.loads(fetch(TRUTH_URL)):
        out.append({"class": "unscheduled", "event_id": f"truth:{s['id']}", "source_ts": iso_ns(s["created_at"]),
                    "payload": {"url": s.get("url"), "text": _strip(s.get("content") or "")[:2000],
                                "reblog": s.get("reblog") is not None, "reply": s.get("in_reply_to_id") is not None,
                                "media": len(s.get("media_attachments") or [])}})
    return out


SOURCES = [("fomc", fomc), ("bls_cpi", bls_cpi), ("truth_social", truth_social)]
