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

---

## D8 — Shared orchestration is a library in `libs/`, not a new service

**Architectural. Taken while implementing the shared AI orchestration foundation.**

`libs/platform_orchestration` holds the workflow lifecycle, routing, approval
gating, retry, recovery and audit trail. Capability services install it and
persist workflow state in **their own** database.

### Why not the obvious alternatives

**Not `agent_factory`.** It is deliberately stateless — no `DATABASE_URL`, no
models — and workflow lifecycle needs durable state. Giving it a database would
change what that service is, and would put every app service's workflow state
behind a network hop in a process whose job is running LangGraph agents.

**Not a new `services/orchestrator`.** Another deployment, another Neon database,
and a round trip per state transition. The brief asks to avoid additional
infrastructure, and a state machine that cannot advance without a network call is
a state machine that fails in more ways.

**So: the convention already in the repo.** `platform_common` is a shared package
in `libs/`, vendored into each service and tracked in git because every service
deploys from its own directory. Orchestration follows that exactly.

### What this means in the running system

- The four `workflow_*` tables live in the mail agent's existing database and are
  created by its existing `create_all` at startup. No migration to run.
- Cross-capability routing crosses the service boundary the same way everything
  else does: an internal-key HTTP call. `RoutingTarget.capability` is how a rule
  says so.
- The Voice Agent adds its own adapter module and changes nothing in the core.
  `libs/platform_orchestration/README.md` is the worked example.

### Consequences accepted

**The vendored copy can drift.** That is not hypothetical —
`services/auth/platform_common/db.py` already differs from the other four copies.
So `scripts/sync_shared_libs.py` makes the copy reproducible, and
`mail_agent/tests/test_workflow_route_scope.py` fails the build if it is stale.
The script deliberately does **not** touch `platform_common`: reconciling those
four copies is a change to four running services and needs its own testing.

---

## D9 — Outbound replies are gated and audited

**Taken while implementing the orchestration foundation.**

`EmailAudit` was being written in exactly one place — `ingest.py`, action
`"ingested"` — despite the model documenting `approved`, `rejected`, `sent` and
`send_failed`. **Nothing recorded who authorised an outbound reply**, because
`POST /emails/{id}/reply` sends immediately with no approval step and no audit row.

The new `mail.reply_approval` workflow is the gate and the record:

- `prepare` validates the draft, the route and the alias, then the run stops.
- `send` carries `requires_approval=True`, so the engine will not call its handler
  until an `ApprovalRecord` naming a **human** exists. A service principal is
  refused the approval path outright.
- The approval and the send are both written to `workflow_events` *and* mirrored
  into `email_audit`, so "who approved this reply?" is answerable from the email's
  own history.

### Superseded in part by D10

This decision originally left `POST /emails/{id}/reply` sending immediately, so
the gate was additional rather than mandatory. D10 closed that. The paragraph that
described the remaining ungated path no longer applies, and neither does the
blanket refusal of restricted mail — see D10 for both.

---

## D10 — The approval gate is mandatory: every send path goes through a workflow

**Taken when closing the acceptance gaps left by D9.**

There were three ways a reply could leave this service. Two bypassed the gate, so
the gate was advisory:

