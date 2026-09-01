# 03 — cloud-init and first boot

**Effort** 30 min · **Depends on** 02 · **Status** todo

## What
Create user `onat` at `/home/onat` so the packaged units and Caddyfile need no
path edits, and check the six things only answerable on the box.

## How
cloud-init is **creation-time only** (32 KiB) — this is the one chance. Keep it
minimal: user, key, `loginctl enable-linger onat`, timezone UTC, and an sshd
drop-in named `10-hardening.conf`.

The `10-` prefix is load-bearing: sshd takes the **first** value from a
lexically ordered glob, and cloud-init writes `50-cloud-init.conf`, which can
carry `PasswordAuthentication yes`. A file named `99-` loses silently.

Linger must precede the first `systemctl --user enable`, and needs root.

## Verify
```
loginctl show-user onat -p Linger              # Linger=yes
sudo sshd -T | grep -E 'permitrootlogin|passwordauthentication'
curl -s -X POST https://api.hyperliquid.xyz/info \
     -H 'content-type: application/json' -d '{"type":"exchangeStatus"}'
timedatectl show -p NTPSynchronized --value    # yes
ls -d /var/log/journal || echo "volatile - fix in 04"
systemctl --version | head -1                  # >= 247 for --timestamp=unix
```

## Done when
All six pass and the venue answers from this IP. Start nothing yet.
