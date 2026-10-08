# Target Architecture & Migration Plan — Final Pre-Implementation Gate

**Project West Email Agent — converting the POC from the personal-Gmail IMAP fixture to the Project West architecture**

| | |
| --- | --- |
| Date | 2026-10-06 |
| Revision | **Final pre-implementation gate review incorporated** |
| Status | **Plan only. Nothing implemented.** No code, database, migrations, Exchange, Entra or configuration changed. No services started, no mailboxes seeded, no mail fetched or sent. |
| Inputs | [Phase -1 gate summary](PHASE_MINUS_1_FINAL_GATE_SUMMARY.md) · [Phase -1C reconciliation](PHASE_MINUS_1C_ACTIVE_PATH_RECONCILIATION.md) · [Mailbox authorization plan](MAILBOX_AUTHORIZATION_PLAN.md) · live Exchange/Entra/Graph verification |

### Classification used on every change

| | Meaning |
| --- | --- |
| **[A]** | **SSOT requirement** — from the capability brief / Access Map / authorization plan |
| **[B]** | **Technical implementation requirement** — follows from [A] plus the verified tenant |
| **[C]** | **Project West decision required** — nobody may assume an answer |
| **[D]** | **Existing POC behaviour that can be retained** — already correct |

**No new business requirements are introduced.** Where the SSOT is silent — SharePoint,
CRM system-of-record precedence — that silence is reported as **[C]**, not filled in.

---

## 1. Current vs target

### 1.1 Current POC — verified

```
  harshithareddy2711l02@gmail.com        (personal Gmail, tenant d868ea1b-…)
        │  IMAP4_SSL + Basic Auth · SELECT "INBOX" · SEARCH "ALL" when unwatermarked
        ▼
  POC — single Mailbox row · parses From/Date/Subject/body/attachments
        To/Cc/Delivered-To retrieved in RFC822 and DISCARDED UNREAD
        LLM taxonomy → draft · flat emails.read / emails.write
        │  raw SMTP (SENDGRID_API_KEY unset) · From: = the IMAP login address
        ▼
  recipient
```

### 1.2 Target PW architecture

```
  INGESTION PLANE (unattended)              AUTHORIZATION PLANE (per request)

  6 physical shared mailboxes               User signs in with Entra ID
  + 4 business aliases                               │
        │  Graph app-only                            ▼
        │  NO tenant-wide Entra grant        Entra group membership
        │  Exchange RBAC scope = those 6              │
        ▼                                            ▼
  Pipeline stamps mailbox_id,               mailbox_access_rule
  receiving_address, original_recipient,             │
  and the resolution status                          ▼
        │                                   user_mailbox_access
        └──────────────┬────────────────────────────┘
                       ▼
        emails — every row carries its mailbox;
        every read filtered by the authorised set;
        restricted rows gated on a separate clearance axis
                       ▼
          human approval (can_approve)
                       ▼
     authorised PW sender identity, derived from
     mailbox + alias + the approving user's rights
```

**[A]** The planes meet at exactly one place: the stored email, carrying its mailbox as an
immutable property. The left plane never sees a user; the right never sees a credential.

---

## 2. Physical mailbox vs business receiving address

**This distinction is modelled explicitly, and `admin@` does not become a seventh physical
mailbox.**

### 2.1 The two levels

| Level | Meaning | Column |
| --- | --- | --- |
| **Physical shared mailbox** | Where the mail physically *is*. The thing Graph polls, and the authorization key | `emails.mailbox_id` → `mailboxes` |
| **Business receiving address** | What the sender actually *typed*. Carries the business meaning | `emails.receiving_address`, `emails.original_recipient` → `mailbox_aliases` |

**[A]** *"The mailbox is where the mail is; the alias is what the sender meant."*

### 2.2 The six physical mailboxes — **[VERIFIED]**

| Mailbox | Graph object id |
| --- | --- |
| `info@projectwest.co.uk` | `9609fee8-549a-47b2-a2fc-e2e1c6d77e6d` |
| `referrals@projectwest.co.uk` | `cbe6ae6b-9f76-4b02-84ea-0088f561e62d` |
| `coordinator@projectwest.co.uk` | `29bad478-47bd-4c04-9143-c511a6b1dfcf` |
| `finance@projectwest.co.uk` | `9c4588ef-287d-4f6f-8d11-5f62d2ae29d1` |
| `management@projectwest.co.uk` | `188f91f4-b916-4fed-8b88-5d30a81f5cde` |
| `reports@projectwest.co.uk` | `70e3685c-3a77-40d5-9d85-e9bb5e1ef4a3` |

