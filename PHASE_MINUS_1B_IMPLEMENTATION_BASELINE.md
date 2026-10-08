# Phase -1B — Corrected Implementation Baseline

**Project West Email Agent — reconciliation of SSOT, Audit, and live tenant evidence**

| | |
| --- | --- |
| Date | 2026-10-06 |
| Scope | **Analysis only.** No code, no RBAC, no Exchange configuration, no database, no migrations. Nothing committed. |
| Supersedes | The topology and tenant assumptions in `MAILBOX_AUTHORIZATION_PLAN.md` |
| Inputs | `PHASE_MINUS_1A_TENANT_VERIFICATION_REPORT.md` · `mail_output.txt` (902-line live EXO transcript) · `MAILBOX_AUTHORIZATION_PLAN.md` |

## Labelling

Every statement below carries one of four tags:

| Tag | Meaning |
| --- | --- |
| **[VTF]** | **Verified tenant fact** — observed in the live Exchange transcript |
| **[SSOT]** | **SSOT requirement** — from the capability brief / Access Map / plan, binding on the build |
| **[DIC]** | **Developer implementation choice** — ours to make; recorded so it can be challenged |
| **[PWD]** | **Unresolved PW decision** — nobody may assume an answer |

---

## 0. Two things the instruction assumed that I do not have

Stated first, because both change what this analysis can claim.

### 0.1 Gapfill: partially supplied, as a later narrative update

No Gapfill transcript file exists on disk — `mail_output.txt` remains byte-identical to
the Phase -1A capture. **A subsequent live-verification update supplied the RBAC results
narratively**, and those are incorporated as **[VTF]**: the scope filter, the assignment
binding, the AAP query failure, and the Graph app-role check (§§2.1–2.3).

**Still not supplied, and not inferred:** per-mailbox item counts (T6), transport rules
(T7), and a successful AAP enumeration (T3 — the query *failed*, which is not the same as
returning nothing). Those remain open gates in §2.

**[DIC]** The RBAC facts arrived as prose rather than transcript. They are internally
consistent and specific enough to rely on, but if any of this is going into a compliance
record, capture the raw `Format-List` output to sit alongside `mail_output.txt`.

### 0.2 "Audit V2" is not in this working tree

`MAILBOX_AUTHORIZATION_PLAN.md` refers to a "current-state audit" whose Phase 1 it
supersedes and whose Phases 2–5 (audit trail, referral depth, documents/Part 2,
operational hardening) it leaves intact. **That audit document is not in the repository.**
I have reconciled against the plan's references to it rather than the document itself.
Where this analysis touches audit scope, it is reasoning from those references, and should
be checked against the real document.

---

## 1. Corrected implementation baseline

### 1.1 Topology — the correction that drives everything

**[VTF]** Six physical shared mailboxes, four aliases:

| # | Physical mailbox | Graph object id (`ExternalDirectoryObjectId`) | Aliases landing here |
| --- | --- | --- | --- |
| 1 | `info@projectwest.co.uk` | `9609fee8-549a-47b2-a2fc-e2e1c6d77e6d` | — |
| 2 | `referrals@projectwest.co.uk` | `cbe6ae6b-9f76-4b02-84ea-0088f561e62d` | — |
| 3 | `coordinator@projectwest.co.uk` | `29bad478-47bd-4c04-9143-c511a6b1dfcf` | `complaints@`, `safeguarding@` |
| 4 | `finance@projectwest.co.uk` | `9c4588ef-287d-4f6f-8d11-5f62d2ae29d1` | `payments@` |
| 5 | `reports@projectwest.co.uk` | `70e3685c-3a77-40d5-9d85-e9bb5e1ef4a3` | — |
| 6 | `management@projectwest.co.uk` | `188f91f4-b916-4fed-8b88-5d30a81f5cde` | **`admin@`** |

**[VTF]** `admin@` is a secondary SMTP proxy on `management@`, not a mailbox. **[SSOT]**
Access Map v1 said it was a mailbox; the September M365 report said alias. The plan's §16
decision 10 chose v1. **The tenant shows the September report was right**, so Access Map
v1 is superseded wherever it conflicts with the tenant.

**[DIC]** The six `ExternalDirectoryObjectId` values above seed `mailboxes.graph_user_id`
directly. No runtime resolution step is needed, which removes a startup dependency the
plan implied.

### 1.2 What changes in the plan, concretely

| Plan element | Was **[SSOT]** | Becomes **[VTF]**-corrected |
| --- | --- | --- |
| `mailboxes` seed | 7 rows | **6 rows** |
| `mailbox_aliases` seed | 3 rows | **4 rows** (`admin@`→`management@` added) |
| Entra groups (§4) | 21 + 1 | **18 + 1**, or **12 + 1** if read/send merge |
| §1 architecture diagram | "Seven shared mailboxes" | Six |
| §7 step 2 seed | "seven mailboxes and three aliases" | Six and four |
| §14 sequencing caution | "verified against all seven" | All six |
| §17 Phase 1d | "Seed the seven mailboxes and three aliases" | Six and four |
| §18 Data DoD | "Seven mailboxes and three aliases seeded" | Six and four |
| §18 Security DoD | "`ApplicationAccessPolicy` confirmed limiting the app to the seven" | **RBAC for Applications** scoped to the six |
| §4, §13 user count | "six named users in the Access Map" | **Nine** |

**[VTF]** The nine named users: arti, pauline, shawana, avis, angela, sarah, praveena,
jeffrey, deshaun.

### 1.3 Permission model — validated, with one simplification

**[VTF]** Full Access and Send As are granted together in every cell, on all six
mailboxes. **[DIC]** This confirms the plan's own suggestion: begin with **one Entra group
per mailbox** carrying read+send, and split later only if PW asks. That is 6 groups + 1
safeguarding group to start, not 21. Decision 6 is answerable by evidence rather than
preference — though the final call is **[PWD]**.

**[VTF]** `GrantSendOnBehalfTo` is empty on all six mailboxes. **[DIC]** Keep the
`can_approve` column (it maps to the brief's approval concept, not to Exchange's Send on
Behalf), but drop any notion of deriving a third axis from Exchange delegation — there is
nothing there to derive from.

### 1.4 `admin@` creates a new modelling problem the plan never had

**[VTF]** `admin@` lands in `management@`, which **[VTF]** only three people hold (arti,
pauline, shawana) — the tightest permission set of any mailbox.

The plan modelled `admin@` as its own mailbox with its own access group. As an alias it
inherits Management's access. Consequences:

- **[PWD]** What queue does `admin@` imply? The other three aliases each carry a business
  meaning (`payment_or_finance`, `complaint`, `safeguarding_restricted`). `admin@` has
  none assigned, because the plan never treated it as an alias.
