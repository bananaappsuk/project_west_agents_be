# Project West Email Agent — decision record

Decisions taken during implementation, and what each one means in the running system.

Recorded so that a behaviour nobody remembers choosing is not later mistaken for a
bug — and so that where the system fails closed, it is on record that the closure
was intended.

---

## D1 — Safeguarding leads: none named

**Decided 2026-10-07. Status: accepted as-is.**

`PW-Safeguarding-Lead` (`a5d90455-e675-458b-ae6d-3c915e22155f`) exists and is
**empty**, and will stay empty until Project West names leads.

### What this means in the running system

**Nobody can see safeguarding material through the agent.** Not any of the nine
named users, not an application Superadmin. Restricted email is absent from every
list, count, search and alert for every account.

An email is restricted when it arrives at `safeguarding@`, or when the classifier
raises `safeguarding_indicator` on it — the restricted route wins whenever either
fires.

### What still happens normally

Restricted mail is **captured, stored, routed and audited in full**. It is only
invisible. Nothing is dropped and nothing is skipped, so the day a lead is named the
entire backlog becomes visible immediately with no re-ingestion and no
re-classification — clearance is evaluated at read time, not baked into the row.

Covered by `tests/test_no_cleared_users.py`, which treats this as a supported
operating state rather than a temporary gap.

### Consequences accepted

- **Capability brief acceptance test 9 cannot pass.** It requires demonstrating that
  a cleared user sees a restricted item; with no cleared user, only the "others
  cannot see it" half is demonstrable.
- **No reply can ever be sent from `safeguarding@`.** The alias is configured
  `reply_allowed = false`, so neither the agent nor any human can send from it. This
  is independent of clearance and would still hold if leads were named.

### ⚠️ What this decision does NOT do

It does not make safeguarding material confidential within Project West.

`safeguarding@` is an alias into `coordinator@`, and **all nine named users hold
Full Access and Send As on `coordinator@` in Exchange**. They can open Outlook and
read every safeguarding disclosure directly, regardless of anything in this
application. The application restricts the application; it cannot restrict Exchange.

Genuine restriction requires a **separate safeguarding mailbox** rather than an
alias — an Exchange change on Project West's side, not a code change. This is
recorded because it is the kind of gap an organisation can believe it has closed.

---

## D2 — Approval follows send rights

**Decided 2026-10-07.** `can_approve = can_send` on every rule.

Whoever may reply from a shared mailbox is also who approves replies from it, which
mirrors the Send As permissions already in Exchange.

`can_approve` remains its own column and `MailboxScope` keeps a separate
`approvable` set, so restricting approval to a senior subset later is a row update
rather than a schema change. The human gate is unaffected: the agent still never
sends a substantive reply by itself.

Applied by `migrate_pw_approve_equals_send.sql`.

---

## D3 — Read and send granted together

**Answered by evidence, not preference.** The live Exchange permission matrix grants
Full Access and Send As together in **every** cell, so the group model is one
security group per mailbox rather than three. Splitting them later needs no code
change.

---

## D4 — Ingestion: newest 10, forward only

**Decided 2026-10-07.** Each mailbox is seeded with its most recent 10 messages and
thereafter only collects new mail. There is no backfill and no "sync last 30 days".

This removed the bidirectional sync window (`sync_floor` / `backfill_to`) entirely.
`monitor_from` is retained only as a hard floor nothing can reach past.

A first sync must have **either** a date floor or a count cap; a mailbox with
neither is refused, because an unbounded first sync of `info@` is 85,120 LLM calls.

---

## D5 — `admin@` is an alias, not a mailbox

**Settled by the live tenant, against the written SSOT.**

`MAILBOX_AUTHORIZATION_PLAN.md` §16 decision 10 chose "M365 Mailbox Access Map v1"
(which said `admin@` was a shared mailbox) over the September M365 report (which
said alias). Exchange shows `admin@` is a secondary SMTP proxy on `management@`,
sharing its `ExternalDirectoryObjectId`.

**The September report was right and Access Map v1 is wrong.** Six physical
mailboxes, four aliases. Access Map v1 is also stale on user count: nine named
users, not six.

Where the live tenant and the written SSOT conflict, the tenant wins.

---

## D6 — Cross-mailbox precedence: OPEN

Where one email names two **different** mailboxes — `coordinator@` + `finance@` — no
rule is applied. Those rows are marked `ambiguous`, routed to the exception queue,
and excluded from autonomous action. A human triages them.

Measured at **11 of 60 (18%)** on live mail.

Already resolved automatically: an alias and **its own parent mailbox**
(`safeguarding@` + `coordinator@`), which is one destination described twice rather
than a conflict. Safe because every candidate resolves to the same mailbox, so the
authorization outcome is identical either way.

Still open because `coordinator@` vs `finance@` is a business question about which
team owns the item, and guessing would misroute mail confidently instead of queuing
it visibly.

---

## D7 — Authorization lives in Exchange RBAC, never in Entra

**Architectural, and load-bearing.**

The service principal `Project West Mail Sender`
(`d1687ded-fc1c-4003-a047-f0966bef6087`) holds **zero Microsoft Graph application
permissions**. Its access comes entirely from Exchange RBAC for Applications, scoped
to exactly the six mailboxes.

**Never grant it `Mail.Read` or `Mail.Send` in Entra.** Microsoft's model unions the
two authorities, so a tenant-wide Entra grant produces *"no effective resource
scoping"* and would expose all nine personal mailboxes. The fix for "the agent
cannot read" is never to grant it permission to read in Entra.

Verified empirically: the app-only token's `roles` claim is empty, `info@` returns
HTTP 200 and `arti@` returns HTTP 403.