**[B]** `mailboxes` is seeded with **exactly these six rows.** The object ids seed
`graph_user_id` directly, removing any runtime resolution step.

### 2.3 The four business aliases — **[VERIFIED]**

| Alias | Physical mailbox | Queue | `reply_allowed` | `restricted` |
| --- | --- | --- | --- | --- |
| `payments@` | `finance@` | `payment_or_finance` | yes | no |
| `complaints@` | `coordinator@` | `complaint` | yes | no |
| `safeguarding@` | `coordinator@` | `safeguarding_restricted` | **no** | **yes** |
| **`admin@`** | **`management@`** | **[C]** decision 11 | **[C]** decision 11 | no |

> **[B] `admin@` is a `mailbox_aliases` row, never a `mailboxes` row.** The SSOT's
> seven-mailbox model was wrong; §16 decision 10 chose Access Map v1 over the September
> M365 report, and **[VERIFIED]** the September report was right. Anything in the
> authorization plan keyed to "seven mailboxes" or "three aliases" is corrected to six and
> four.

**[B]** The four `.onmicrosoft.com` routing addresses (`referrals6151@`,
`coordinator2744@`, `finance@`, `management@`) are seeded as alias rows with no queue and
`reply_allowed = false`, so mail arriving at them resolves rather than falling to the
exception path.

**[C]** Whether `admin@`'s three-person visibility is intended (decision 12) — **[VERIFIED]**
only arti, pauline and shawana hold `management@`.

---

## 3. Receiving-address capture — **P1a, first, before any seeding**

**[A]** *"The receiving address is the only durable evidence, and it exists only in the
headers at the moment of collection. If it is not captured then, it is gone."*

**[VERIFIED]** The POC is destroying this on every message today. **[B]** P1a precedes
mailbox seeding and broad ingestion, and does not depend on any authorization work.

### 3.1 Evidence preservation

**[B]** Capture and retain the raw recipient evidence that the decision was made from, not
only its conclusion:

| Source | Graph field |
| --- | --- |
| Primary recipients | `toRecipients` |
| Copied recipients | `ccRecipients` |
| Delivery evidence | `internetMessageHeaders` — `Delivered-To`, `X-MS-Exchange-Organization-OriginalRecipient`, `X-MS-Exchange-*` where present |

**[B]** Store as `recipient_evidence` (JSON) alongside the resolved values. This extends
the SSOT's five columns; it is an implementation requirement, not a business one, and
exists so a receiving-address decision can be audited or re-derived later without the
message. **Without it, a wrong precedence rule is undetectable and uncorrectable after the
fact.**

### 3.2 The deterministic decision algorithm

**[B]** Let **K** = the known-address map: the 6 physical addresses ∪ all alias addresses
(including the routing addresses). Let **E** = `toRecipients` ∪ `ccRecipients` ∪ delivery
headers.

```
C := E ∩ K                       (candidate business addresses, de-duplicated, case-folded)

|C| == 1                     → status = resolved_single
                               receiving_address  := the member of C
                               original_recipient := that address if it is an alias, else NULL

|C| >  1, precedence applies → status = resolved_precedence
                               resolved by the rule PW supplies (decision 8)
                               all candidates retained in recipient_evidence

|C| >  1, no rule            → status = ambiguous      ← EXCEPTION
|C| == 0                     → status = unresolved     ← EXCEPTION
```

**[A]** `mailbox_id` is **always** known — it is the mailbox being polled — and is never
subject to this algorithm. Only the *business* address can be ambiguous.

### 3.3 Exception behaviour — no silent defaulting

**[B]** A row whose status is `ambiguous` or `unresolved`:

1. **Is explicitly marked.** `receiving_address_status` is a non-null column and is the
   load-bearing signal. The SSOT's "fall back to the polled mailbox" is permitted **only**
   as a storage value to satisfy the `NOT NULL` constraint, and **only** when accompanied
   by a non-`resolved_*` status. It is never a business or authorization signal, and
   nothing may read `receiving_address` without reading the status.