- **[PWD]** Is `reply_allowed` true for `admin@`? Unspecified — the Access Map covers only
  the three aliases it knew about.
- **[PWD]** Is it intended that `admin@` correspondence is visible to three people only?
  If PW's mental model is "the general administrative inbox", the tenant contradicts it.

**[DIC]** Until PW answers, seed `admin@` with `reply_allowed = false` and no queue
override. That fails closed and is reversible by a row update.

### 1.5 Five uncatalogued routing addresses

**[VTF]** `referrals6151@`, `coordinator2744@`, `finance@`, `management@` (all
`@ProjectWest27.onmicrosoft.com`), plus `SIP:` entries on `info@` and `reports@`.

**[DIC]** Add the four `.onmicrosoft.com` SMTP addresses to `mailbox_aliases` as
non-replyable, no-queue rows. Under §6's derivation they currently match nothing, so
every message arriving at one falls to the "no match" branch and is flagged as ambiguous —
noise that is trivially avoidable. `SIP:` addresses are not mail routes and are ignored.

---

## 2. Remaining technical gates

### 2.1 ✅ Gate T1 — RESOLVED, and the answer is good news

**[VTF] (2026-10-06 live verification)** The `Project West Mail Sender` service principal
has **no Microsoft Graph application-role assignments at all** — including no `Mail.Read`.

Earlier in this analysis I framed this as a dichotomy: either (a) `Mail.Read` unconsented
→ ingestion blocked, or (b) consented with no restriction → tenant-wide read exposure.
**The live evidence shows (a), and the exposure scenario (b) is closed for this service
principal.** There is no tenant-wide Graph grant to leave unrestricted.

> **This also corrects the remediation I proposed in §4.1.** I listed "grant `Mail.Read`
> application permission with admin consent" as change E1. **Following that literally
> would have been actively harmful.** Microsoft's guidance is explicit: Graph application
> permissions granted in Entra are organization-wide and **bypass Exchange Online RBAC
> scoping**, and for each permission assigned through Exchange RBAC the matching
> tenant-wide Graph permission should be *removed* from the app registration. Granting
> Entra `Mail.Read` would therefore have overridden the very scope built to contain it —
> manufacturing exposure (b) by hand. §4.1 is corrected accordingly.

**[VTF]** The current posture — Exchange RBAC role assignment, zero Graph app roles — is
**the recommended modern configuration**, not a misconfiguration. RBAC for Applications is
Microsoft's replacement for Application Access Policies and is designed to work without
tenant-wide Graph permissions.

**[DIC]** Adopt the RBAC-only model deliberately and record it as the architecture.
Its decisive property is not that it is currently well-scoped but that **the catastrophic
misconfiguration becomes impossible by construction**: with no tenant-wide permission in
existence, no future scope error can widen access beyond the management scope. That is a
stronger guarantee than any amount of careful filter-writing.

**[VTF] (live Graph verification — E3 now closed)** The sign-in registration has been
examined:

| Property | Value |
| --- | --- |
| DisplayName | **PW Agents (Staging)** |
| AppId | `49dc5530-53c6-4cbc-8c90-8f7686cf02e3` |
| ObjectId | `b016795b-cbf6-447a-b635-ea564634834d` |
| AccountEnabled | True |
| `Get-MgServicePrincipalAppRoleAssignment -All` | **zero assignments** |

**[VTF] Both POC service principals hold zero Graph application-role assignments.** That
is the correct posture for each, for different reasons: the sign-in app is delegated and
should never hold application permissions, and the mail app is authorized entirely through
Exchange RBAC. **The §4.1 instruction — grant nothing in Entra — is confirmed from both
sides and there is no tenant-wide grant anywhere in the POC's identity surface.**

> ⚠️ **One discrepancy worth resolving.** The tenant calls this registration **"PW Agents
> (Staging)"**. The repository calls it **"PW Agents (Dev)"**
> (`frontend-mail-agent/.env.local:11`). And **[VTF]** the same AppId is configured against
> `NEXT_PUBLIC_AZURE_REDIRECT_URI=https://project-west-agents.vercel.app` — a deployed
> URL, not a local one. Three environment labels for one registration. Either a
> Staging-named app is serving the live frontend, or a production registration exists that
> the repository does not reference. **[PWD]** 16, §3.
>
> **Not a security finding** — it holds no application permissions either way, is
> single-tenant (`AzureADMyOrg`), and its publisher domain is `projectwest.co.uk`. But it
> makes "which app is production?" unanswerable from the repository, and that question
> bites when credentials are rotated or the app is locked down.
>
> **[DIC] Do not rename or otherwise change the registration.** Renaming is the obvious
> tidy-up and the wrong first move: **[VTF]** the DisplayName is what operators match on in
> the Entra portal, and it may already be referenced in PW's own runbooks or change
> records that this repository cannot see. The cheap, reversible fix is to correct the
> *repository's* comment to match Entra, and leave the tenant alone pending PW's decision.

**[DIC]** Minor, unrelated: `frontend-mail-agent/.env.local` declares
`NEXT_PUBLIC_AZURE_REDIRECT_URI` **twice** (lines 14–15, the Vercel URL then
`localhost:3001`). Most dotenv parsers take the last value. Harmless in a local file —
worth a glance if that file is ever used as the basis for deployment configuration.

### 2.2 ✅ Gate T2 — RESOLVED: the send scope is exactly one mailbox

**[VTF] (live)** The assignment and its scope, verified directly:

| Property | Value |
| --- | --- |
| Service principal ObjectId | `30b62c54-4953-44fa-bd95-43ed7d412a1f` |
| Role | `Application Mail.Send` |
| Assignment type | Direct, **Enabled** |
| `CustomResourceScope` | `ProjectWest-Referrals-Mail-Scope` |
| **Recipient filter** | **`PrimarySmtpAddress -eq 'referrals@projectwest.co.uk'`** |
| `RecipientWriteScope` | `CustomRecipientScope` |
| `RecipientReadScope` | `Organization` |

**[VTF]** Exchange-side `Mail.Send` is scoped to **`referrals@projectwest.co.uk` and
nothing else.** The assignment *is* bound to the scope, and the filter is a single
explicit address.

Two observations:

- **[DIC]** The filter is written as an explicit `PrimarySmtpAddress -eq` equality, not a
  class predicate. That is exactly the shape §4.2 argues for — it admits one named
  mailbox and cannot silently absorb a future one. **The existing scope is a good
  template; the only thing wrong with it is its size.**
- **[DIC]** `RecipientReadScope: Organization` governs which recipient *directory objects*
  the role assignment can resolve, not mailbox content. Paired with `Application
  Mail.Send` it confers no mail-reading ability. Worth confirming with PW IT so nobody
  mistakes it for a read grant, but it is not an exposure.