| Path | Before | Now |
|---|---|---|
| `POST /emails/{id}/reply` (the review UI's Send button) | sent immediately; no approval, no audit row | **queues** a `mail.reply_approval` run and stops at the gate; returns 202 and the run |
| `pipeline.maybe_send` (autonomous auto-reply) | sent directly when the org setting was on | runs `mail.auto_reply`, released by a recorded **policy approval** |
| `POST /workflows/reply-approval` | gated | unchanged; shares one implementation with the route above |

`mail_client.send_reply` and `graph_client.send_reply` are still the only functions
that touch a mail host, and the `mail.send_reply` step handler is now their only
caller in application code. `test_no_ungated_send.py` asserts that statically by
reading `app/`, so a future route that sends directly fails a test rather than an
audit.

### Sending is now a different permission from drafting

`MailboxScope` already had `can_send` and `can_approve` as separate axes; nothing
used the distinction for outbound mail. Queueing a reply takes `can_send`;
releasing it takes `can_approve`. A user holding only `can_send` can ask for a
reply to go out and cannot make it go out — which is the substance of "mandatory",
as opposed to a gate everyone can also open.

A user holding both can do it in two clicks. That is deliberate: separation of
duties becomes a *configuration* choice (don't grant both), and the approval is a
recorded, attributable decision either way.

### Autonomous replies are approved by a standing policy, not by nobody

The org's auto-reply setting is user-facing and documented in onboarding ("on
means high-confidence replies send automatically"). Requiring per-email human
approval would delete that feature; leaving it ungated would leave exactly the
hole this ticket is about.

So `platform_orchestration` gained two things:

- `StepSpec.policy_approval` (default **False**) — "a standing policy may stand in
  for the reviewer on this step".
- `Orchestrator.submit_policy_approval()` — the mirror of `submit_approval`: it
  takes a **service** principal and refuses a human, where `submit_approval` takes
  a human and refuses a service principal. A person's decision must be recorded as
  theirs and never laundered through a policy name.

The approval row reads `decided_by = "policy:mail.auto_reply_enabled"` and the
audit action is `approved_by_policy`, so a reader cannot mistake it for someone
pressing Approve. `mail.reply_approval` does **not** opt in, so the cron can never
release a reply a person was asked about.

This loosened nothing that existed: a step that has not opted in is unreachable
this way, and `policy_approval` on a step with no `requires_approval` fails to
register.

### What the gate must not newly forbid

D9 refused any agent-drafted reply to safeguarding-restricted mail, reasoning that
a cleared human could still reply "by hand through the existing route". Once that
route is gated, that escape no longer exists — so the blanket refusal would have
left **nobody** able to answer a safeguarding item.

The refusals now follow `send_identity.py`, which had already decided these
questions. Two of them are about the agent acting alone:

| Reason | Human | Autonomous |
|---|---|---|
| `no_mailbox` | refused | refused |
| `alias_reply_forbidden` (safeguarding@) | refused | refused |
| `restricted_no_autonomous_reply` | **allowed, with clearance** | refused |
| `unresolved_receiving_address` | **allowed** | refused |

Clearance is not re-checked in the handler: `MailboxAuthorizationPort` already
refuses a restricted email to a user without it, both to queue and to approve.
Allowing the human case for an unresolved address matters more than it looks —
pre-PW rows carry `receiving_address_status = "legacy"`, so refusing it would have
made every historical email permanently unanswerable.

The refusal strings are `send_identity`'s own constants rather than prose, because
they reach both the audit trail and the UI; the frontend maps them to readable text.

### A third sweep, because a policy gate is not a human gate

`recover_stale` looks at `running` runs with a lapsed lease and `resume_due` at
`retry_scheduled`. Neither reaches `awaiting_approval`, which is right: a run
waiting for a person must wait as long as it takes.

A run waiting for a *policy* is not in that position — the authority already
exists, so the wait is not meaningful and nothing else will end it. `auto_send_reply`
starts a run and then approves it in a second call, so a process that died between
the two left the run parked forever: no sweep looked at its state and no person was
expected to act on it. The email was unaffected (the pipeline had already fallen
back to a draft), but the run was stranded and would surface in the human review
queue.

`Orchestrator.resume_policy_gated(policy=..., definition=...)` closes that, and the
mail scheduler calls it scoped to `mail.auto_reply`. It only touches steps that
opted in with `policy_approval=True`, so a `mail.reply_approval` run waiting for a
reviewer is untouched by all three sweeps — asserted in both suites.

### `auto_sent` follows the approval record, not the payload

The column means "went out without a person pressing send", so only the authority
on record can answer it. It was first implemented by reading an `auto` flag off the
run's payload, which got the reachable case backwards: a reviewer releasing a
stranded autonomous run was reported as the agent having sent it. The send handler
now reads the `ApprovalRecord` and treats `decided_by` starting with `policy:` as
the autonomous case.

### Known limitation, not introduced here

**`send_identity.derive()` is dead code.** It decides the From address for a reply
— alias vs mailbox, with the rights of the approver — and *nothing in the service
calls it*; the only callers are its own tests. `graph_client.send_reply` already
accepts a `from_address` and sets `message["from"]` when given one, and no caller
passes it, so replies go out as the mailbox's own address. The gap is therefore
precisely those two unmade connections.

Wiring them changes which identity outbound mail carries and needs Exchange SendAs
configured in the tenant, which is not verifiable from here, so this ticket uses
`send_identity`'s refusal reasons and leaves the From-address derivation alone.

---

## D11 — Lint and type configuration lives on the new library, not at the backend root

**Taken when the ticket asked for the "configured Ruff and mypy checks".**

There was no such configuration: no `ruff.toml`, no `mypy.ini`, no `[tool.*]`
section anywhere, and no CI. The only trace was a `.ruff_cache` and `.mypy_cache`
in `services/auth`, left by someone running them ad hoc once. Neither tool was
installed in any venv.

Both are now configured in `libs/platform_orchestration/pyproject.toml`, and the
library is clean under both. The configuration is **not** at the backend root
deliberately: it would then govern five services written before it existed, report
hundreds of pre-existing findings on its first run, and be switched off. The mail
agent's new modules are checked with the same rules by pointing at that config —
see the commands in that file's comment.

Four rules are off, each for a reason rather than to make the run quiet: `B008`
(FastAPI's `Depends()` idiom *is* a call in a default argument), `UP031` (this repo
formats with `%` throughout, which is also the correct form for logging calls),
`UP042` (suggests `enum.StrEnum`, 3.11+; these services run on 3.10), and `E501` in
tests.

The checks found four real defects in code written for the previous ticket: a
nullable `client_secret_enc` reaching Fernet, an assertion in `test_states.py` that
was never asserted (`assert_transition(...) is None` with no `assert`), a
`pytest.raises(Exception)` that would have passed on any error, and a dead local.
Pre-existing findings in the five older services were left alone.

---

## D12 — The Auth signing key is gitignored, not relocated

**Taken while closing the same acceptance gaps.**

`services/auth/private_key_pem.txt` is a real, unencrypted 2048-bit RSA private key
in the single-line escaped form `services/auth/app/keys.py` reads out of
`PRIVATE_KEY_PEM`. It was **untracked and unignored** — `*.pem` does not match a
`.txt`, and `keys/` does not match a file beside it — so one `git add -A` would have
committed the key that signs every access token.

It is the same key as `services/auth/keys/private.pem` (compared by public-key
fingerprint, without either file being read out), which is the dev-generated key
and is already ignored twice over. So this is the local dev signing key
re-serialised for an environment variable, not necessarily a deployed one.

`.gitignore` now covers `*private_key*.txt`, `*private_key*.pem`, `*_key_pem*` and
`*.key`. The file was not moved, renamed or modified: it is presumably there because
someone needs to paste it into a deployment, and deleting a key to silence a warning
is how an environment stops being able to issue tokens. No tracked file matches the
new patterns.

### What the repository can and cannot establish

Checked rather than assumed:

- **Never committed.** `git log --all -- services/auth/private_key_pem.txt` returns
  zero commits, so no history rewrite is needed — only the ignore rule, which is in
  place.
- **Not referenced by local config.** `services/auth/.env` does not set
  `PRIVATE_KEY_PEM`, so this service falls through to `keys/private.pem` (the same
  key) in development, exactly as `app/keys.py` documents.
- **No deployment configuration exists in the repository** — no Procfile, no
  railway/render manifest, no compose file, no CI workflow. So nothing here records
  whether `PRIVATE_KEY_PEM` was ever set in a deployed environment, and it cannot
  be established from the repository.

**Still for a human to decide**, and now answerable in one step. The key's RFC 7638
JWK thumbprint, computed from the public half only, is:

    bdb51afdf0621251a49dfcbe516b5bad79ba8b9eb8267cb5a7b32f7c5e2489b1

The Auth service publishes its public key at `/.well-known/jwks.json`. Computing
the same thumbprint from that endpoint's JWK and comparing says definitively
whether the deployed signing key is this key. If it matches, this key is a
production signing key that exists unencrypted in two files on a workstation and
should be rotated through the normal change process. The comparison uses only
public material, so it discloses nothing.