2. **Is excluded from autonomous action.** No auto-send, no alias-derived reply identity.
3. **Is surfaced for human resolution** through the existing flag mechanism.
4. **Does not block ingestion.** The message is stored; only autonomous handling is withheld.

> **[B] The tension to resolve before build:** the SSOT makes `receiving_address` `NOT
> NULL` after migration step 4, which forces *some* value onto an unresolved row. The
> status column is what prevents that value being mistaken for a determination. If PW
> would rather the column be nullable, that is a cleaner expression of the same intent and
> a small deviation from the plan — **[C]** worth confirming.

**[A]** Out of scope: an exception-queue *workspace*. §18 lists it among the deferred
items. P1a records the status and withholds autonomous action; it does not build a UI.

---

## 4. Target architecture by concern

### 4.1 Graph ingestion

- **[A]** App-only. Delegated was rejected — the agent polls on a schedule with no user.
- **[B]** Replace `mail_client.fetch_latest` with `graph_client.fetch_latest`.
- **[B]** `$select` extends to `id, subject, from, toRecipients, ccRecipients,
  receivedDateTime, bodyPreview, body, hasAttachments, conversationId, internetMessageId`,
  plus `internetMessageHeaders` where §3.1 requires it. **[A]** No new Graph permission is
  needed — these are fields on a message the app already reads.
- **[C]** Inbox-only or all folders (decision 17) — gate **T10**.

### 4.2 Exchange RBAC for Applications

**[VERIFIED]** The tenant is already on Microsoft's RBAC-for-Applications model; both PW
service principals hold **zero** Graph application-role assignments. That is correct.

- **[B]** A management scope admitting **exactly the six mailboxes**, written as explicit
  `PrimarySmtpAddress -eq` clauses. **[VERIFIED]** The existing
  `ProjectWest-Referrals-Mail-Scope` already models this for one address — widen the same
  pattern. A class predicate would silently absorb future shared mailboxes.
- **[B]** Assign `Application Mail.Read` **and** `Application Mail.Send`, bound to it.
- **[A]** **Grant nothing in Entra.** The two authorities **union**; a tenant-wide grant
  yields *"no effective resource scoping"* and would expose all nine personal mailboxes.
- **[B]** "Zero Graph app-role assignments on both SPs" becomes a standing invariant with a
  periodic assertion, not a one-off check.
- **[B]** Allow for the documented 30-min-to-2-hour permission cache; the test cmdlet
  bypasses it, so config can test correct while live calls still fail.

### 4.3 Per-mailbox scoping and per-user access control

- **[A]** `MailboxScope` on **every** email route; the filter is unconditional.
- **[A]** A mailbox id may only **narrow, never widen**. Out-of-scope reads return **404,
  not 403**.
- **[A]** Rules bind to **Entra groups, never individuals**. The resolved set lives in the
  **database, not the JWT**.
- **[A]** Group-claim overage **fails closed loudly** — fall back to `GET /me/memberOf`,
  never a silent empty set.
- **[B]** Route-coverage test asserting every email route depends on `mailbox_scope`.
- **[C]** Entra groups and object ids for **nine** users (decision 1); read/send as one
  grant or two (decision 6); who may approve (decision 7).

### 4.4 Send / reply identity

- **[A]** Derived, never chosen. The frontend sends no sender field.
- **[A]** `scope.sendable` is **the only control** — app-only access bypasses Exchange's
  Send As check entirely.
- **[B]** Add `message.from` to the Graph `sendMail` body. **[VERIFIED]** It is absent
  today; `Mailbox.username` is the only thing determining sender.
- **[C]** Alias sending under app-only `Mail.Send` (decision 5) — gate **T4**.

### 4.5 Safeguarding — classification is not sufficient

**[VERIFIED]** `safeguarding@` is an alias into `coordinator@`, and all nine named users
hold Full Access **and** Send As on `coordinator@`.

- **[D]** **Retain the taxonomy** — `safeguarding_concern` and the `safeguarding_restricted`
  route already exist in `agent_factory/app/taxonomy.py`.
