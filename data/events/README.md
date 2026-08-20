# data/events — point-in-time event log (REDUCED_SPECS §3 A5)

Written by `deploy/evlog/evlog.py once` every 5 min (`nat2-evlog.timer`). One
NDJSON file per UTC day; closed days get sha256 + line count in `_manifest.jsonl`.
Record: `{"schema":1,"class":"scheduled|unscheduled","source","event_id","source_ts","receipt_ts","payload"}`,
both timestamps epoch ns UTC. `receipt_ts` is *when this machine saw it*; that is
the datum. Nothing here is analysed — see §3b for the pre-registration gate.

## Sources (v1, 2026-08-20)
| source | class | what | confirmation |
|---|---|---|---|
| `fomc` | scheduled | meeting dates from the Fed calendar page; `source_ts` = 14:00 ET on the last day | `…:released` record when the statement link appears |
| `bls_cpi` | scheduled | CPI release dates/times from the BLS schedule page | `cpi:YYYY-Mmm:released` when the public API's latest period advances (API has no publication timestamp; `receipt_ts` is the evidence) |
| `truth_social` | unscheduled | @realDonaldTrump statuses via the instance's public Mastodon-style API, 20 newest per poll | — |

## Known limitations — stated, not worked around
- **OPEC dropped.** Every opec.org page answers with a Cloudflare browser challenge (HTTP 403, 2026-08-20). The task forbids a browser or scraping framework; the source is out until a plain HTTP path exists.
- **Paid wire headlines (Bloomberg/Reuters) are out of scope.** No licence; nothing here should be read as "first public print".
- **Receipt lag ≤ poll interval (5 min) for unscheduled events** — a Truth post is seen up to 300 s late. Scheduled releases are confirmed on the first poll after them; the *scheduled* record (written days ahead) carries the true release instant.
- Truth Social `limit=20` per poll: a burst of >20 posts inside 5 min loses the oldest. Not observed; recorded as a risk.
