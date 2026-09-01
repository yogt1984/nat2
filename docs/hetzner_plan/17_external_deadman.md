# 17 — External dead-man

**Effort** 20 min · **Depends on** 07 · **Status** todo

## What
The cheapest thing that makes 24/7 real, and it was in no spec. gapwatch is a
`--user` timer **on the box it watches**: if the user manager dies, linger is
lost, or the box is powered off, everything stops and the only signal is
*absence*. ntfy is push-only — it cannot express "I heard nothing", and
`notify()` already swallows `OSError` and returns `False`.

## How
healthchecks.io's free tier (20 checks) covers gapwatch, capture, cycle, gates,
report, evlog, statuspage and the nightly restic job. Arm the existing 5-minute
timer with one line:

```
# ~/.config/systemd/user/nat2-gapwatch.service.d/10-deadman.conf
[Service]
ExecStartPost=/usr/bin/curl -fsS -m 10 --retry 3 https://hc-ping.com/<uuid>
```

`ExecStartPost` runs only after a **successful** ExecStart, so a gapwatch crash,
a dead VM and a dead uplink all stop the ping identically. That is the property
you want.

Do not post `$EXIT_STATUS`: systemd gives the numeric code only when the service
exited; on a signal it is the signal name.

## Verify
Stop the timer for 20 minutes — the check must go red.

## Done when
A deliberate silence raises an alarm you receive.