- **[A] Classification alone is insufficient and must not be mistaken for compliance.**
  The POC today computes the restricted route and then does nothing with it: no clearance
  model, no visibility restriction, no send restriction. The target must **enforce**:
  - **[A]** A restricted email is **absent** from list, counts, search **and alerts**
    without clearance — not redacted, not placeheld. A visible marker would itself leak
    that a safeguarding matter exists.
  - **[A]** Clearance is a **separate axis**, driven by `PW-Safeguarding-Lead`. A
    Superadmin does **not** receive it implicitly.
  - **[A]** The restricted route wins whenever **either** the alias or the classifier fires.
  - **[A]** `safeguarding@` is `reply_allowed = false` — *"no user and no agent can send
    from it."*
  - **[A] No autonomous substantive reply may be sent for any restricted email**,
    irrespective of which address it arrived at. **[B]** `auto_sendable` must incorporate
    restricted status, not only the alias's `reply_allowed`.

> **[B] Precision on provenance.** The first four bullets are stated outright in the SSOT.
> The fifth — suppressing autonomous replies for a restricted email arriving at a
> *non-restricted* address, e.g. `info@` mail flagged `safeguarding_indicator` — is the
> review tightening an ambiguity the SSOT leaves open. It is consistent with the
> fail-closed direction throughout and is adopted here, but it is a clarification rather
> than a quotation, and PW should be told it was read that way.

- **[C]** Name the safeguarding leads (decision 3). Until then the group is empty and
  nobody sees restricted items — correct, but it must be deliberate.
- **[C]** Whether to narrow Coordinator access (decision 9). **The application restricts
  the application; it cannot restrict Exchange.** Nine people plus a service account can
  read safeguarding mail in Outlook regardless of anything built here.

### 4.6 Referral approval

- **[D]** **Retain** `reply_status` (`none|draft|sent|rejected`), `draft_reply`, and the
  auto-send gate (`auto_reply_enabled AND auto_sendable AND needs_reply`) with its
  fall-back-to-draft on any failure.
- **[A]** Approval becomes a per-mailbox right (`can_approve`), distinct from sending. A
  user without `can_send` cannot send; the draft survives, visibly awaiting someone who can.
- **[C]** Who holds `can_approve` (decision 7).

### 4.7 Attachments, identity, dedup

- **[D]** **Retain attachment handling entirely** — decoded in memory, forwarded to CRM,
  **bytes never persisted**. Already SSOT-aligned.
- **[B]** Capture `conversationId` and `internetMessageId`.
- **[A] ⚠️ Do NOT make `internet_message_id` unique** — the same message legitimately lands
  in two monitored mailboxes with an identical value; a unique constraint would silently
  drop the second copy.
- **[D]** **Retain the dedup design** — `known_uids` filtered before the count slice, so a
  stale cursor causes redundant work, never duplicate inserts.

### 4.8 First-sync / cutover

**[VERIFIED]** 205,316 items; `info@` alone is 161,833 (78.8%); `referrals@` +
`coordinator@` hold **316 between them**. No `monitor_from` mechanism exists.

- **[C]** The cutover/cutoff boundary per mailbox (decision 4) — the largest cost decision.
- **[B]** `monitor_from` per mailbox; an **enforced first-sync ceiling** that halts and
  alerts; a **per-mailbox fetch budget** — **[VERIFIED]** `fetch_per_run` is one global
  value and a 1,124:1 size ratio would let `info@` starve `referrals@`.

### 4.9 CRM system of record

- **[D]** **Retain `crm_sync.py` unchanged** — `/intake/referral`, `/intake/communication`,
  `/intake/activity` (once per email, always) and `/intake/attachment`; best-effort, never
  raising into the pipeline. **[A]** CRM integration is unaffected by the mailbox work.
- **[C]** **Whether the CRM is the system of record is not stated in the SSOT.** The
  integration guide defines endpoints and rules; it declares no precedence over the
  agent's store. **No precedence is invented here.**

### 4.10 SharePoint

- **[A]** The **only** SSOT statement is §18's *"Explicitly out of scope"* list, deferring
  it to the audit's Phases 2–5.
- **[B] The current SSOT does not provide enough target design detail to implement
  SharePoint behaviour** — no document model, no retention rules, no mapping to referrals
  or mailboxes, no access model. **No SharePoint architecture is proposed.**
