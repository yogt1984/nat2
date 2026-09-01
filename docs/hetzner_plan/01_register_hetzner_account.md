# 01 — Register the Hetzner account

**Effort** 30 min, then an unknown wait · **Status** todo

## What
Create the account and project so the verification clock starts. Verification is
order-triggered, manual, and has no published SLA.

## How
- Register at `accounts.hetzner.com` — **no VPN**, and have an ID photo ready.
  Hetzner advises against free email addresses; a gmail account is a documented
  risk factor, so mitigate with the ID rather than by hoping.
- Set the account main address correctly: root credentials and abuse tickets go
  there, and an unnoticed abuse ticket ends in an IP lock.
- Currency is fixed at creation and cannot be changed. Pick EUR.
- Set up SEPA direct debit or card auto-charge now — new customers are locked
  early for late payment, and a locked server ends the clean-day count.
- Create project `nat2`; upload the SSH key under Security.
- Escalate a stuck order to `cda-review@hetzner.com`.

## Verify
Console shows the project, the key, and the Limits tab (expect 5 servers).

## Done when
The account can create a server, and the key is listed in the project.
