# Mailbox Authorization Architecture — Implementation Plan

Status: **plan only — not implemented**
Last updated: 2026-10-06

Mailbox access becomes a property of the authenticated user's identity, resolved and
enforced server-side, rather than something an administrator types into a settings form.

This plan supersedes Phase 1 of the current-state audit's build order. Phases 2–5 of
that plan (audit trail, referral depth, documents/Part 2, operational hardening) are
unchanged and follow this work.

---

## Table of contents

1. [Architecture](#1-architecture)
2. [Current vs target model](#2-current-vs-target-model)
3. [Authentication and authorization](#3-authentication-and-authorization)
4. [Entra group model](#4-entra-group-model)
5. [Mailbox access model](#5-mailbox-access-model)
6. [Database changes](#6-database-changes)
7. [Migration plan](#7-migration-plan)
8. [Backend enforcement](#8-backend-enforcement)
9. [API changes](#9-api-changes)
10. [Frontend changes](#10-frontend-changes)
11. [Alias handling](#11-alias-handling)
12. [Sending / reply identity](#12-sending--reply-identity)
13. [Safeguarding access](#13-safeguarding-access)
14. [Files to remove / deprecate](#14-files-to-remove--deprecate)
15. [Testing strategy](#15-testing-strategy)
16. [Project West decisions / blockers](#16-project-west-decisions--blockers)
17. [Revised build order](#17-revised-build-order)
18. [Definition of Done](#18-definition-of-done)

---

## 1. Architecture

There are **two planes**, and the whole design depends on not confusing them.

```
INGESTION PLANE                          AUTHORIZATION PLANE
(runs unattended, no user present)       (per request, as the user)

  Seven shared mailboxes                   User signs in with Entra ID
  Exchange Online, sign-in blocked         no mailbox credential entered
            |                                        |
            v                                        v
  Graph app-only token                     Entra group membership
  ApplicationAccessPolicy limits           resolved to an authorised
  it to those seven                        mailbox set
            |                                        |
            v                                        v
  Scheduler + pipeline                     API guard, every request
  stamps mailbox_id and                    rejects any mailbox outside
  receiving_address on each row            that set
            |                                        |
            +------------------+---------------------+
                               v
        emails — every row carries its mailbox;
        every read is filtered by the authorised set
```

The left plane never sees a user. The right plane never sees a mailbox credential.
They meet at one place: the stored email, which carries its mailbox as an immutable
property.

### Why app-only ingestion stays

Three approaches were considered:

| Approach | Verdict |
| --- | --- |
| **Delegated (on-behalf-of)** — call Graph as the signed-in user | **Rejected.** The agent polls seven mailboxes on a schedule. A delegated token needs a live user session; when nobody is signed in, nothing is collected. |
| **Runtime derivation** — app-only ingestion, ask Graph at sign-in what the user may access | **Rejected.** Microsoft Graph does not expose Exchange mailbox delegation. There is no supported endpoint returning the Full Access / Send As grants held by a user. Those live in Exchange Online (`Get-MailboxPermission`, `Get-RecipientPermission` — PowerShell, not Graph). |
| **App-only ingestion + Entra group authorization** | **Chosen.** Ingestion runs unattended; authorization is explicit, auditable, and configurable by PW IT without touching the application. |

> **Load-bearing assumption.** The application cannot ask Microsoft "which shared
> mailboxes may this person see?" and get a reliable answer. Verify this against the
> live tenant before build. If a supported Graph path does exist, this design still
> holds — it simply gains an automated way to reconcile the model against Exchange.

**Rejected shortcut:** probing (attempt a delegated read of each mailbox at sign-in,
infer access from success/failure). Slow, ambiguous — a failure may be permissions, a
throttle, or an outage — and it makes authorization depend on a system that is not the
authority for it.

### The consequence that drives everything below

Under app-only access the agent reads and sends as the **application**, not as the
person. Exchange's Full Access and Send As checks apply to humans in Outlook; an
app-only token bypasses them entirely. **Nothing in Microsoft 365 will stop the
application sending as `finance@` on behalf of a user with no Finance rights.**

The application is therefore the only thing standing between a user and a mailbox they
should not touch. The enforcement in §8 is not defence in depth — it is the defence.

### Tenant-side controls (required, not substitutes)

- **ApplicationAccessPolicy** scoping the app registration to a mail-enabled security
  group containing only the seven shared mailboxes. Without it a tenant-wide
  `Mail.Read` grant reaches `pauline@`, `arti@`, and every other named mailbox — which
  capability brief §7.2 forbids.
- **Least-privilege Graph permissions**: `Mail.Read` and `Mail.Send` application
  permissions, admin-consented, nothing wider.

---

## 2. Current vs target model

| | Current (POC) | Target |
| --- | --- | --- |
| Mailbox definition | A connection an admin configures in a form | A resource a user is authorized against |
| Mailbox count | Exactly one (`ux_mailboxes_singleton`) | Seven shared mailboxes + three aliases |
| Credentials | Per-mailbox, encrypted in `mailboxes` table | One app registration, in deployment config |
| Who configures | Any admin, via Settings UI | PW IT, via Entra groups; mailboxes seeded by migration |
| Transport | Graph **or** IMAP/SMTP **or** SendGrid | Graph only |
| Access control | Flat `emails.read` — every user sees every email | Per-mailbox, enforced on every query |
| Receiving address | Not captured | Immutable column on every email |
| Reply identity | The single configured mailbox | Derived from mailbox + alias + user's send rights |
| Safeguarding | Route and alert only; email readable by anyone | Separate clearance axis; invisible without it |

### What this closes

The audit's most serious finding: permissions are a flat `emails.read` with no mailbox
dimension, so the moment seven mailboxes fed one table every user would see
Management's confidential correspondence. Under this design that cannot happen, because
mailbox scope is part of every query rather than a filter applied afterwards.

### What does not change

Taxonomy, routing, CRM integration, the draft-vs-send controls, dedup, and the
scheduler's shape are all unaffected.

---

## 3. Authentication and authorization

Authentication is unchanged and already works. Authorization is new.

**Authentication (existing, keep as-is):**
Entra ID sign-in → platform RS256 JWT → verified against JWKS with `iss`, `aud`, `exp`,
`iat`, `sub` enforced. Access token 15 min, refresh 8 h absolute. This is the only part
of the system with real test coverage (`services/auth/tests/test_entra.py`).

**Authorization (new):** two independent axes.

| Axis | Grants | Held against |
| --- | --- | --- |
| **Mailbox access** | `can_read`, `can_send`, `can_approve` per mailbox | Entra group → mailbox rule |
| **Safeguarding clearance** | A single boolean grant | `PW-Safeguarding-Lead` membership |

They are independent because `safeguarding@` is an alias into `coordinator@`, and every
named user in the M365 Access Map has Coordinator access. If safeguarding were a mailbox
permission it would already be granted to everyone. See §13.

**Where the resolved set lives: the database, not the JWT.**
A JWT claim would be faster and equally tamper-proof, but 21 scopes bloat the token and
— more importantly — revocation would lag by the 15-minute access-token lifetime. For
safeguarding material that lag is not acceptable. A DB read is one indexed lookup,
cacheable per request.

---

## 4. Entra group model

One Entra security group per mailbox per access level. At most 21 for seven mailboxes,
plus one for safeguarding clearance.

```
PW-Mailbox-Info-Read
PW-Mailbox-Info-Send
PW-Mailbox-Info-Approve
PW-Mailbox-Referrals-Read
PW-Mailbox-Referrals-Send
PW-Mailbox-Referrals-Approve
PW-Mailbox-Coordinator-Read
...
PW-Mailbox-Management-Approve
PW-Safeguarding-Lead
```

In practice fewer are needed at the start: the Access Map grants Full Access and Send As
together in every cell, so Read and Send can begin as one group per mailbox and split
later if PW wants the distinction (**decision 6**, §16).

### Resolution at sign-in

The auth service already syncs roles into memberships on every sign-in
(`services/auth/app/routers/entra.py`). The same hook resolves mailbox access:

1. User signs in through Entra ID.
2. Read group membership — from the `groups` claim where it fits, otherwise
   `GET /me/memberOf` using the user's own token.
3. Match groups against `mailbox_access_rule`.
4. Rebuild `user_mailbox_access` for that user, taking the **union** where several
   groups overlap.
5. Set or clear `safeguarding_clearance` from `PW-Safeguarding-Lead`.

### The overage case — fail closed, loudly

Entra replaces the `groups` claim with an overage indicator above ~200 groups. Rare at
PW's size, but the resolver **must** fall back to `GET /me/memberOf` rather than
resolving to an empty set.

> Failing closed is correct. Failing to an empty set **silently** is not — the user must
> see an error, not an empty mailbox list that reads as "no mail today".

### Why not Entra app roles

App roles would also work and arrive in the `roles` claim. Not chosen because role
assignment happens in the Enterprise Application blade rather than in group management,
which is where PW IT already administers mailbox permissions. Keeping both in one place
reduces drift. **If PW IT prefers app roles, only step 2 above changes.**

### The reconciliation gap (accept it explicitly)

Entra group membership and Exchange mailbox permissions are two different systems and
nothing keeps them aligned automatically. Someone could hold Full Access to Finance in
Exchange but not be in `PW-Mailbox-Finance-Read`, or the reverse.

**The group model is the authority for this application, regardless of what Exchange
says.** PW must maintain the groups as the operational source of truth. A periodic
reconciliation report (group membership vs `Get-MailboxPermission` output) is worth
building later; it is not a blocker.

---

## 5. Mailbox access model

```
Authenticated user
      |
      v
Entra group membership  --->  mailbox_access_rule  --->  user_mailbox_access
                                                               |
                                                               v
                                                   authorised mailbox set
                                                               |
                                                               v
                                        emails WHERE mailbox_id IN (set)
```

**Rules are bound to groups, never to individual users.** The brief requires assignments
to remain configurable and forbids hard-coding individual Project West people. PW IT adds
someone to `PW-Mailbox-Finance-Read` in Entra and their access changes at next sign-in —
no deployment, no application change. The application stores which *group* grants which
mailbox (~20 stable rows) and never stores who is in it.

This also keeps joiner/leaver handling where it belongs: disabling an Entra account
removes access everywhere at once, including here.

---

## 6. Database changes

### New tables

```sql
-- System configuration. Seeded by migration. NOT user-editable.
CREATE TABLE mailboxes (
    id              varchar PRIMARY KEY,
    address         varchar NOT NULL UNIQUE,   -- referrals@projectwest.co.uk
    display_name    varchar NOT NULL,          -- "Referrals"
    graph_user_id   varchar,                   -- Graph object id, resolved once
    monitor_from    timestamptz,               -- start-date cutoff for first sync
    enabled         boolean NOT NULL DEFAULT true,
    sync_cursor     timestamptz,               -- per-mailbox watermark
    last_synced_at  timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Addresses that route into a mailbox but carry their own business meaning.
CREATE TABLE mailbox_aliases (
    id              varchar PRIMARY KEY,
    mailbox_id      varchar NOT NULL REFERENCES mailboxes(id),
    address         varchar NOT NULL UNIQUE,   -- safeguarding@projectwest.co.uk
    queue           varchar NOT NULL,          -- taxonomy queue it implies
    reply_allowed   boolean NOT NULL DEFAULT false,
    restricted      boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- The authorization model. Entra GROUPS, never individual users.
CREATE TABLE mailbox_access_rule (
    id              varchar PRIMARY KEY,
    entra_group_id  varchar NOT NULL,          -- group object id
    entra_group_name varchar,                  -- for operator legibility only
    mailbox_id      varchar NOT NULL REFERENCES mailboxes(id),
    can_read        boolean NOT NULL DEFAULT false,
    can_send        boolean NOT NULL DEFAULT false,
    can_approve     boolean NOT NULL DEFAULT false,
    UNIQUE (entra_group_id, mailbox_id)
);

-- Resolved per user at sign-in. A materialised view of the rules above.
CREATE TABLE user_mailbox_access (
    user_id         varchar NOT NULL,
    mailbox_id      varchar NOT NULL REFERENCES mailboxes(id),
    can_read        boolean NOT NULL DEFAULT false,
    can_send        boolean NOT NULL DEFAULT false,
    can_approve     boolean NOT NULL DEFAULT false,
    resolved_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, mailbox_id)
);

-- Its own table, not a column: this grant's history is the one most likely to be audited.
CREATE TABLE safeguarding_clearance (
    id              varchar PRIMARY KEY,
    user_id         varchar NOT NULL,
    granted_by      varchar,
    granted_at      timestamptz NOT NULL DEFAULT now(),
    revoked_at      timestamptz
);
CREATE INDEX ix_safeguarding_clearance_user ON safeguarding_clearance (user_id)
    WHERE revoked_at IS NULL;
```

### Changes to `emails`

```sql
ALTER TABLE emails ADD COLUMN mailbox_id          varchar REFERENCES mailboxes(id);
ALTER TABLE emails ADD COLUMN receiving_address   varchar;
ALTER TABLE emails ADD COLUMN original_recipient  varchar;
ALTER TABLE emails ADD COLUMN conversation_id     varchar;
ALTER TABLE emails ADD COLUMN internet_message_id varchar;

CREATE INDEX ix_emails_mailbox          ON emails (mailbox_id);
CREATE INDEX ix_emails_conversation     ON emails (conversation_id);
CREATE INDEX ix_emails_internet_msg_id  ON emails (internet_message_id);
```

| Column | Source | Why |
| --- | --- | --- |
| `mailbox_id` | the mailbox being polled | **The authorization key.** Every query filters on it |
| `receiving_address` | `toRecipients`/`ccRecipients` matched against known addresses and aliases | The physical address the message arrived at |
| `original_recipient` | `toRecipients`, plus `X-MS-Exchange-*` headers where present | What the sender actually typed — the alias, which carries the business meaning |
| `conversation_id` | Graph `conversationId` | Thread matching (brief Appendix E, evidence tier 2) |
| `internet_message_id` | Graph `internetMessageId` | Stable cross-system identity, second dedup signal |

### Graph `$select` change (enables all of the above)

The current `$select` in `graph_client.fetch_latest` omits every one of these. It becomes:

```
$select=id,subject,from,toRecipients,ccRecipients,receivedDateTime,
        bodyPreview,body,hasAttachments,conversationId,internetMessageId
```

**No new Graph permission is required** — these are fields on a message the app already
reads.

### Immutability

`mailbox_id` and `receiving_address` are set at insert and **never written again**. They
are authorization inputs; a mutable authorization key is a vulnerability — anything able
to update `mailbox_id` could move an email into a mailbox the attacker can read.

Enforce with a database trigger, not only in application code:

```sql
CREATE OR REPLACE FUNCTION emails_guard_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.mailbox_id IS DISTINCT FROM OLD.mailbox_id
       OR NEW.receiving_address IS DISTINCT FROM OLD.receiving_address THEN
        RAISE EXCEPTION 'mailbox_id and receiving_address are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_emails_guard_immutable
    BEFORE UPDATE ON emails
    FOR EACH ROW EXECUTE FUNCTION emails_guard_immutable();
```

### ⚠️ Do NOT make `internet_message_id` unique

The same message can legitimately appear in two monitored mailboxes (sent to both
`info@` and `referrals@`). Under Graph each copy has a different message `id`, so dedup
on `(uid_validity, uid)` still works — but `internet_message_id` will be **identical**
across those copies. It is a matching signal, not an identity. Making it unique would
silently drop the second copy.

### Deriving `receiving_address`

For each message, intersect `toRecipients` + `ccRecipients` with the combined set of
known mailbox addresses and alias addresses:

| Outcome | Action |
| --- | --- |
| **Exactly one match** (normal case) | That is the receiving address. An alias match also sets `original_recipient` |
| **No match** — forward, distribution list, or Bcc | Fall back to the polled mailbox's own address **and flag the row**; the business meaning is genuinely unknown |
| **Several matches** — e.g. addressed to both `info@` and `referrals@` | Prefer the most specific alias; record that it was ambiguous. Precedence needs a PW decision (**decision 8**, §16) |

---

## 7. Migration plan

Five steps, in order. Reversible up to step 4.

> **Prerequisite:** the taxonomy and `org_id` migrations from the previous session are
> **still unapplied**. Apply them first. These migrations assume `org_id` is already
> gone from `emails` and will fail otherwise.

### Step 1 — Add structures (safe, reversible)

Create the five new tables. Add the five `emails` columns as **nullable**. Nothing reads
them yet; nothing breaks.

### Step 2 — Seed system configuration

Insert the seven mailboxes and three aliases from the M365 Mailbox Access Map v1.

Insert `mailbox_access_rule` rows **once PW supplies the Entra group object ids**. Until
then the table is empty, which means nobody has access — correct, and the reason this
step needs PW input before the feature can be switched on.

### Step 3 — Backfill existing emails

Every existing row came from the one configured mailbox, so the association is knowable:

```sql
UPDATE emails e
   SET mailbox_id = m.id,
       receiving_address = m.address
  FROM mailboxes m
 WHERE m.address = (SELECT username FROM mailboxes_legacy LIMIT 1)
   AND e.mailbox_id IS NULL;
```

Two caveats:

1. `conversation_id` and `internet_message_id` **cannot be backfilled from the
   database** — they were never collected. Either leave them null on historical rows, or
   re-fetch from Graph by message id as a one-off job.
   **Recommendation:** leave null; treat historical rows as unmatched for thread purposes.
2. If the configured address does not match a seeded mailbox (e.g. the POC pointed at a
   test mailbox), those rows need an explicit decision, not a guess. A `legacy` mailbox
   row — seeded but **not visible in the switcher** — is the safe destination.

### Step 4 — Enforce (point of no return)

Run only after step 3 verifies zero nulls remain:

```sql
ALTER TABLE emails ALTER COLUMN mailbox_id        SET NOT NULL;
ALTER TABLE emails ALTER COLUMN receiving_address SET NOT NULL;
DROP INDEX IF EXISTS ux_mailboxes_singleton;
-- + the immutability trigger from §6
```

### Step 5 — Drop dead columns

The IMAP/SMTP columns listed in §14, **after** the Graph path is confirmed working
against all seven mailboxes.

### On the singleton

`ux_mailboxes_singleton` was added during the single-tenant work to preserve the "exactly
one mailbox" guarantee that `org_id`'s unique constraint used to provide. It was
documented at the time as the index that gets dropped when multi-mailbox lands. This is
that moment.

### First-sync cost

Each mailbox starts from its `monitor_from` cutoff, not from the beginning of time.
Without it, `reports@` alone (24,007 items / 7.5 GB) would make 24,007 LLM calls. The
cutoff is a per-mailbox value PW must supply (**decision 4**, §16) and belongs in the
step 2 seed.

---

## 8. Backend enforcement

One dependency, applied to every email route, replacing today's bare `require(READ)`.

```python
@dataclass(frozen=True)
class MailboxScope:
    user_id: str
    readable: frozenset[str]      # mailbox ids
    sendable: frozenset[str]
    approvable: frozenset[str]
    safeguarding: bool

    def assert_readable(self, mailbox_id: str) -> None:
        if mailbox_id not in self.readable:
            # 404, not 403 — do not confirm the mailbox exists
            raise HTTPException(404, "not found")

    def assert_sendable(self, mailbox_id: str) -> None: ...
    def assert_approvable(self, mailbox_id: str) -> None: ...


async def mailbox_scope(
    claims: dict = Depends(require(READ)),
    session: AsyncSession = Depends(get_session),
) -> MailboxScope:
    """The authorised mailbox set for this user.
    NEVER derived from anything in the request."""
```

### The five-step check

| Step | Where |
| --- | --- |
| 1. Authenticate the user | `require(READ)` — JWT verified against JWKS, unchanged |
| 2. Determine the authorised set | `mailbox_scope` reads `user_mailbox_access` |
| 3. Validate the requested mailbox | `scope.assert_readable(mailbox_id)` |
| 4. Filter the query | `.where(Email.mailbox_id.in_(scope.readable))` on **every** select |
| 5. Perform the action if authorised | `assert_sendable` / `assert_approvable` before the write |

### Three rules that make this hold

**1. The filter is unconditional.**
`Email.mailbox_id.in_(scope.readable)` is applied whether or not the frontend asked for
a mailbox. A mailbox id in the request can only ever *narrow* the set, never widen it.
If the frontend sends a mailbox the user cannot read, the intersection is empty and the
result is empty — the request **cannot escalate by construction**.

**2. Single-row reads go through the same path.**
`GET /emails/{id}` must not fetch by id and then check. It fetches by id **and** mailbox
scope in one query, so an unauthorised id is indistinguishable from a nonexistent one.

```python
# correct
e = await session.scalar(
    select(Email).where(
        Email.id == email_id,
        Email.mailbox_id.in_(scope.readable),
    )
)

# WRONG — leaks existence, and a later refactor may drop the check
e = await session.scalar(select(Email).where(Email.id == email_id))
if e.mailbox_id not in scope.readable:
    raise HTTPException(403)
```

**3. The scope is never passed in from outside.**
No endpoint accepts a user id, a mailbox set, or an "admin override" parameter.

### Guarding against the obvious regression

This pattern is only as good as its application, and the failure mode is a new endpoint
written later that forgets the filter. Two mitigations, both cheap:

- Route queries through a small set of helpers — `emails_for(scope)` — rather than
  building selects inline.
- Add a test that enumerates every route on the email router and asserts each one
  depends on `mailbox_scope`. See §15.

### Scope of the change

All ~20 email routes change, but mechanically: swap the dependency, add the filter.

`GET /alerts` needs particular care — it currently returns safeguarding and exception
items across everything, and must become scope-aware **before** multi-mailbox lands.

---

## 9. API changes

### New

| Endpoint | Returns |
| --- | --- |
| `GET /mailboxes` | Only the mailboxes the signed-in user may access |

```jsonc
[
  { "id": "...", "name": "Referrals", "address": "referrals@projectwest.co.uk",
    "canSend": true, "canApprove": false, "unread": 12 },
  { "id": "...", "name": "Info", "address": "info@projectwest.co.uk",
    "canSend": true, "canApprove": false, "unread": 3 }
]
```

A user with access to two mailboxes receives two entries. **Nothing in the response
hints that Management exists.**

### Changed

| Endpoint | Change |
| --- | --- |
| `GET /emails` | Scope-filtered. Accepts optional `?mailbox_id=` as a *narrowing* filter only |
| `GET /emails/{id}` | Scope-filtered in the same query; 404 when out of scope |
| `POST /emails/move`, `/archive`, `/unarchive`, `/auto-reply` | Scope-filtered on the id set — silently skips out-of-scope ids rather than erroring, so a partial selection cannot be used to probe |
| `POST /emails/{id}/reply` | Adds `assert_sendable`. **No `from` field accepted** — identity is derived (§12) |
| `POST /emails/{id}/reply/dismiss` | Scope-filtered |
| `POST /emails/{id}/retry-summary` | Scope-filtered |
| `POST /emails/analyze-backlog` | Scope-filtered |
| `GET /alerts` | Scope-filtered **and** safeguarding-filtered |
| `GET /agent/status` | Per-mailbox sync status, filtered to the authorised set |
| `GET /folders`, `POST /folders`, … | Unchanged — folders are user-scoped, not mailbox-scoped |

### Removed

| Endpoint | Why |
| --- | --- |
| `GET /settings/mailbox` | The configuration workflow being removed |
| `POST /settings/mailbox` | Same |
| `POST /settings/mailbox/test` | Connection testing is deployment-time, not user-facing |

`GET`/`POST /settings/agent` (schedule, auto-reply opt-in) stays — those remain
legitimate user settings.

---

## 10. Frontend changes

### Added

**Mailbox switcher.** A row of chips above the email workspace, rendered from
`GET /mailboxes`:

```
MAILBOXES   [ Info ]  [ Referrals ]  [ Coordinator ]
```

The selected mailbox becomes the workspace context and is passed as a filter on
subsequent calls — as a **narrowing parameter, never an authorisation claim**. Selection
persists per user across sessions.

**Mailbox context on every email.** The list shows which mailbox each message belongs
to; the detail view shows the receiving address — the alias where there was one, since
"arrived at `payments@`" tells a user more than "is in Finance". Where an alias differs
from the mailbox, show both.

### Removed

The entire mailbox connection experience from Settings: host/port fields, username and
password, the Graph tenant/client/secret form, the connection-test button, the provider
toggle.

Settings keeps: schedule, auto-reply opt-in, backlog catch-up.

### Must not exist

- No add-mailbox affordance
- No mailbox access management
- No From-address picker in the composer — reply identity is **displayed as derived
  fact**, never offered as a choice
- Where a user has read but not send, the composer is visible but sending is disabled
  with an explanation

### None of this is a security control

The backend returns nothing the user may not see, so a tampered frontend gains nothing.
That is the point of the architecture. **Put this in the code-review checklist** — it is
tempting to "fix" an authorisation question in the component where the symptom appears.

### Empty states to design deliberately

- A user with **no authorised mailboxes** sees a clear explanation and who to contact —
  not an empty inbox that reads as "no mail today".
- The group-overage failure (§4) shows an error, never a silent empty list.

---

## 11. Alias handling

An alias is a row in `mailbox_aliases` pointing at the mailbox it physically lands in,
carrying its own business meaning. **The mailbox is where the mail *is*; the alias is
what the sender *meant*.**

Seeded configuration, from the M365 Mailbox Access Map v1:

| Alias | Lands in | Queue | `reply_allowed` | `restricted` |
| --- | --- | --- | --- | --- |
| `payments@` | `finance@` | `payment_or_finance` | ✅ yes | no |
| `complaints@` | `coordinator@` | `complaint` | ✅ yes | no |
| `safeguarding@` | `coordinator@` | `safeguarding_restricted` | ❌ **no** | ✅ **yes** |

The seven mailbox addresses are themselves implicit aliases of their own mailbox, so the
matching logic has one rule rather than two: look the receiving address up in a combined
map of mailbox addresses and alias addresses.

### Why the alias must survive ingestion

Three aliases collapse onto two mailboxes. Once a message is sitting in `coordinator@`,
nothing in its content reliably says whether it came through `complaints@` or
`safeguarding@` — and those demand very different handling. The receiving address is the
only durable evidence, and **it exists only in the headers at the moment of collection.
If it is not captured then, it is gone.**

### How the alias feeds the taxonomy

The alias supplies a **prior, not a verdict**:

- An email arriving at `safeguarding@` is routed restricted **regardless of what the
  model concludes** — the sender's choice of address is itself a signal, and the cost of
  a false negative is unacceptable.
- In the other direction the classifier can escalate: a message arriving at `info@` that
  the model flags `safeguarding_indicator` is routed restricted too.

Belt and braces. **The restricted route wins whenever either fires.**

### Keeping this out of the UI

`mailbox_aliases` is system configuration — seeded by migration, changed by deployment.
Brief Appendix B asks that PW be able to update routing "without major redevelopment",
and a configuration table satisfies that (a row change, not a code change) without
putting alias routing in reach of an end user. If PW later wants a Superadmin screen,
the table supports it; out of scope now.

---

## 12. Sending / reply identity

**The From address is derived, never chosen.** The frontend sends no sender field at all.

```
email.mailbox_id         -> the mailbox the message arrived at
email.receiving_address  -> the alias, if it arrived at one
  + alias.reply_allowed  -> may the agent reply as this alias?
  + scope.sendable       -> may THIS user send from that mailbox?
  -> the From identity, or a refusal
```

If the user lacks `can_send` on that mailbox the request is refused. The draft remains,
and the UI shows it as awaiting someone who can send.

### The alias rule

A reply to a message that arrived at `payments@` goes out as `payments@` — that is the
business identity the sender used — but **only where `alias.reply_allowed` is true**.

The Access Map marks `payments@` and `complaints@` "allowed initially", and
`safeguarding@` **Restricted, with no authorised users at all**. So `safeguarding@` is
configured `reply_allowed = false`, and no user and no agent can send from it until PW
approves safeguarding leads. **That is data configuration, not a code branch.**

### ⚠️ The Send As problem under app-only

This is the sharpest edge in the design.

Exchange `Send As` governs humans acting in Outlook. An app-only token does not act as a
human — it acts as the application. If the application has `Mail.Send` scoped to a
mailbox, it **can send as that mailbox regardless of which user triggered it**. Microsoft
will not block a send on behalf of a user with no Finance rights.

**Therefore the `scope.sendable` check is not a convenience that mirrors Exchange. It is
the only control. There is no second line of defence underneath it.**

### Sending from an alias, technically

`POST /users/{mailbox}/sendMail` with `message.from` set to the alias address.

> **Verify against the live tenant before build** (**decision 5**, §16). Whether Exchange
> accepts this for a proxy address under an app-only token with `Mail.Send` behaves
> differently across tenant configurations — it is the kind of thing that works in one
> and silently rewrites the sender in another. If it does not work, the fallback is
> `Mail.Send` plus an explicit Send As grant for the app's service principal on each
> alias, configured by PW IT.

### SendGrid is retired by this change

It exists only because Railway blocks outbound SMTP, and it sends from one verified
Single Sender. It cannot express per-mailbox or per-alias identity, and verifying ten
senders to work around that would be fighting the tool. Graph `sendMail` is HTTPS,
unaffected by the SMTP block, and already in use for reading.

### Recording the send

`saveToSentItems` is already true, so the reply lands in the correct shared mailbox's
Sent Items — which is what Appendix A asks for, and comes free once the right mailbox is
addressed. The audit record should capture the approving user, the identity used, and
the alias decision.

---

## 13. Safeguarding access

### Mailbox access cannot protect safeguarding material — this is structural

`safeguarding@` routes into `coordinator@`. **Every one of the six named users in the
Access Map has Coordinator access.** So any design where safeguarding visibility is a
mailbox permission grants it to everyone, immediately.

This is not an oversight in PW's configuration — it is what makes an alias useful. The
consequence is simply that the restriction has to live one level lower: on the email,
not the mailbox.

### The rule

An email is **restricted** when any of:

- its receiving alias is marked `restricted`, or
- its route is `safeguarding_restricted`, or
- it carries the `safeguarding_indicator` flag.

A restricted email is visible only to a user holding safeguarding clearance. The scope
filter gains a second clause:

```python
.where(
    Email.mailbox_id.in_(scope.readable),
    or_(Email.restricted.is_(False), literal(scope.safeguarding)),
)
```

### What a user without clearance sees: nothing

Not a redacted row. Not a placeholder. A restricted email is absent from their list,
their counts, and their search.

> A visible "restricted item hidden" marker would itself leak that a safeguarding matter
> exists for a family — the kind of inference brief Appendix E warns about when it
> cautions against exposing sensitive data in alerts.

The existing alert behaviour already follows this instinct (the safeguarding alert omits
the subject line). That becomes stricter: **the alert is only generated for cleared users
at all.**

### Clearance is not seniority

A distinct grant, in its own table with history, driven by `PW-Safeguarding-Lead`
membership. **A Superadmin of the application does not get it implicitly.** The
administrative role that configures the system is not the clinical role that handles
disclosures, and conflating them would be the easy mistake.

### Blocker

The Access Map records safeguarding reply identity as "Restricted — none until
safeguarding leads are approved". PW has not named them. Until they do, the group exists
and is empty, which means **no user can see safeguarding items**.

That is the correct failure direction, but it must be a deliberate decision rather than a
surprise on launch day — and it means **acceptance test 9 cannot be demonstrated until PW
names at least one lead**.

### What this does NOT solve

Someone with Coordinator access can still open `coordinator@` in Outlook and read
everything, including safeguarding mail. **The application restricts the application; it
cannot restrict Exchange.**

If PW needs the material restricted at mailbox level, the answer is a separate
safeguarding mailbox rather than an alias — an operational change on their side, not a
code change on ours. Raise this explicitly (**decision 9**, §16); people may assume this
design closes a gap it cannot reach.

---

## 14. Files to remove / deprecate

This change **deletes more than it adds**. Standardising on Graph app-only makes the
IMAP and SMTP paths dead code.

| File / element | Action | Why |
| --- | --- | --- |
| `GET`/`POST /settings/mailbox` (`app/api.py`) | **Delete** | The configuration workflow being removed |
| `POST /settings/mailbox/test` | **Delete** | Connection testing is deployment-time |
| `MailboxIn` (`app/schemas.py`) | **Delete** | Nothing is submitted any more |
| Settings page mailbox form (frontend) | **Delete** | The whole connection experience |
| `app/mail_client.py` (IMAP + SMTP) | **Delete** | IMAP cannot do app-only multi-mailbox |
| `app/sendgrid_client.py` | **Delete** | Cannot express per-alias identity; Graph is HTTPS |
| `app/crypto.py` | **Delete** | Nothing user-supplied is encrypted at rest once credentials move to deployment config |
| `Mailbox.provider` | **Drop** | Only one provider remains |
| `Mailbox.imap_host/port`, `smtp_host/port` | **Drop** | IMAP and SMTP are gone |
| `Mailbox.username`, `password_enc` | **Drop** | Replaced by `address` |
| `Mailbox.tenant_id`, `client_id`, `client_secret_enc` | **Move to deployment config** | One app registration serves all seven |
| `Mailbox.uid_validity`, `sync_epoch`, `last_synced_uid` | **Drop after migration** | IMAP-specific; Graph ids are globally stable |
| `Email.uid_validity` | **KEEP** | Still part of the dedup constraint on historical rows |
| `pipeline.load_send_context` | **Rewrite** | Becomes per-mailbox and authorisation-aware |
| `pipeline._send_fn_for(provider)` | **Delete** | One send path |
| `ux_mailboxes_singleton` | **Drop** | The whole point |

### The credentials question

Today the Graph tenant id, client id and client secret live in the `mailboxes` table,
encrypted. Under one app registration serving seven mailboxes, per-mailbox credentials
make no sense. They move to environment configuration alongside every other secret.

That removes the only reason `crypto.py` exists and takes the application **out of the
business of storing credentials entirely** — a meaningful reduction in what a database
compromise would yield.

### ⚠️ Sequencing caution

**Do not delete `mail_client.py` until the Graph path is verified against all seven
mailboxes in the live tenant.** If an unexpected tenant restriction forces a fallback,
having just removed the alternative would be an awkward position. Delete it at the *end*
of the phase, not the start.

---

## 15. Testing strategy

The repository currently has **one** test file (`services/auth/tests/test_entra.py`,
12 cases). This work must not follow that precedent — it is authorization code.

### Priority 1 — Authorization (non-negotiable)

| Test | Asserts |
| --- | --- |
| `test_every_email_route_requires_scope` | Enumerate every route on the email router; assert each depends on `mailbox_scope`. **Catches the forgotten-filter regression** |
| `test_list_excludes_unauthorized_mailboxes` | A user with Info only never sees a Management email |
| `test_get_by_id_out_of_scope_returns_404` | Not 403 — existence must not be confirmed |
| `test_mailbox_id_param_cannot_widen` | Requesting a mailbox outside scope yields empty, not more |
| `test_bulk_ops_skip_out_of_scope_ids` | A mixed id list acts only on authorised ids |
| `test_send_requires_can_send` | Read-only user on a mailbox is refused |
| `test_union_across_overlapping_groups` | Two groups granting the same mailbox produce the union |
| `test_no_groups_yields_empty_scope` | And the API returns an explanatory error, not a silent empty list |
| `test_group_overage_falls_back_to_graph` | Overage indicator triggers `memberOf`, never an empty set |

### Priority 2 — Safeguarding

| Test | Asserts |
| --- | --- |
| `test_restricted_hidden_without_clearance` | Absent from list, counts, search, and alerts |
| `test_restricted_visible_with_clearance` | Present for a cleared user |
| `test_safeguarding_alias_forces_restriction` | Arrival at `safeguarding@` restricts regardless of model output |
| `test_safeguarding_flag_forces_restriction` | `safeguarding_indicator` on an `info@` email restricts it |
| `test_superadmin_does_not_get_clearance_implicitly` | Admin role ≠ clearance |
| `test_no_send_from_safeguarding_alias` | `reply_allowed = false` is honoured |

### Priority 3 — Ingestion and identity

| Test | Asserts |
| --- | --- |
| `test_receiving_address_from_alias` | `payments@` in `toRecipients` → alias recorded |
| `test_receiving_address_no_match_flags_row` | Forwarded/Bcc mail falls back and flags |
| `test_same_message_in_two_mailboxes` | Two rows, both kept; `internet_message_id` identical |
| `test_mailbox_id_immutable` | The trigger rejects an update |
| `test_reply_identity_derived_from_alias` | Reply to `payments@` mail goes out as `payments@` |
| `test_reply_identity_ignores_client_supplied_from` | A `from` in the request body is ignored or rejected |

### Priority 4 — Migration

Run steps 1–4 against a copy of production data and assert: zero null `mailbox_id`,
row count unchanged, no duplicate rows created, singleton index gone.

### Mocks needed

A Graph fixture (message JSON with `toRecipients`, `conversationId`,
`internetMessageId`) and an Entra group-claims fixture. Neither exists today; both are
small and unblock everything above.

### Acceptance

Capability brief demonstration tests 1–14 remain the contractual bar. This work makes
tests 7 (payments alias → Finance), 9 (safeguarding restricted alert) and 13 (staff
sends from the correct PW address) demonstrable for the first time — **test 9 only once
PW names a safeguarding lead**.

---

## 16. Project West decisions / blockers

Decisions 1–4 **block the build**. The rest can be answered while it proceeds.

| # | Decision | Blocks | Why it cannot be assumed |
| --- | --- | --- | --- |
| **1** | **Create the Entra security groups** and supply object ids | Everything — with no rules nobody has access | Group ids are tenant data. The naming convention in §4 is a proposal; PW IT may have their own |
| **2** | **Confirm the agent's app registration** and apply an `ApplicationAccessPolicy` scoped to the seven shared mailboxes | Ingestion; §7.2 compliance | The Access Map covers six human users and is silent on the service identity. Unscoped, the app reads every mailbox in the tenant |
| **3** | **Name the safeguarding leads** | Safeguarding path; acceptance test 9 | Access Map says "none until approved". Until someone is named, no user can see safeguarding items |
| **4** | **A `monitor_from` date per mailbox** | First sync | `reports@` holds 24,007 items. Without a cutoff, 24,007 LLM calls on mostly historical mail |
| 5 | **Verify alias sending** under app-only `Mail.Send` with `message.from` | Reply identity | Behaves differently across tenant configurations. If it fails, the service principal needs explicit Send As per alias |
| 6 | **Read and Send as one grant, or two?** | Group count | The Access Map grants both together in every cell. One group per mailbox is simpler; two is more precise |
| 7 | **Who may approve?** | The `can_approve` axis | Distinct from sending in the brief, but PW has not defined who holds it. May map to seniority rather than mailbox |
| 8 | **Precedence when an email hits two mailboxes** | `receiving_address` derivation | An email to both `info@` and `referrals@` has two valid answers. Most-specific-alias-wins is a guess |
| 9 | **Should Coordinator access be narrowed?** | Nothing technical — an operational choice | The application cannot stop a Coordinator reading safeguarding mail in Outlook. Only a separate mailbox would |
| 10 | **Confirm `admin@` is a shared mailbox** | The seed | Access Map v1 says yes; the September M365 report said alias onto `management@`. v1 taken as authoritative |

> **Decision 9 deserves a conversation, not a table row.** PW may believe this design
> restricts safeguarding material, and within the Email Agent it does. But everyone with
> Coordinator access can still read the same messages directly in Outlook, because
> `safeguarding@` is an alias into a mailbox they all hold. If the intent is genuine
> restriction, that is an Exchange change on PW's side — better raised now than
> discovered at an inspection.

---

## 17. Revised build order

Replaces Phase 1 of the current-state audit. Phases 2–5 there are unchanged and follow.

### Phase 0 — Unblock (days, mostly PW-side)

- Apply the pending taxonomy and `org_id` migrations (everything below assumes them)
- PW creates Entra groups, applies `ApplicationAccessPolicy`, confirms the app
  registration, supplies `monitor_from` dates
- **Spike:** verify alias sending against the live tenant (decision 5) — it changes the
  sending design if it fails

### Phase 1a — Capture (small, do first)

Extend the Graph `$select`. Add the five columns as nullable. Start writing them for
newly ingested mail.

Independently valuable and low-risk: nothing reads the columns yet, but every email
collected from this point carries its mailbox and alias. **Doing this first means the
data accumulates while the rest is built**, instead of starting empty on launch day.

### Phase 1b — The authorization model

The five new tables, the sign-in resolver, `MailboxScope`. Build it with no mailboxes
seeded, so it is exercisable before it is load-bearing.

### Phase 1c — Enforcement

Apply `mailbox_scope` to every email route. Add the route-coverage test.

Nothing is user-visible yet — with one seeded mailbox the behaviour is identical to
today, which makes this **a safe change to verify in production before it matters**.

### Phase 1d — Multi-mailbox ingestion

Seed the seven mailboxes and three aliases. Per-mailbox sync cursors. Migration steps
3–4 (backfill, then enforce).

This is where the system genuinely changes behaviour, and it lands with authorisation
already proven by 1c.

### Phase 1e — Sending identity

Derive From from mailbox + alias. Add the `can_send` check. Remove IMAP/SMTP/SendGrid
once Graph is confirmed across all seven.

### Phase 1f — Frontend

`GET /mailboxes`, the switcher, mailbox context on emails, removal of the Settings
mailbox form.

### Phase 1g — Safeguarding restriction

Clearance table, second filter clause, scope-aware alerts.

Last, because it depends on PW naming leads, and because an empty clearance table
correctly hides everything — which would look like a bug if it landed mid-phase.

### The ordering principle

Capture first because data accumulates. Enforcement **before** multi-mailbox, so
authorisation is proven while there is still only one mailbox and the blast radius is
zero. Sending and safeguarding last, because both depend on answers PW has not given.

### ⚠️ What not to do

**Do not seed the seven mailboxes before 1c is complete.** The window between "seven
mailboxes write to one table" and "every read is filtered" is exactly the
data-protection exposure this design exists to prevent. It should never be open — not
even briefly in a staging environment holding real mail.

---

## 18. Definition of Done

### Functional

- [ ] A user sees exactly the mailboxes their Entra groups grant — no more, no fewer
- [ ] A user with no groups sees an explanatory error, not an empty inbox
- [ ] Switching mailbox filters the workspace; the current mailbox is always visible
- [ ] Every email displays its mailbox, and its receiving alias where one applies
- [ ] A reply goes out from the correct mailbox or alias, derived not chosen
- [ ] A user without `can_send` cannot send; the draft survives and is visibly awaiting someone who can
- [ ] No mailbox configuration exists anywhere in the user-facing UI
- [ ] Restricted items are invisible — list, counts, search, alerts — without clearance

### Security

- [ ] Every email route depends on `mailbox_scope`, asserted by an automated test
- [ ] A mailbox id in a request can only narrow, never widen (test)
- [ ] Out-of-scope single-row reads return 404, not 403 (test)
- [ ] `mailbox_id` and `receiving_address` are immutable, enforced by DB trigger (test)
- [ ] No endpoint accepts a user id, mailbox set, or admin override
- [ ] `ApplicationAccessPolicy` confirmed limiting the app to the seven shared mailboxes
- [ ] No mailbox credential is stored in the database

### Data

- [ ] Every row in `emails` has a non-null `mailbox_id` and `receiving_address`
- [ ] `ux_mailboxes_singleton` dropped
- [ ] Seven mailboxes and three aliases seeded and matching the M365 Access Map
- [ ] `internet_message_id` is **not** unique
- [ ] Historical rows associated with a mailbox, or explicitly assigned to `legacy`

### Operational

- [ ] Each mailbox syncs independently, with its own cursor and `monitor_from`
- [ ] First sync does not exceed the agreed LLM-call budget for any mailbox
- [ ] IMAP, SMTP and SendGrid paths removed, after Graph is confirmed on all seven
- [ ] Migration rehearsed against a copy of production data

### Documentation

- [ ] This file updated with any decisions that changed during build
- [ ] Entra group → mailbox mapping documented for PW IT, including how to add a user

### Explicitly out of scope

Audit trail, referral matching hierarchy, Part 2 workflow, SharePoint, exception-queue
workspace, prompt-injection defence. All remain in the current-state audit's Phases 2–5.