- **[C]** Any inclusion is a Project West scope and design decision, not a technical one.
- **[D]** Document handling today is CRM attachment upload, retained.

### 4.11 Audit trail

- **[A]** Largely the audit's Phase 2. **But** §12 requires the send record capture the
  **approving user, the identity used, and the alias decision** — in scope because this
  work creates it.
- **[D]** `agent_runs` retained. **[B]** Record those three fields at send time.

### 4.12 Failure / retry

**[D] Retain — the strongest part of the current implementation.** Bounded socket
timeouts, per-message parse isolation, watermark-independent dedup, UIDVALIDITY
namespacing, concurrency guards, send-failure-to-draft.

**[B]** Port the equivalents to Graph, adding throttling/`429` handling the IMAP path
never needed.

---

## 5. Final gate table

| Gate | Status | Evidence required | Blocking? |
| --- | --- | --- | --- |
| **T1** Entra app-role posture | ✅ **CLOSED** | Zero Graph app-role assignments on `Project West Mail Sender` — obtained | No — but becomes a **standing invariant** |
| **T2** Send scope contents | ✅ **CLOSED** | Filter `PrimarySmtpAddress -eq 'referrals@projectwest.co.uk'`; `CustomRecipientScope`; Enabled — obtained | No |
| **T3** Legacy Application Access Policies | ⚠️ **UNVERIFIED** | `Get-ApplicationAccessPolicy` succeeding. Query currently **fails** with an object-resolution error — not proven absent | **No** for build (both SPs hold no Entra grant for a policy to constrain); **yes** for security sign-off |
| **T4** Alias sending under app-only | ⬜ **OPEN** | Live `sendMail` with `message.from` set to a proxy address, in this tenant; and whether an alias of a scoped mailbox stays in scope | **Yes — blocks P1e** |
| **T5** Alias recoverable from Graph | ⬜ **OPEN** | `toRecipients`/`ccRecipients`/`internetMessageHeaders` on real messages incl. Bcc, distribution-list and redirect cases | **Yes — blocks P1a design freeze.** The safeguarding restriction depends on it |
| **T6** Mailbox volumetrics | ✅ **CLOSED** | Per-mailbox item counts — obtained (205,316 total) | No |
| **T7** Transport rules | ✅ **CLOSED** | `Get-TransportRule` → zero rows — obtained | No |
| **T8** POC mail identity | ✅ **CLOSED** | Third identity `e18a9374-…`, tenant `d868ea1b-…`, `provider=imap`, personal Gmail — obtained | No — but it is **why** this plan exists |
| **T9** RBAC allow/deny | ✅ **CLOSED** | `Test-ServicePrincipalAuthorization`: `referrals@` True, `info@` False — obtained | No |
| **T10** Inbox rules / forwarding / folder distribution | ⬜ **OPEN** | `Get-InboxRule` per mailbox; `ForwardingSmtpAddress`/`ForwardingAddress`/`DeliverToMailboxAndForward`; `Get-EXOMailboxFolderStatistics` | **Yes — blocks mailbox seeding.** Determines whether Inbox-only polling is sound |
| **T11** Existing data disposition | ⚠️ **PARTIAL** | §6 below. Agent-side counts obtained; **CRM-side reconciliation and PW direction outstanding** | **Yes — blocks migration steps 3–4 and any deletion** |

---

## 6. T11 — existing data disposition

**[B]** Read-only aggregate counts, obtained from the POC database. **Counts only — no
message content was read.**

| Measure | Value |
| --- | --- |
| Emails stored (all Gmail-derived) | **31,809** |
| Rows carrying an `activity_ref` (**a CRM-side record exists**) | **14,002** |
| Rows with `crm_status = 'sent'` | 35 |
| Rows with a `crm_reference` (referral/case ref) | 34 |
| **Replies actually sent** | **425** |
| `received_at` range | 2020-03-29 → 2026-10-06 |

### What this establishes

- **[VERIFIED]** Gmail-derived emails exist in the POC database, at a scale (31,809) far
  beyond a handful of test messages, spanning six years of mail history.
