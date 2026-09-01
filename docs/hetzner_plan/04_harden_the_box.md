# 04 — Harden the box

**Effort** 45 min · **Depends on** 03 · **Status** todo

## What
Close the one default that would silently restart the capture daemon, make the
journal survive reboots, and put the clock on a leash.

## How
**needrestart is the highest risk on this box.** On 24.04 it restarts affected
services with no prompt, and `apt` passes `-m u` on the command line which
supersedes any config setting. If it ever restarts `user@1000.service`, capture,
cycle and every timer go down together — with gapwatch among the casualties, so
it cannot record its own outage.

```
sudo sed -i 's/[[:space:]]-m[[:space:]]\+u//' /etc/apt/apt.conf.d/99needrestart
printf '$nrconf{restart} = "l";\n' | sudo tee /etc/needrestart/conf.d/90-nat2.conf
```

Then: journald `Storage=persistent` with `SystemMaxUse=4G` (one budget for the
whole directory — a chatty system journal evicts the capture forensics);
chrony with `makestep 1.0 3` so the clock slews rather than jumps mid-capture;
ufw allowing 22/80/443 with `--force enable`.

## Verify
```
grep -c ' -m u' /etc/apt/apt.conf.d/99needrestart   # 0
journalctl --disk-usage; chronyc -N tracking        # Leap status: Normal
```

## Done when
No automatic service restart can reach the user units.