**Per instruction, the existing sender assignment is not treated as reusable for
ingestion** — and the verified filter now makes that concrete rather than cautionary:
it is the wrong role (`Mail.Send`) bound to a scope containing one of the six mailboxes.

### 2.3 ⚠️ Gate T3 — Application Access Policies: UNVERIFIED, not absent

**[VTF]** `Get-ApplicationAccessPolicy` was attempted and **failed with an Exchange
object-resolution error**. **[DIC]** A failed query is not a negative result. Legacy AAP
state is recorded as **UNVERIFIED** and must not be written up as "no policies exist."

**[DIC]** Lower risk than it was, for two reasons: an AAP restricts a tenant-wide Graph
permission, and **[VTF]** this service principal has none for an AAP to act on. Any policy
found would be inert with respect to this app. It still needs confirming — a policy
attached to a *different* app could matter — but it no longer gates the build.

Retry once the resolution error is diagnosed (commonly a stale or unresolvable
`PolicyScopeGroupId` from a deleted group):

```powershell
Get-ApplicationAccessPolicy -ErrorAction Continue | Format-List
```

### 2.4 Gate T4 — alias sending under app-only

**[SSOT]** Plan decision 5. **[VTF]** Now testable immediately: the send scope is
`referrals@` alone, and `referrals@` has **[VTF]** no business aliases — so the spike must
run against a mailbox that does, which means it is gated on X1/X3 widening the scope.

Questions: does `POST /users/{mailbox}/sendMail` with `message.from` set to a proxy
address work in *this* tenant; and does sending as an alias of a scoped mailbox stay
inside the scope, or does the RBAC filter match on `PrimarySmtpAddress` only and reject
it? **[DIC]** The second question is new and specific to the verified filter shape:
`PrimarySmtpAddress -eq 'referrals@projectwest.co.uk'` matches the *mailbox*, so sending
as `payments@` from a scope entry of `finance@` should be admitted — but "should" is not
"verified", and this is exactly the class of thing that differs between tenants.

### 2.4b ✅ Gate T9 — Exchange RBAC authorization empirically verified

**[VTF] (live, `Test-ServicePrincipalAuthorization`)** — no configuration changed:

| Test | Resource | Role | Scope | `InScope` |
| --- | --- | --- | --- | --- |
| **Allow** | `referrals@projectwest.co.uk` | `Application Mail.Send` | `ProjectWest-Referrals-Mail-Scope` (`CustomRecipientScope`) | **True** |
| **Deny** | `info@projectwest.co.uk` | same | same | **False** |

**[VTF]** The scope boundary is real and enforced: the intended mailbox is admitted, an
out-of-scope mailbox is refused. Testing the *negative* case as well as the positive is
what makes this evidence rather than a smoke test — a scope that admits everything would
also have returned True for `referrals@`.

**[DIC] What this closes, precisely.** Microsoft's documentation states the test cmdlet
"simulate[s] the behavior enabled by RBAC assignments" and **"excludes permissions that
might be granted separately in Microsoft Entra ID."** Normally that caveat would limit the
result — but here it is moot: **[VTF]** this service principal has *zero* Entra
permissions, so there is nothing excluded. The simulation and the effective state
coincide.

**[DIC] What remains, and why I am no longer treating it as a gate.** Strictly, this
verifies Exchange's authorization decision rather than a live Graph HTTP call. But
Microsoft's own documentation now settles the architectural question that T9 was raised to
answer:

> *"You might notice these roles represent Microsoft Graph permissions that you can
> consent to elsewhere in the Azure Identity platform. **These permissions have the same
> effect as the Graph permissions** except for these role assignments allowing for
> granular resource scoped access."*

RBAC-only is therefore sufficient for Graph by design, and T9's premise — that both layers
might be required — is disproved. **[DIC]** Downgrade from a blocking gate to a
confirmation step: fold a real `POST /users/referrals@.../sendMail` into the Phase 0f
alias-sending spike rather than running it separately. Residual risk is low.

### 2.5 Gate T5 — alias recoverability from Graph

**[SSOT]** Plan §11: the receiving address "exists only in the headers at the moment of
collection. If it is not captured then, it is gone." **[VTF]** `complaints@` and
`safeguarding@` both land in `coordinator@`, so once a message is in that mailbox nothing
distinguishes them except the recipient fields.

Open: whether `toRecipients`/`ccRecipients` reliably carry the alias, or whether
`internetMessageHeaders` is needed for Bcc, distribution-list and redirect cases. **The
entire safeguarding restriction depends on this.** Untestable without read access, so it
is downstream of T1.

### 2.6 ✅ Gate T6 — RESOLVED, and the plan understated the problem by 8×

**[VTF] (live, `Get-EXOMailboxStatistics`)** — no configuration changed:

| Mailbox | Items | Size | Share of corpus | Drain time at current settings¹ |
| --- | ---: | ---: | ---: | ---: |
| `info@` | **161,833** | 45.13 GB | **78.8%** | **337 days** |
| `reports@` | 24,162 | 7.519 GB | 11.8% | 50 days |
| `finance@` | 15,337 | 3.249 GB | 7.5% | 32 days |
| `management@` | 3,668 | 1.174 GB | 1.8% | 7.6 days |
| `referrals@` | 172 | 8.278 MB | 0.08% | 8.6 hours |
| `coordinator@` | 144 | 27.21 MB | 0.07% | 7.2 hours |
| **Total** | **205,316** | **~57 GB** | 100% | **~428 days** |

¹ **[VTF] (repo)** `FETCH_PER_RUN=20`, `CRON_INTERVAL_MINUTES=60`
(`services/apps/mail_agent/.env`) — 480 messages/day.

**[SSOT]** The plan's decision 4 says *"`reports@` holds 24,007 items. Without a cutoff,
24,007 LLM calls on mostly historical mail."* **[VTF]** That figure is confirmed almost
exactly (24,162 today) — **but `reports@` is not the problem.** The plan never saw `info@`,
which is **6.7× larger** and alone accounts for nearly four fifths of all mail in the
tenant. The real exposure without a cutoff is **~205,000 LLM calls, not ~24,000.**

#### The distribution inverts the priority

**[VTF]** The two mailboxes the agent exists to serve are the two smallest:
`referrals@` (172 items) and `coordinator@` (144 items) together hold **316 items —
0.15% of the corpus.** `info@`, a general-purpose catch-all, holds 78.8%.

**[DIC]** This is the most useful thing T6 tells us, and it points to a strategy the plan's
schema already supports but its prose does not: **`monitor_from` is per-mailbox, so the
answer is not one cutoff but six.** Full history is *free* where it matters most, and
unaffordable only where it matters least.