- **[VERIFIED]** **14,002 corresponding CRM activity records exist downstream.**
  `crm_sync` posts `/intake/activity` once per email **always**, independent of referral
  outcome. **Deleting agent rows would not remove them.**
- **[VERIFIED]** 34 referral/case references and 35 submissions reached the CRM.
- **[VERIFIED]** **425 replies were actually sent** — real outbound email left the Gmail
  account to real recipients. That cannot be withdrawn by any action here.
- **[VERIFIED]** Attachments were never persisted agent-side, but `/intake/attachment`
  uploads mean **copies may exist CRM-side**.

### Required before any migration or deletion

| # | Action | Type |
| --- | --- | --- |
| 1 | Reconcile the 14,002 `activity_ref` values against CRM-side records | **[B]** |
| 2 | Determine whether `/intake/attachment` uploads persisted documents CRM-side, and which | **[B]** |
| 3 | Identify further downstream copies where technically possible — Gmail Sent Items for the 425 replies, CRM exports, any backups | **[B]** |
| 4 | **[C] Decision 18** — has real Project West correspondence passed through the personal Gmail? | **[C]** |
| 5 | **[C] Decision 20** — retention, deletion or quarantine of the 31,809 rows and their CRM counterparts | **[C]** |
| 6 | **[C] Decision 19** — whether the Gmail account remains an accepted development fixture | **[C]** |

> **[B] Nothing is deleted automatically, and nothing is deleted by this plan.** Deletion,
> if PW directs it, reaches the CRM and possibly the Gmail mailbox. Scope it before acting.
> **Deleting agent database rows does not remove CRM-side data** — that is the single most
> important thing to understand before anyone starts tidying up.

---

## A. Decisions required from Project West

| # | Decision | Blocking |
| --- | --- | --- |
| 1 | Entra groups + object ids, for **nine** users | **Yes** — everything |
| 2 | Confirm the agent registration (`d1687ded…`?) | **Yes** — ingestion |
| 3 | Name the safeguarding leads | **Yes** — P1g, acceptance test 9 |
| 4 | Cutover/cutoff boundary per mailbox | **Yes** — first sync |
| 11 | `admin@` queue and `reply_allowed` | **Yes** — alias seed |
| 14 | Extend the existing SP or create a dedicated identity | **Yes** — tenant change |
| 18 | Has real PW correspondence passed through the Gmail account? | **Yes** — data protection |
| 20 | Disposition of 31,809 rows + 14,002 CRM activities | **Yes** — migration steps 3–4 |
| 8 | Precedence when a message hits two monitored addresses | **Yes** — §3.2 `resolved_precedence` |
| 5, 6, 7, 9, 12, 13, 15, 16, 17, 19 | As previously recorded | No |
| — | Is the CRM the system of record? (§4.9) | No |
| — | SharePoint scope and design (§4.10) | No |
| — | Should `receiving_address` be nullable rather than defaulted? (§3.3) | No |

## B. Technical gates required before mailbox seeding

**T10** (Inbox rules / forwarding / folder distribution) · **T5** (alias recoverable from
Graph) · **T11** (data disposition, PW direction obtained) · **scope verification** —
`Get-Recipient -RecipientPreviewFilter` returns **exactly six**, *and*
`Test-ServicePrincipalAuthorization` passes a positive case **and a negative case against a
personal mailbox such as `arti@`**. Under RBAC-only that scope is the entire authorization
boundary; the negative test is the one that matters.

**T4** blocks P1e rather than seeding. **T3** blocks security sign-off rather than build.

## C. Implementation sequence

