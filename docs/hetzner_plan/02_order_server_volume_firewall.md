# 02 — Order server, volume, firewall

**Effort** 30 min · **Depends on** 01 · **Status** todo

## What
One x86 server in the EU with a primary IPv4, a 200 GB volume, and an
inbound-only firewall.

## How
- **CPX22** (2 vCPU / 4 GB / 80 GB, €19.49 net). The whole Cost-Optimized line
  — CX23/CX33 and every CAX — is currently unorderable. Not ARM: architecture is
  sticky, and a server only rescales within its own.
- **Falkenstein, Nuremberg or Helsinki.** Not Ashburn or Hillsboro: the venue's
  terms restrict US persons, which would place the host in a restricted
  territory.
- **Primary IPv4** (€0.50/mo) — mandatory, the venue has no AAAA.
- **200 GB volume** (€0.0572/GB/mo). Growth is 0.32–0.44 GB/day; volumes grow
  only, and are excluded from every Hetzner backup product.
- **Attach the SSH key at creation** — Console cannot add one afterwards.
- Firewall: inbound TCP 22/80/443 and ICMP. Write **no** outbound rule; with
  none, all egress is permitted, which is what the venue client needs.

## Verify
Server boots; `hcloud firewall describe` shows inbound rules only.

## Done when
The box is reachable by key and the volume is attached.