| Mailbox | **[DIC]** Suggested posture | Rationale |
| --- | --- | --- |
| `referrals@`, `coordinator@` | **Full history** | 316 items total. Negligible cost, maximum value — and `coordinator@` carries the safeguarding alias |
| `management@` | Full history or 12 months | 3,668 items is affordable either way |
| `finance@` | 6–12 months | `payments@` routing matters; older finance mail rarely needs an agent |
| `reports@` | **Cutover only** | 24k items, 7.5 GB, and the name suggests automated output — likely low classification value |
| `info@` | **Cutover only** | 162k items. Historical ingestion is not a cost decision, it is out of the question |

**[PWD] 4 is unchanged in substance but transformed in framing.** As the user states,
historical ingestion must not be assumed and PW must give an explicit boundary. The
distinction worth putting to them in these words:

- A **cutover** boundary means `monitor_from` = go-live date; nothing historical is read.
- A **cutoff** means `monitor_from` = some past date; a bounded backfill runs once.

**[DIC]** Default to **cutover for every mailbox**, then let PW opt specific mailboxes into
a cutoff. That is the fail-safe direction: the cost of starting too late is that someone
re-sends an email; the cost of starting too early is a multi-week, multi-thousand-pound
run that cannot be stopped cleanly once drafts start reaching queues.

#### Two engineering consequences

**[DIC] 1 — A shared fetch budget would starve the mailboxes that matter.** **[VTF] (repo)**
`fetch_per_run` is a single value applied per pipeline run (`pipeline.py:479`,
`config.py:15`), which was correct when `ux_mailboxes_singleton` guaranteed one mailbox.
Under six, if the budget stays global and mailboxes are polled in any order that lets
`info@` consume it, `referrals@` — **[VTF]** 0.08% of the corpus and the core of the
business workflow — can be delayed indefinitely behind a catch-all inbox. **The plan's §18
promise that "each mailbox syncs independently, with its own cursor" must extend to the
budget, not just the cursor.** Per-mailbox `fetch_per_run`, or a fair-share scheduler.

**[DIC] 2 — Make the first-sync budget an enforced cap, not an aspiration.** **[SSOT]**
§18 lists *"First sync does not exceed the agreed LLM-call budget for any mailbox"* as a
Definition-of-Done item. Given 205,316 reachable items and a `monitor_from` that is a
single seeded date, a typo is a six-figure LLM run. **[DIC]** Enforce a hard per-mailbox
ingestion ceiling in code that halts and alerts rather than continuing — a mis-set date
should fail loudly and cheaply, not succeed expensively.

#### Order-of-magnitude cost

**[DIC]** Illustrative only — PW should price it against the model actually in use. At one
classification call per message and ~2,500 input / ~300 output tokens per call, full
historical ingestion of all six mailboxes is roughly **510M input and 62M output tokens**.
Excludes draft generation, retries, and attachment handling. The `referrals@` +
`coordinator@` full-history option, by contrast, is ~316 calls — **under a million tokens
in total**, which is why it should simply be done.

### 2.7 ✅ Gate T7 — RESOLVED: no transport rules exist

**[VTF] (live, `Get-TransportRule`)** Zero rows. No configuration changed.

**[DIC]** This closes the §3.3 residual gap in the Phase -1A report. The alias map is
derived from proxy-address resolution, and the only org-level mechanism that could have
contradicted it — a mail-flow rule redirecting or blind-copying an address — **[VTF]** does
not exist. **The four-alias topology in §1.1 is now complete and uncontradicted at the
organisation level.** `safeguarding@ → coordinator@` in particular is confirmed by two
independent means.

A clean negative result, and the one that most needed to be clean: the §13 safeguarding
model depends entirely on the alias landing where we think it lands.

### 2.8 ⚠️ Gate T8 — still open; the sign-in app is a *different* identity

**[VTF]** `49dc5530-53c6-4cbc-8c90-8f7686cf02e3` is now fully resolved:

| Property | Value |
| --- | --- |
| DisplayName | **PW Agents (Staging)** |
| Application object id | `f622fb9c-688a-482f-baab-c66c47ab0985` |
| Service principal object id | `b016795b-cbf6-447a-b635-ea564634834d` |
| Sign-in audience | `AzureADMyOrg` (single-tenant) |
| Publisher domain | `projectwest.co.uk` |
| Enabled | True |
| Graph app-role assignments | **zero** |

**[DIC] This closes E3 and informs decision 16, but it does not close T8.** T8 asks which
identity performs **Graph mail reading and sending** — a different registration. **[VTF]
(repo)** the mail path never reads `ENTRA_CLIENT_ID`; it takes `tenant_id` / `client_id` /
`client_secret` from the `mailboxes` row (`graph_client.py:30`, `pipeline.py:126`,
`api.py:158`). `49dc5530…` is the delegated user-sign-in app used by
`services/auth` and the frontend. The two are unrelated at runtime.

> **Correcting myself:** I previously wrote that closing T8 "means decrypting that row —
> out of scope." **That was wrong.** **[VTF] (repo)** only the *secret* is encrypted
> (`client_secret_enc`, `models.py:145`); `tenant_id` and `client_id` are plain `String`
> columns (`models.py:143–144`). The identity is readable without touching any secret.

**[DIC]** One authenticated read closes it — `GET /settings/mailbox` already returns
exactly these fields, and deliberately returns `clientSecretConfigured` as a boolean
rather than the secret (`api.py:400–418`):

```
GET /settings/mailbox  →  { "provider", "username", "tenantId", "clientId",
                            "clientSecretConfigured": true|false, ... }
```

Equivalently, read-only against the database:

```sql
SELECT id, provider, username, tenant_id, client_id,
       (client_secret_enc IS NOT NULL) AS has_secret
FROM mailboxes;
```

**The question T8 answers:** does `clientId` equal `d1687ded-fc1c-4003-a047-f0966bef6087`
(`Project West Mail Sender`), or something else? **[VTF]** That SP holds `Application
Mail.Send` scoped to `referrals@` and no read grant — so if the POC is configured with a
*different* registration, there is a fourth identity in play that no verification so far
has examined. Worth closing precisely because it is cheap and the answer is currently
unknown either way.

### 2.9 ⚠️ Gate T10 — mailbox-level redirection, and the Inbox-only blind spot (new)

**[VTF]** T7's zero transport rules is an *organisation-level* result. Two **mailbox-level**
mechanisms can still move mail, and neither has been queried — `Get-InboxRule`,
`ForwardingSmtpAddress`, `ForwardingAddress` and `DeliverToMailboxAndForward` all return
**0 occurrences** in the live transcript. (The Phase -1A collector requested the forwarding
properties; the session that actually ran used `Get-EXOMailbox` with a narrower property
set, so they were never returned.)