| Phase | Work | Gated on |
| --- | --- | --- |
| **P0** | Run T10, T11 reconciliation, retry T3; PW answers 1, 2, 4, 8, 11, 14, 18, 20; apply pending taxonomy + `org_id` migrations; create the six-mailbox scope; assign `Mail.Read` + `Mail.Send`; **grant nothing in Entra**; spike T4/T5 | Tenant access; PW |
| **P1a** | **Receiving-address capture** — `$select` extension, `recipient_evidence`, the §3.2 algorithm, the §3.3 exception states. Five `emails` columns nullable | T5; P0 read access |
| **P1b** | Authorization model — five tables, sign-in resolver, `MailboxScope`, **no mailboxes seeded** | Decision 1 |
| **P1c** | Enforcement — `mailbox_scope` on every route; route-coverage test | P1b |
| **P1d** | Multi-mailbox ingestion — seed **6 mailboxes + 4 aliases** (+ routing addresses); per-mailbox cursors, budgets, `monitor_from`; migration steps 3–4 incl. `legacy` assignment | **P1c (hard gate)**, T10, T11 |
| **P1e** | Sending identity — derive From; `can_send`; `message.from`; retire IMAP/SMTP/SendGrid | P1d, T4 |
| **P1f** | Frontend — `GET /mailboxes`, switcher, mailbox context, remove Settings form | P1d |
| **P1g** | Safeguarding — clearance table, second filter clause, scope-aware alerts, restricted-aware `auto_sendable` | P1c, decision 3 |

> **[A] The hard gate:** do not seed the six mailboxes before P1c is complete. The window
> between "six mailboxes write to one table" and "every read is filtered" is exactly the
> exposure this design exists to prevent — not even briefly, not even in staging with real
> mail.

**[B]** P1a stays first because the receiving address is being destroyed on every message
today. P1g stays last because an empty clearance table correctly hides everything, which
looks like a bug if it lands mid-phase.

## D. Reusable POC components

**[D]** Attachment handling (in-memory, CRM-forwarded, never persisted) · failure and retry
semantics (bounded timeouts, per-message isolation, concurrency guards, send-failure-to-draft)
· dedup design (`known_uids` before the count slice) · taxonomy including
`safeguarding_concern` and `safeguarding_restricted` · CRM integration (`crm_sync.py` and
its intake endpoints) · draft/approve mechanics (`reply_status`, `draft_reply`, auto-send
gating) · Entra authentication (RS256 JWT against JWKS — the only part with real test
coverage) · scheduler shape, pipeline batching, progress reporting · `Email.uid_validity`
for historical-row dedup.

## E. Components that must be replaced

| Replaced | Why |
| --- | --- |
| `mail_client.py` (IMAP + SMTP) | **[VERIFIED]** Cannot serve PW — Graph exists precisely "for tenants with IMAP Basic Auth disabled" |
| `sendgrid_client.py` | Cannot express per-mailbox or per-alias identity |
| `crypto.py` | Nothing user-supplied is encrypted once credentials move to deployment config |
| `GET`/`POST /settings/mailbox`, `/test`, `MailboxIn`, Settings form | **[A]** No mailbox configuration in the user-facing UI |
| `Mailbox.provider`, `imap_host/port`, `smtp_host/port`, `username`, `password_enc` | One provider, one transport; addresses not logins |
| `Mailbox.tenant_id`, `client_id`, `client_secret_enc` | **[A]** No mailbox credential stored in the database |
| `ux_mailboxes_singleton` | The whole point |
| `pipeline._send_fn_for`, `load_send_context` | One send path; per-mailbox and authorization-aware |
| Flat `emails.read` / `emails.write` | **[A]** Replaced by per-mailbox scope on every route |
| The configured mailbox itself | A personal Gmail in an unrelated tenant |

> **[B]** Do not delete `mail_client.py` until the Graph path is verified against all
> **six** mailboxes. Delete it at the end of the phase, not the start.

## F. Data-disposition actions required before migration

1. **[B]** Reconcile 14,002 `activity_ref` values against CRM-side records.
2. **[B]** Determine which `/intake/attachment` uploads persisted documents CRM-side.
3. **[B]** Identify downstream copies where technically possible — Gmail Sent Items (425
   replies), CRM exports, backups.
4. **[C]** Obtain PW direction (decisions 18, 19, 20) on retention / deletion / quarantine.
5. **[B]** Only then execute migration step 3 (backfill, with unmatched rows assigned to a
   seeded `legacy` mailbox **not visible in the switcher**) and step 4 (`NOT NULL` +
   immutability trigger).
6. **[B]** **Delete nothing automatically.** `conversation_id` and `internet_message_id`
   cannot be backfilled on historical rows — **[A]** leave them null and treat those rows
   as unmatched for thread purposes.

---

**No implementation performed. No code written, no database modified, no migrations run, no
Exchange or Entra changes, no mailboxes seeded, no services started, no mail fetched or
sent.**
