# 16 — Caddy and DNS

**Effort** 1 h · **Depends on** 03 · **Status** todo

## What
Serve the status page and the daily digest over HTTPS behind basic auth.

## How
**Buy the domain and set the A record on day 1**, so propagation completes before
Caddy first runs — Let's Encrypt rate limits otherwise cost hours. The repo's
`deploy/Caddyfile` ships `status.localhost`, which ACME can never issue for, and
a placeholder bcrypt hash.

Install from the official apt repo, then `caddy hash-password`,
`caddy validate`, `systemctl reload caddy`.

**The permission trap:** the package runs Caddy as the `caddy` system user, and
`/home/onat` is mode 750 — so `root * /home/onat/www/status` yields 403s that
look like a Caddy misconfiguration. Fix it explicitly: `chmod o+x /home/onat`,
or an ACL, or write the pages outside the home.

Only **two** output directories exist in the codebase — `~/www/status` and
`~/www/reports` — so the Caddyfile needs two roots.

## Verify
```
curl -u nat2:<pw> https://<host>/status/   # 200, valid cert
curl -I https://<host>/status/             # 401 without auth
```

## Done when
Both pages load over HTTPS and refuse without credentials.