**[VTF] (repo)** This matters more than it would otherwise, because ingestion polls the
**Inbox folder only**:

```
services/apps/mail_agent/app/graph_client.py:104
  /users/{mailbox}/mailFolders/inbox/messages
```

**[DIC] The failure mode is silent and complete.** An inbox rule that files incoming mail
into a subfolder — "move newsletters to Archive", "move supplier mail to Suppliers" — moves
it **out of the agent's view entirely**. Nothing errors. Nothing is flagged. The mail is
simply never seen, and the only symptom is an absence nobody is looking for.

**[DIC]** Two reasons to treat this as likely rather than theoretical: **[VTF]** these are
shared mailboxes worked by nine people, where filing rules are normal practice; and
**[VTF]** `info@` holds 161,833 items, which is not a volume anyone leaves unfiled in a
flat Inbox.

Three read-only commands:

```powershell
# 1. Inbox rules on each shared mailbox — the blind spot
Get-EXOMailbox -RecipientTypeDetails SharedMailbox -ResultSize Unlimited | ForEach-Object {
    Get-InboxRule -Mailbox $_.PrimarySmtpAddress -ErrorAction SilentlyContinue |
        Select-Object @{n='Mailbox';e={$_.MailboxOwnerId}}, Name, Enabled,
                      MoveToFolder, ForwardTo, RedirectTo, DeleteMessage
}

# 2. Mailbox-level forwarding
Get-EXOMailbox -RecipientTypeDetails SharedMailbox -ResultSize Unlimited `
    -Properties ForwardingSmtpAddress,ForwardingAddress,DeliverToMailboxAndForward |
    Select-Object PrimarySmtpAddress, ForwardingSmtpAddress, ForwardingAddress,
                  DeliverToMailboxAndForward

