# 07 — Secrets and ntfy rotation

**Effort** 30 min · **Depends on** 06 · **Blocks** 17, 18 · **Status** todo

## What
Create every secret the units read, and burn the topic that is already public.

## How
`nat2-ops-0db264232a4c36cd56e4` is a literal at `deploy/systemd_units.py:43` in
a **public** repo, in history since `7f246bd`. On ntfy.sh a known topic grants
read *and* publish. Rotation re-hides it; it does not authenticate it, and making
the repo private does not un-publish what was already public.

```
install -d -m 700 ~/.config/nat2
umask 077
cat > ~/.config/nat2/ops.env <<EOT
NAT2_NTFY_TOPIC=nat2-ops-$(openssl rand -hex 10)
RESTIC_PASSWORD_FILE=/home/onat/.config/nat2/restic.pass
EOT
openssl rand -base64 48 > ~/.config/nat2/restic.pass
chmod 600 ~/.config/nat2/*
```

Copy `restic.pass` into the password manager — a lost repo password makes the
backup unreadable, which is worse than no backup because it looks like one.
Pre-seed the Storage Box host key with `ssh-keyscan -p 23`, or an unattended
`Type=oneshot` job hangs forever on a first-connection prompt.

## Verify
```
stat -c '%a %U %n' ~/.config/nat2/*      # 600 onat
curl -d test https://ntfy.sh/<new-topic> # arrives on the phone
```

## Done when
The phone receives a test push on the new topic and both files are 600.