# 3. Folder inventory — how much mail already lives outside the Inbox
Get-EXOMailbox -RecipientTypeDetails SharedMailbox -ResultSize Unlimited | ForEach-Object {
    Get-EXOMailboxFolderStatistics -Identity $_.PrimarySmtpAddress |
        Where-Object ItemsInFolder -gt 0 |
        Select-Object @{n='Mailbox';e={$_.Identity.Split('\')[0]}}, Name, ItemsInFolder
}
```

**[DIC]** Command 3 is the quantitative one, and it also answers a design question the plan
never asks: **should the agent poll the Inbox only, or all folders?** **[VTF]** T6 gives
total item counts per mailbox but not their distribution across folders. If a large share
of `referrals@` or `coordinator@` sits outside the Inbox, Inbox-only polling is
under-collecting the two mailboxes that matter most — and `monitor_from` tuning (§2.6)
would be calibrated against the wrong denominator.

**[PWD] 17** — new: is Inbox-only the intended ingestion scope, or should subfolders be
included? Not answerable by us; it depends on how PW staff actually work the mailboxes.

---

## 3. Remaining PW business decisions

| # | Decision | Status | Blocks |
| --- | --- | --- | --- |
| 1 | Create Entra groups, supply object ids | **Open** — and now for **nine** users, not six | Everything |
| 2 | Confirm the agent's app registration | **Partially answered [VTF]** — `Project West Mail Sender` exists; is it the intended one? | Ingestion |
| 3 | Name the safeguarding leads | **Open** | Safeguarding path; acceptance test 9 |
| 4 | **Ingestion cutover/cutoff boundary per mailbox** | **Open — now quantified [VTF]**: 205,316 items total; `info@` alone is 161,833 (78.8%). Historical ingestion must not be assumed | First sync; **the largest cost decision in the project** |
| 5 | Alias sending behaviour | **Open** — spike T4 | Reply identity |
| 6 | Read and Send: one grant or two? | **Evidence now favours one [VTF]** — granted together in every cell | Group count |
| 7 | Who may approve? | **Open** | `can_approve` |
| 8 | Precedence when mail hits two mailboxes | **Open** | `receiving_address` derivation |
| 9 | Narrow Coordinator access? | **Open — now evidence-backed [VTF]**: nine users + `DevAI` can read safeguarding mail in Outlook | Nothing technical; an operational/compliance choice |
| 10 | Is `admin@` a shared mailbox? | ✅ **RESOLVED [VTF]** — it is an alias of `management@` | — |
| **11** | **`admin@`'s queue and `reply_allowed`** | **New — open** | Alias seed |
| **12** | **Is `admin@`'s three-person visibility intended?** | **New — open** | Operational expectation |
| **13** | **Is `DevAI@`'s standing Full Access on five mailboxes intended?** | **New — open** | Security posture |
| **14** | **Extend the existing SP, or a dedicated ingestion identity?** | **New — open** | Tenant change shape |
| **15** | **Formally supersede Access Map v1 where it conflicts with the tenant** | **New — open** | Governance |
| **16** | **Which sign-in registration is production?** **[VTF]** Entra says "PW Agents (Staging)" (app obj `f622fb9c…`, SP obj `b016795b…`, `AzureADMyOrg`, publisher domain `projectwest.co.uk`); repo says "(Dev)"; redirect URI is the live `project-west-agents.vercel.app` | **New — open** | Credential rotation, environment hygiene. **Not a security finding** — zero app-role assignments either way. **Do not rename or alter the registration without PW's decision** |
| **17** | **Is Inbox-only the intended ingestion scope, or should subfolders be included?** **[VTF]** ingestion polls `mailFolders/inbox` only; an inbox rule filing mail elsewhere makes it invisible with no error | **New — open** | Ingestion completeness; `monitor_from` calibration |

**[PWD] Decision 9 deserves escalation, not a table row.** **[VTF]** All nine named users
hold Full Access *and* Send As on `coordinator@`, and `safeguarding@` is an alias into it.
Nine people plus a service account can read every safeguarding disclosure directly in
Outlook, entirely outside the agent. The §13 design restricts the application; it cannot
restrict Exchange. Only a separate safeguarding mailbox closes this, and that is an
Exchange change on PW's side.

---

## 4. Exact Exchange / Entra changes that would be required

**None of these have been made.** Listed as specification, conditional on the gates above.

### 4.1 Entra — application permissions: **grant nothing**

> ⚠️ **This section reverses my earlier recommendation.** It previously listed "E1 — grant
> `Mail.Read` application permission with admin consent". **Do not do that.**

| Change | Detail |
| --- | --- |
| **E1** | **None.** Grant **no** Graph application permissions to the agent registration |
| E2 | ✅ **[VTF] Verified** — `Project West Mail Sender` holds zero Graph app roles. Keep it that way |
| E3 | ✅ **[VTF] Verified** — `PW Agents (Staging)` (`49dc5530…`) holds zero app-role assignments; delegated only |
| E4 | Create 6 mailbox groups + `PW-Safeguarding-Lead` (or 18+1 if read/send split) — **[PWD]** 1, 6 |

**[DIC]** E2 and E3 are now **standing invariants, not one-off checks.** The entire
scoping guarantee rests on neither registration ever acquiring a Graph application
permission, and nothing in Entra prevents an administrator adding one later — the effect
would be silent, since access would simply widen with no error anywhere. Worth a periodic
assertion:

```powershell
foreach ($id in '49dc5530-53c6-4cbc-8c90-8f7686cf02e3','d1687ded-fc1c-4003-a047-f0966bef6087') {
    $sp = Get-MgServicePrincipal -Filter "appId eq '$id'"
    $n  = (Get-MgServicePrincipalAppRoleAssignment -ServicePrincipalId $sp.Id -All).Count
    "{0,-28} {1} app-role assignment(s) {2}" -f $sp.DisplayName, $n, $(if($n -eq 0){'OK'}else{'INVESTIGATE'})
}
```

This is now confirmed by Microsoft's own documentation, not inferred. The authorization
model is a **union of two independent authorities**:

> *"The assigned permissions are a **union** operation on the permissions from Microsoft
> Entra ID and the permissions assigned in Exchange Online RBAC. Each authority can act
> independently."*
>
> *"If your Service Principal has `Mail.Read` granted in Microsoft Entra ID and you
> configure a resource-scoped `Mail.Read` permission in Application RBAC, **it's important
> that you remove the assignment of `Mail.Read` from Microsoft Entra ID**. Otherwise, the
> union of an unscoped `Mail.Read` grant from Microsoft Entra and a resource-scoped
> `Mail.Read` grant in Application RBAC results in **no effective resource scoping**."*
>
> — [Role Based Access Control for Applications in Exchange Online](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac), FAQ

**[VTF]** Because the two authorities union rather than intersect, a tenant-wide Entra
grant cannot be narrowed by a management scope — it simply wins. Microsoft's summary:
*"Organization-wide permissions should be assigned in Microsoft Entra ID while
resource-scoped permissions should be granted using RBAC for Applications."* Project West
needs the second, and therefore must have none of the first.

**[DIC]** So the entire authorization for ingestion and sending lives in Exchange RBAC
(§4.2), and Entra holds the credential only. Adding `Mail.Read` in Entra "to make
ingestion work" would silently void the management scope and grant read access to all
nine personal mailboxes — a direct breach of capability brief §7.2. **This is the single
most important thing for whoever applies the tenant changes to understand**, because it is
counter-intuitive: the fix for "the app cannot read" is *not* to grant it the permission
to read.

### 4.2 Exchange — RBAC for Applications (the only grant layer)

| Change | Detail |
| --- | --- |
| X1 | Create a management scope admitting **exactly** the six mailboxes |
| X2 | Assign `Application Mail.Read` to the SP, bound to that scope |
| X3 | Assign `Application Mail.Send` to the SP, bound to that scope |
| X4 | Retire or replace `ProjectWest-Mail-Sender-Referrals` + `ProjectWest-Referrals-Mail-Scope` — **[VTF]** scoped to `referrals@` alone, so it covers one of six |

**[DIC] X1's filter must enumerate the six addresses explicitly**, not use a class
predicate. A filter such as `RecipientTypeDetails -eq 'SharedMailbox'` is evaluated
dynamically, so any shared mailbox created later would gain the agent's access silently.
**[VTF]** The existing scope already models this correctly —
`PrimarySmtpAddress -eq 'referrals@projectwest.co.uk'` — so X1 is that pattern widened to
six `-or` clauses, or a group whose membership PW controls deliberately. Follow the
existing template; it is sound.

**[DIC]** Prefer **one** service principal holding both roles against one scope (option A)
over separate read and send identities (option B). B is marginally more least-privilege
but doubles the configuration surface and the credential count for no gain — both roles
bind to the same six mailboxes either way. **[PWD]** 14 is PW's to confirm.

**[DIC]** X4: extend rather than delete. The existing assignment is `Enabled` and
presumably in use by whatever sends today; retire it only after X3 is verified working, on
the same reasoning as the plan's §14 caution about deleting `mail_client.py` too early.

#### 4.2.1 Four documented behaviours that will otherwise cost a day each

**[VTF]** From Microsoft's RBAC-for-Applications documentation. None are obvious, and each
produces a confusing failure:

1. **Permission changes are cached for 30 minutes to 2 hours.** *"Changes to app
   permissions are subject to cache maintenance that varies between 30 minutes and 2 hours
   depending on the app's recent usage... The cache of an active app is kept alive for up
   to 2 hours."* **[DIC]** So after X1–X3 land, ingestion may keep failing for up to two
   hours with no further change needed. Build this into the Phase 0e → 1d handover
   expectation, or someone will "fix" a working configuration. Note also that
   `Test-ServicePrincipalAuthorization` **bypasses the cache** — it shows the *configured*
   state, which is exactly why it can report success while live calls still fail.
2. **Exclusive management scopes do not restrict app access.** **[DIC]** If PW IT reaches
   for an exclusive scope to lock the agent down, it will silently not work. Use an
   ordinary `RecipientScope`, as the existing one already does.
3. **`MemberOfGroup` scope filters ignore nested groups.** *"Nested group members are
   considered out of scope. Only direct group membership results in the member being
   considered in scope."* **[DIC]** This narrows my own §4.2 suggestion of "a group whose
   membership PW controls": viable, but only with the six mailboxes as *direct* members.
   Given there are exactly six and they change rarely, **the explicit six-address filter
   is the better choice** — it has no nesting semantics to get wrong.
4. **`New-ServicePrincipal` takes the IDs from the Enterprise Applications page, not App
   Registrations.** *"Don't use the IDs from the App Registrations page, as it shows
   different values."* **[DIC]** Relevant only if a second SP is ever registered; the
   existing one is already correct — **[VTF]** `ObjectId 30b62c54-…` resolves and its
   assignment evaluates.

### 4.3 Explicitly not required

- **Application Access Policies** — superseded by RBAC for Applications as the target.
  Any found by T3 are legacy, to be retired once X1–X3 are in place, not before.
- **Changes to human mailbox permissions** — the §4 matrix is PW's operational
  configuration. The *only* exception PW may wish to consider is decision 9 (a separate
  safeguarding mailbox) and decision 13 (`DevAI`'s standing access). Neither is required
  by this build.

---

## 5. Proposed minimal implementation sequence

Revises plan §17. The ordering principle is unchanged — **capture first, enforcement
before multi-mailbox, sending and safeguarding last** — but Phase 0 grows, because the
plan assumed a working app registration that is not verified to exist.

### Phase 0 — Verify and unblock (PW + IT; no application code)

**[VTF]** T1 and T2 are now resolved, so Phase 0 is shorter than it was — and contains no
emergency. There is no live exposure to contain.

0a. **[VTF]** T6 and T7 are closed (§2.6, §2.7). Run **T10** — inbox rules, mailbox
    forwarding, and the per-folder item distribution (§2.9); retry **T3** once the AAP
    resolution error is diagnosed; close **T8** with a single `GET /settings/mailbox`
    (§2.8) — no secret is exposed and it takes seconds.
0b. ✅ **Done [VTF]** — both service principals verified to hold zero Graph app roles (E2, E3).
0c. Apply the pending taxonomy and `org_id` migrations — **[SSOT]** everything downstream
    assumes them.
0d. PW answers decisions 1, 2, 11, 14; supplies Entra group object ids.
0e. Make tenant changes X1–X3 (**and explicitly *not* E1** — see §4.1).
0f. **Spike T4** (alias sending), with a real `sendMail` call folded in to confirm the
    RBAC-only Graph path end to end (§2.4b).

> **[DIC] Gate: do not start Phase 1d until 0e is verified two ways** — re-run
> `Get-Recipient -RecipientPreviewFilter` and confirm it returns **exactly six**
> recipients, **and** re-run `Test-ServicePrincipalAuthorization` with a positive case
> (one of the six) *and* a negative case (a personal mailbox such as `arti@`), confirming
> `True` and `False` respectively. Under the RBAC-only model that scope is the *entire*
> authorization boundary — there is no second layer beneath it, so the negative test is
> the one that matters.
>
> **[DIC]** Allow for the 30-minute-to-2-hour permission cache (§4.2.1) between 0e and any
> live verification that is not the test cmdlet.

### Phase 1a — Capture (small, first, low risk)

Extend the Graph `$select` to `conversationId`, `internetMessageId`, `toRecipients`,
`ccRecipients`. Add the five `emails` columns as nullable. Begin populating them.

**[SSOT]** No new Graph permission is required — these are fields on a message the app
already reads. **[DIC]** Valuable independently of everything else: data accumulates while
the rest is built. Depends on T1 resolving to a working read path.

### Phase 1b — Authorization model

Five new tables; the sign-in resolver; `MailboxScope`. Built with **no mailboxes seeded**,
so it is exercisable before it is load-bearing.

### Phase 1c — Enforcement

Apply `mailbox_scope` to every email route. Add the route-coverage test.

**[DIC]** With one seeded mailbox the behaviour is identical to today, which makes this
verifiable in production before it matters.

### Phase 1d — Multi-mailbox ingestion

Seed **6 mailboxes and 4 aliases** (plus the 4 `.onmicrosoft.com` rows, §1.5). Per-mailbox
cursors. Migration steps 3–4.

**[DIC]** Three additions required by T6 (§2.6), all before the first real sync:

- **Per-mailbox `fetch_per_run`**, or a fair-share scheduler — otherwise `info@` starves
  `referrals@`.
- **An enforced per-mailbox first-sync ceiling** that halts and alerts, so a mis-set
  `monitor_from` fails cheaply.
- **Seed `monitor_from` = go-live for all six by default**, and enable any backfill as a
  deliberate, per-mailbox act afterwards. **[DIC]** Bring up `referrals@` and
  `coordinator@` first: **[VTF]** 316 items between them, so the whole multi-mailbox path
  is provable end to end in under a day and at negligible cost, before `info@` is ever
  pointed at the pipeline.

> **[SSOT] The plan's hard rule carries over unchanged, with corrected numbers:** do not
> seed the six mailboxes before 1c is complete. The window between "six mailboxes write to
> one table" and "every read is filtered" is exactly the exposure this design exists to
> prevent — not even briefly, not even in staging with real mail.

### Phase 1e — Sending identity

Derive From from mailbox + alias. `can_send` enforcement. **[DIC]** Retire IMAP / SMTP /
SendGrid only after Graph is confirmed across all **six** mailboxes — the plan's §14
sequencing caution, with the corrected count.

### Phase 1f — Frontend

`GET /mailboxes`, the switcher, mailbox context on emails, removal of the Settings mailbox
form.

### Phase 1g — Safeguarding restriction

Clearance table, second filter clause, scope-aware alerts. Last, because **[PWD]** 3 gates
it and an empty clearance table correctly hides everything — which looks like a bug if it
lands mid-phase.

### What moved, and why

| Change | Reason |
| --- | --- |
| T1 added as 0a, ahead of everything | It may be an active exposure, not a prerequisite |
| Phase 0 now includes creating `Mail.Read` | The plan assumed it existed; **[VTF]** no scoped grant does |
| Scope-membership re-verification gates 1d | A wrong filter admits nine personal mailboxes |
| Seed counts corrected throughout | 6 + 4, not 7 + 3 |

---

## 6. Acceptance-test implications

**[SSOT]** Capability brief demonstration tests 1–14 remain the contractual bar.

### 6.1 The dominant constraint

**[VTF]** The agent has **no read authorization of any kind** — no Exchange RBAC
`Mail.Read` assignment and no Graph application role. **Every acceptance test that requires
ingesting a message is undemonstrable until X1–X2 are applied.** That is most of tests
1–14, not a subset.

**[VTF]** And the single grant that does exist — `Mail.Send` scoped to
`PrimarySmtpAddress -eq 'referrals@projectwest.co.uk'` — covers one of the six mailboxes.
So the *send* side is demonstrable today for `referrals@` only.

**[DIC]** Say both plainly to PW now rather than discovering them at a demonstration. The
remedy is a small, well-understood tenant change (§4.2), not a redesign — but it is PW's
to make, and nothing can be demonstrated end-to-end before it lands.

### 6.2 Test-by-test

| Test | Status | Why |
| --- | --- | --- |
| **7** — `payments@` → Finance queue | **Unblocked by [VTF]**, pending T1 | The alias is confirmed to land in `finance@`; the routing premise is sound |
| **9** — safeguarding restricted alert | **Double-blocked** | **[PWD]** 3 (no leads named → empty clearance → nobody sees restricted items) *and* T1. The plan already flagged the first |
| **13** — staff sends from the correct PW address | **Partially blocked [VTF]** | The send scope is verified as `PrimarySmtpAddress -eq 'referrals@projectwest.co.uk'`. Demonstrable for `referrals@` **only**, until X3 widens it |
| Any test using `admin@` as a distinct mailbox | **Invalid as written** | **[VTF]** `admin@` is an alias. Must be rewritten as an alias test against `management@` |
| Tests asserting seven mailboxes | **Invalid as written** | **[VTF]** six |

### 6.3 New acceptance criteria the corrected topology requires

**[DIC]** Each follows from a verified fact and should be added:

1. Mail to `admin@` is ingested into `management@` with `receiving_address = admin@` and
   `original_recipient` preserved.
2. A user with Management access sees `admin@` mail; a user without it sees nothing —
   **[VTF]** only three people hold `management@`.
3. `complaints@` and `safeguarding@` mail, both resident in `coordinator@`, are
   distinguishable by `receiving_address` and routed differently (the §13 restriction is
   the whole point).
4. Mail arriving at a `.onmicrosoft.com` routing address is ingested without being flagged
   ambiguous (§1.5).
5. The management scope admits exactly six recipients — asserted against live Exchange,
   not against the seed.
6. **[DIC]** First sync of any mailbox halts at its configured ceiling rather than
   draining the mailbox — demonstrable against `info@`, where **[VTF]** the difference is
   161,833 items.
7. **[DIC]** A backlog in one mailbox does not delay ingestion in another — the
   fair-share property, which **[VTF]** the 1,100:1 size ratio between `info@` and
   `coordinator@` makes a real risk rather than a theoretical one.

### 6.4 Plan §15 test suite — still correct, with corrections

**[SSOT]** The nine Priority-1 authorization tests, six Priority-2 safeguarding tests, six
Priority-3 ingestion tests and the Priority-4 migration rehearsal all stand. Two
amendments:

- **[DIC]** `test_reply_identity_derived_from_alias` should cover **four** aliases,
  including `admin@` — whose expected behaviour is **[PWD]** 11 and therefore not yet
  writable.
- **[DIC]** Add `test_admin_alias_resolves_to_management` — the specific error that
  reached the plan via decision 10, now a regression guard.

**[SSOT]** The repository has one test file (`services/auth/tests/test_entra.py`). This is
authorization code and must not follow that precedent.

---

## 7. Summary

**What is now settled [VTF]:** the topology (6 + 4), the permission matrix, the nine-user
set, the service principal's identity, that `admin@` is an alias — resolving decision 10
against the plan — and now the full authorization picture: **Exchange RBAC `Mail.Send`
scoped to `referrals@` alone, and no Graph application roles whatsoever.**

**What the authorization answer means:** there is **no live exposure**. **[VTF]** *Both*
POC service principals — `Project West Mail Sender` and `PW Agents (Staging)` — hold zero
Graph application-role assignments, so the feared case of a tenant-wide grant with nothing
restricting it does not exist anywhere in the POC's identity surface. The tenant is
already on Microsoft's recommended RBAC-for-Applications model. The gap is a missing
grant, not a leaking one, and that is much the better problem to have.

**[DIC]** Keeping it that way is now a standing invariant rather than a closed gate
(§4.1): nothing in Entra prevents an administrator adding an application permission later,
and the effect would be a silent widening of access with no error raised anywhere.

**What is empirically proven [VTF]:** `Test-ServicePrincipalAuthorization` returns
`InScope: True` for `referrals@` and `InScope: False` for `info@`. The scope boundary is
enforced, not merely configured — and the negative case is what demonstrates it.

**The one thing not to get wrong:** the fix is **not** to grant `Mail.Read` in Entra.
Microsoft's documentation is explicit that the two authorities **union**, so an unscoped
Entra grant would produce *"no effective resource scoping"* and expose all nine personal
mailboxes. All authorization belongs in Exchange RBAC (§4.1, §4.2). I had this backwards
earlier in this document and have corrected it in place.

**The thing most likely to waste a day:** permission changes are cached for up to two
hours, and the test cmdlet bypasses that cache (§4.2.1). Configuration can test correct
while live calls still fail. Expect it rather than debug it.

**The largest cost decision [VTF]:** 205,316 items across the six mailboxes, of which
`info@` is 161,833 — the plan sized this problem at 24,007. Historical ingestion is not
to be assumed, and the fail-safe default is cutover everywhere, opted out of per mailbox.
The happy accident is that **[VTF]** `referrals@` and `coordinator@` hold 316 items
between them, so full history for the two mailboxes that carry the actual workflow costs
almost nothing — do that, and start everything else at go-live.

**What is unchanged [SSOT]:** the two-plane architecture, the DB design, the enforcement
model, the safeguarding logic, the build ordering principle, and the §15 test strategy.
The plan's reasoning survives the correction — it is the inputs that were wrong, not the
design.

**Nothing was implemented. No RBAC, Exchange configuration, database, migration or
application code was created or modified. Analysis stops here.**

---

## Appendix — sources for the RBAC-for-Applications model

The §4.1 reversal and the §4.2.1 behaviours are taken from Microsoft's primary
documentation, quoted inline above.

**Primary (authoritative):**

- [Role Based Access Control for Applications in Exchange Online](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac) — Microsoft Learn. Source of the union-of-authorities FAQ, the "same effect as the Graph permissions" statement, the permission cache window, the exclusive-scope and nested-group limitations, and the Enterprise-Applications-ID note.
- [`Test-ServicePrincipalAuthorization`](https://learn.microsoft.com/en-us/powershell/module/exchangepowershell/test-serviceprincipalauthorization?view=exchange-ps) — Microsoft Learn. Confirms the cmdlet simulates RBAC evaluation and excludes Entra-granted permissions.

**Corroborating:**

- [Announcing Public Preview of RBAC for Applications in Exchange Online](https://techcommunity.microsoft.com/blog/exchange/announcing-public-preview-of-role-based-access-control-for-applications-in-excha/3688228) — Microsoft Exchange Team
- [ExO RBAC improvements #1: Limiting application access](https://michev.info/blog/post/4282/exo-rbac-improvements-1-limiting-application-access) — Vasil Michev
- [Secure Access To Some Mailboxes Via Graph](https://c7solutions.com/2024/09/secure-access-to-mailboxes-via-graph)
- [Migrating Exchange Apps to RBAC for Applications](https://mikecrowley.us/2026/07/10/exchange-app-access-policy-rbac-migration/) — Mike Crowley
- [Microsoft Launches Role-Based Access Control for Applications](https://practical365.com/rbac-for-applications/) — Practical 365

**[DIC]** Gate T9 was raised because the model was documentation-derived rather than
tenant-observed. It is now both: Microsoft states RBAC roles have the same effect as Graph
permissions, and **[VTF]** the live tenant evaluates the scope correctly in both the
positive and negative case. The remaining confirmation is a single live `sendMail`, folded
into the Phase 0f spike.
