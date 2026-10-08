# Phase -1 — Final Technical-Gate Summary

**Project West Email Agent — consolidated findings from Phase -1A and Phase -1B**

| | |
| --- | --- |
| Date | 2026-10-06 |
| Tenant | `projectwest27.onmicrosoft.com` / `projectwest.co.uk` — `98b19873-116e-481c-8f5b-fd536f44f02c` |
| Scope | **Summary only.** No code, migrations, Exchange, Entra or database changed. Nothing committed. |
| Sources | [Phase -1A report](PHASE_MINUS_1A_TENANT_VERIFICATION_REPORT.md) · [Phase -1B baseline](PHASE_MINUS_1B_IMPLEMENTATION_BASELINE.md) · `mail_output.txt` · live PowerShell/Graph updates |
| **Overall gate** | ❌ **NOT PASSED** — two verified blockers, five open uncertainties |

### Labels used

| Label | Meaning |
| --- | --- |
| **[VTF]** | Verified tenant fact — observed live in Exchange/Graph |
| **[VRF]** | Verified repository fact — read first-hand from this working tree |
| **[SSOT]** | Binding requirement from the capability brief / Access Map / plan |
| **[PWD]** | Unresolved Project West business decision |
| **[IR]** | Implementation recommendation — ours, and challengeable |

*(**[VRF]** is not in the requested label set, but section 2 requires a tenant/repository
distinction. A statement about our code is never evidence about the tenant.)*

---

## 1. Verified tenant facts

### 1.1 Six physical shared mailboxes

| # | Mailbox | Graph object id | Items | Size |
| --- | --- | --- | ---: | ---: |
| 1 | `info@projectwest.co.uk` | `9609fee8-549a-47b2-a2fc-e2e1c6d77e6d` | 161,833 | 45.13 GB |
| 2 | `reports@projectwest.co.uk` | `70e3685c-3a77-40d5-9d85-e9bb5e1ef4a3` | 24,162 | 7.519 GB |
| 3 | `finance@projectwest.co.uk` | `9c4588ef-287d-4f6f-8d11-5f62d2ae29d1` | 15,337 | 3.249 GB |
| 4 | `management@projectwest.co.uk` | `188f91f4-b916-4fed-8b88-5d30a81f5cde` | 3,668 | 1.174 GB |
| 5 | `referrals@projectwest.co.uk` | `cbe6ae6b-9f76-4b02-84ea-0088f561e62d` | 172 | 8.278 MB |
| 6 | `coordinator@projectwest.co.uk` | `29bad478-47bd-4c04-9143-c511a6b1dfcf` | 144 | 27.21 MB |
| | **Total** | | **205,316** | **~57 GB** |

All six are `RecipientTypeDetails: SharedMailbox`. **[VTF]** `admin@projectwest.co.uk` is
**not** a mailbox.

### 1.2 Four aliases

| Alias | Physical mailbox |
| --- | --- |
| **`admin@`** | **`management@`** |
| `payments@` | `finance@` |
| `complaints@` | `coordinator@` |
| `safeguarding@` | `coordinator@` |

Plus five uncatalogued routing addresses: `referrals6151@`, `coordinator2744@`,
`finance@`, `management@` (all `@ProjectWest27.onmicrosoft.com`), and `SIP:` entries on
`info@` and `reports@`.

### 1.3 Permission matrix — nine named users

| Mailbox | Full Access | Send As |
| --- | --- | --- |
| `info@` | DevAI, arti, pauline, avis, shawana, angela, sarah, praveena | same |
| `referrals@` | DevAI + 7 humans | 7 humans |
| `coordinator@` | DevAI + **all 9 humans** | **all 9 humans** |
| `finance@` | DevAI, arti, pauline, shawana, jeffrey, deshaun | 5 humans |
| `reports@` | DevAI + 7 humans | 7 humans |
| `management@` | **arti, pauline, shawana only** | same |

**[VTF]** Full Access and Send As are granted together in every cell. **[VTF]**
`GrantSendOnBehalfTo` is **empty on all six**. **[VTF]** The nine users: arti, pauline,
shawana, avis, angela, sarah, praveena, jeffrey, deshaun. **[VTF]**
`DevAI@ProjectWest27.onmicrosoft.com` holds Full Access on five of six.

### 1.4 Identity mapping (T8 scope)

| Registration | AppId | Objects | Graph app roles | Role |
| --- | --- | --- | --- | --- |
| **Project West Mail Sender** | `d1687ded-fc1c-4003-a047-f0966bef6087` | SP `30b62c54-4953-44fa-bd95-43ed7d412a1f` | **zero** | Exchange RBAC mail app |
| **PW Agents (Staging)** | `49dc5530-53c6-4cbc-8c90-8f7686cf02e3` | App `f622fb9c-688a-482f-baab-c66c47ab0985`, SP `b016795b-cbf6-447a-b635-ea564634834d` | **zero** | Delegated user sign-in |

**[VTF]** `PW Agents (Staging)`: enabled, `AzureADMyOrg` (single-tenant), publisher domain
`projectwest.co.uk`.

**[VTF] Both service principals hold zero Microsoft Graph application-role assignments.**
There is no tenant-wide Graph grant anywhere in the POC's identity surface.

> ⚠️ **[VRF] T8 is now closed, and the answer is neither.** A read-only `SELECT` against
> the `mailboxes` row (the same three fields `GET /settings/mailbox` returns) shows the
> POC is configured with **a third application identity, in a different tenant, over IMAP
> against a personal Gmail address** — see §4, T8. No Project West Graph identity is in
> use for mail read or send.

### 1.5 Exchange RBAC — scope and allow/deny evidence

**[VTF]** One application role assignment exists:

| Property | Value |
| --- | --- |
| Assignment | `ProjectWest-Mail-Sender-Referrals` |
| Role | **`Application Mail.Send`** |
| Assignee | SP `30b62c54-4953-44fa-bd95-43ed7d412a1f` |
| Method / state | Direct, **Enabled** |
| `CustomResourceScope` | `ProjectWest-Referrals-Mail-Scope` |
| **Recipient filter** | **`PrimarySmtpAddress -eq 'referrals@projectwest.co.uk'`** |
| `RecipientWriteScope` | `CustomRecipientScope` |
| `RecipientReadScope` | `Organization` |

**[VTF]** Empirically confirmed by `Test-ServicePrincipalAuthorization`:

| Case | Resource | `InScope` |
| --- | --- | --- |
| **Allow** | `referrals@projectwest.co.uk` | **True** |
| **Deny** | `info@projectwest.co.uk` | **False** |

The negative case is what makes this evidence: a scope admitting everything would also
have returned True for `referrals@`.

**[VTF]** No `Application Mail.Read` assignment exists for the service principal — in
Exchange RBAC or in Entra. **[VTF]** The 24 `Application *` rows assigned to *Organization
Management* are built-in `-Delegating` assignments present in every tenant; they confer no
access to any application.

### 1.6 Mail flow

**[VTF]** `Get-TransportRule` returns **zero rows** (T7). No org-level rule redirects or
blind-copies any address, so the §1.2 alias map is complete and uncontradicted at the
organisation level — `safeguarding@ → coordinator@` is confirmed by two independent means.

### 1.7 Not verified

**[VTF]** `Get-ApplicationAccessPolicy` **failed** with an Exchange object-resolution
error. Legacy AAP state is **UNVERIFIED, not absent** — a failed query is not a negative
result.

---

## 2. Verified repository facts

### 2.1 The send path

| Fact | Reference |
| --- | --- |
| Graph send function | [`graph_client.py:159`](services/apps/mail_agent/app/graph_client.py#L159) `send_reply()` |
| The call | [`graph_client.py:166`](services/apps/mail_agent/app/graph_client.py#L166) — `POST {GRAPH_BASE}/users/{mailbox}/sendMail` |
| Sending identity | the `{mailbox}` path segment = **`Mailbox.username`** ([`pipeline.py:127`](services/apps/mail_agent/app/pipeline.py#L127), [`api.py:159`](services/apps/mail_agent/app/api.py#L159)) |
| **`from` property** | **absent.** Body carries only `subject`, `body`, `toRecipients`, `saveToSentItems` ([`graph_client.py:168–176`](services/apps/mail_agent/app/graph_client.py#L168-L176)) |
| Caller-supplied sender | impossible — `ReplyIn` has only `body` ([`schemas.py:20–21`](services/apps/mail_agent/app/schemas.py#L20-L21)) |
| Mailbox count | exactly one — `ux_mailboxes_singleton` ([`migrate_drop_org_columns.sql:45`](services/apps/mail_agent/migrate_drop_org_columns.sql#L45)) |
| **Alias representation** | **none anywhere.** Only two prose mentions ([`models.py:121`](services/apps/mail_agent/app/models.py#L121), `migrate_drop_org_columns.sql:44`) describing future work |

### 2.2 Ingestion

**[VRF]** Polls the **Inbox folder only** —
[`graph_client.py:104`](services/apps/mail_agent/app/graph_client.py#L104):
`/users/{mailbox}/mailFolders/inbox/messages`.

**[VRF]** **There is no `monitor_from` mechanism in the code.**
[`models.py:147–150`](services/apps/mail_agent/app/models.py#L147-L150): *"NULL means
'never synced' — the first sync for a mailbox therefore naturally pulls everything,
however large the backlog."*

**[VRF]** `FETCH_PER_RUN=20`, `CRON_INTERVAL_MINUTES=60` (`services/apps/mail_agent/.env`)
= 480 messages/day. **[VRF]** `fetch_per_run` is a **single global value**, not per-mailbox
([`config.py:15`](services/apps/mail_agent/app/config.py#L15),
[`pipeline.py:479`](services/apps/mail_agent/app/pipeline.py#L479)).

### 2.3 Authentication

**[VRF]** MSAL `ConfidentialClientApplication`, `acquire_token_for_client`, scope
`https://graph.microsoft.com/.default`
([`graph_client.py:27,30–39`](services/apps/mail_agent/app/graph_client.py#L30-L39)).

### 2.4 Credentials are database values, not configuration

**[VRF]** `Mailbox.tenant_id`, `Mailbox.client_id`, `Mailbox.client_secret_enc`
([`models.py:143–145`](services/apps/mail_agent/app/models.py#L143-L145)), supplied through
the Settings form and read back at send time
([`pipeline.py:126`](services/apps/mail_agent/app/pipeline.py#L126),
[`api.py:158`](services/apps/mail_agent/app/api.py#L158)).

**[VRF]** Only the secret is encrypted. `tenant_id` and `client_id` are plain `String`
columns, and `GET /settings/mailbox` already returns them with
`clientSecretConfigured` as a boolean
([`api.py:400–418`](services/apps/mail_agent/app/api.py#L400-L418)). **The POC's actual
identity is readable without exposing any secret.**

### 2.5 Plan documents disagree with the tenant

**[VRF]** `MAILBOX_AUTHORIZATION_PLAN.md` states seven mailboxes and three aliases
throughout; §16 decision 10 resolved `admin@` **the wrong way**, choosing Access Map v1
over the September M365 report. **[VRF]** It specifies `ApplicationAccessPolicy` as a
required control in five places (§1 ×2, §16, §17, §18). **[VRF]**
`frontend-mail-agent/.env.local:11` labels `49dc5530…` "PW Agents (Dev)"; Entra says
"(Staging)"; the redirect URI is the live Vercel URL.

**[VRF]** The repository has **one** test file (`services/auth/tests/test_entra.py`).

---

## 3. Remaining PW business decisions

| # | Decision | Blocks |
| --- | --- | --- |
| **1** | **[PWD]** Create Entra groups and supply object ids — for **nine** users, not six | Everything |
| **2** | **[PWD]** Confirm `d1687ded…` is the intended agent registration | Ingestion |
| **3** | **[PWD]** Name the safeguarding leads | Safeguarding path; acceptance test 9 |
| **4** | **[PWD]** **Ingestion cutover/cutoff boundary per mailbox** | First sync; **largest cost decision in the project** |
| 5 | **[PWD]** Alias sending behaviour (spike T4) | Reply identity |
| 6 | **[PWD]** Read and Send: one group or two? **[VTF]** evidence favours one | Group count |
| 7 | **[PWD]** Who may approve? | `can_approve` |
| 8 | **[PWD]** Precedence when mail hits two mailboxes | `receiving_address` |
| 9 | **[PWD]** Narrow Coordinator access? **[VTF]** nine users + DevAI can read safeguarding mail in Outlook | Compliance posture |
| ~~10~~ | ✅ Resolved **[VTF]** — `admin@` is an alias | — |
| 11 | **[PWD]** `admin@`'s queue and `reply_allowed` | Alias seed |
| 12 | **[PWD]** Is `admin@`'s three-person visibility intended? | Operational expectation |
| 13 | **[PWD]** Is `DevAI@`'s standing Full Access intended? | Security posture |
| 14 | **[PWD]** Extend the existing SP, or a dedicated ingestion identity? | Tenant change shape |
| 15 | **[PWD]** Formally supersede Access Map v1 where it conflicts | Governance |
| 16 | **[PWD]** Which sign-in registration is production? **Do not rename without PW's decision** | Credential rotation |
| 17 | **[PWD]** Is Inbox-only the intended ingestion scope? | Ingestion completeness |

**[IR]** Decisions **1, 3, 4** are the critical path. Decision 9 deserves a conversation
rather than a table row: **[VTF]** `safeguarding@` is an alias into `coordinator@` and all
nine users hold Full Access on it, so the §13 application-level restriction cannot prevent
any of them reading safeguarding disclosures directly in Outlook.

---

## 4. Remaining technical uncertainties

| Gate | Status | What is unknown |
| --- | --- | --- |
| **T3** | ⚠️ UNVERIFIED | Legacy Application Access Policies. Query **failed**; not proven absent. Low risk — **[VTF]** an AAP constrains only Entra-granted permissions, and both SPs have none |
| **T4** | Open | Does app-only `sendMail` honour `message.from` for a proxy address in *this* tenant, and does sending as an alias of a scoped mailbox stay in scope? **[VTF]** Untestable until the scope widens — `referrals@` has no business alias |
| **T5** | Open | Is the receiving alias recoverable from Graph? **[VTF]** `complaints@` and `safeguarding@` are indistinguishable once mail sits in `coordinator@`. Bcc / DL / redirect cases may need `internetMessageHeaders`. **The safeguarding restriction depends entirely on this** |
| **T8** | ✅ **CLOSED** | **[VRF]** The POC is configured against **a third application identity in a non-Project-West tenant**, with `provider = imap` and a personal Gmail address as the mailbox. No PW Graph identity is configured for mail. Detail below |
| **T10** | ⚠️ **Raised, not run** | **Mailbox-level redirection and the Inbox-only blind spot.** `Get-InboxRule`, `ForwardingSmtpAddress`, `ForwardingAddress`, `DeliverToMailboxAndForward` all return **0 occurrences** in the transcript — never queried |

### T8 in full — closed, 2026-10-06

**[VRF]** Read-only `SELECT` against `mailboxes` (one row), returning only the fields
`GET /settings/mailbox` exposes. The secret was never read, decrypted or printed — only
its presence was evaluated, in SQL, exactly as `api.py:415` does.

| Field | Value |
| --- | --- |
| `provider` | **`imap`** |
| `username` (mailbox) | **`harshithareddy2711l02@gmail.com`** |
| `tenantId` | `d868ea1b-ba92-4cb5-9a8a-c693515cf1dc` |
| `clientId` | `e18a9374-2b25-4ccd-b950-721c658ebbae` |
| `clientSecretConfigured` | `true` |

**Match against the known identities: neither.**

- Not `d1687ded-fc1c-4003-a047-f0966bef6087` (Project West Mail Sender)
- Not `49dc5530-53c6-4cbc-8c90-8f7686cf02e3` (PW Agents (Staging))

**[VRF]** Three further facts returned by the same read:

1. **The tenant is not Project West.** `d868ea1b-…` ≠ `98b19873-116e-481c-8f5b-fd536f44f02c`.
2. **`provider` is `imap`, not `graph`.** **[VRF]** `pipeline._send_fn_for`
   (`pipeline.py:46–50`) routes a non-`graph` provider to SendGrid or raw SMTP, and
   `pipeline.py:484/488` selects `mail_client.fetch_latest` for reading. **The Graph code
   path is not exercised at all**, so the stored Graph credentials are currently inert.
3. The configured mailbox is a **personal Gmail address**, not a Project West mailbox.

**[VRF]** No further investigation was performed on `e18a9374-…` or `d868ea1b-…`, per
instruction. No secret was exposed.

### T10 in full — the finding, pending the evidence

**[VTF]** T7's zero transport rules is an *organisation-level* result. **[VRF]** ingestion
polls the Inbox folder only. **[IR]** An inbox rule filing incoming mail into a subfolder
therefore removes it from the agent's view **entirely — nothing errors, nothing is
flagged, and the only symptom is an absence nobody is looking for.**

**[IR]** Likely rather than theoretical: **[VTF]** these are shared mailboxes worked by
nine people, where filing rules are normal practice, and `info@` holds 161,833 items —
not a volume anyone leaves unfiled in a flat Inbox. A per-folder item count also answers
**[PWD]** 17 and tells us whether `monitor_from` tuning is being calibrated against the
right denominator.

**Three read-only commands close it** — detailed in
[Phase -1B §2.9](PHASE_MINUS_1B_IMPLEMENTATION_BASELINE.md).

---

## 5. SSOT requirements that must be preserved

**[SSOT]** These survive every correction below. The plan's *inputs* were wrong; its
*reasoning* was not.

### Architecture
- Two planes: app-only unattended ingestion; per-request Entra-group authorization. They
  meet only at the stored email, which carries its mailbox as an immutable property.
- Authorization rules bind to **Entra groups, never individual users**.
- The resolved mailbox set lives in the **database, not the JWT** — revocation must not lag
  by the access-token lifetime.

### Enforcement
- Every email route depends on `mailbox_scope`; the mailbox filter is **unconditional**.
- A mailbox id in a request can only **narrow, never widen**.
- Out-of-scope single-row reads return **404, not 403** — existence must not be confirmed.
- `mailbox_id` and `receiving_address` are **immutable**, enforced by DB trigger.
- `internet_message_id` is **not** unique — the same message legitimately lands in two
  mailboxes.
- Group-claim overage **fails closed loudly**, never to a silent empty set.

### Safeguarding
- Restricted items are **absent** from list, counts, search *and* alerts without clearance
  — not redacted, not placeheld.
- Clearance is a **separate axis** from mailbox access; a Superadmin does not get it
  implicitly.
- The **restricted route wins** whenever either the alias or the classifier fires.

### Identity and data
- The From address is **derived, never chosen**; the frontend sends no sender field.
- **No mailbox configuration in the user-facing UI.**
- **No mailbox credential stored in the database.**
- **[SSOT]** Capability brief §7.2: the agent must not reach named personal mailboxes.
- **[SSOT]** Capability brief demonstration tests 1–14 remain the contractual bar.

### Sequencing
- **[SSOT]** Do not seed the mailboxes before per-request enforcement is complete. The
  window between "many mailboxes write to one table" and "every read is filtered" is
  exactly the exposure this design exists to prevent — not even briefly, not even in
  staging with real mail.

---

## 6. Implementation blockers

| # | Blocker | Type | Clears when |
| --- | --- | --- | --- |
| **B1** | **No `Mail.Read` authorization exists anywhere.** Ingestion cannot run | **[VTF]** | Exchange RBAC `Application Mail.Read` assigned to a six-mailbox scope |
| **B2** | **Send scope is `referrals@` only** — one of six | **[VTF]** | The scope widens (X1/X3) |
| **B3** | No Entra groups exist; no group object ids supplied | **[PWD]** 1 | PW creates them for the **nine** users |
| **B4** | **No `monitor_from` mechanism in the code.** First sync pulls everything | **[VRF]** | Built, with an enforced per-mailbox ceiling |
| **B5** | Taxonomy and `org_id` migrations still unapplied | **[SSOT]** | Applied — everything downstream assumes them |
| **B6** | Safeguarding leads unnamed → empty clearance → nobody sees restricted items | **[PWD]** 3 | PW names at least one |
| **B7** | ✅ **Resolved** — **[VRF]** the POC is configured against a third identity in a non-PW tenant, over IMAP to a personal Gmail. Nothing points at Project West | **[VRF]** | Closed by verification; what to do about it is **[PWD]** |
| **B8** | Inbox-only polling unvalidated against inbox rules | **[VRF]** | T10 runs |

**[IR]** B1 is the critical one and it is **not** fixed by granting `Mail.Read` in Entra.

> **[VTF]** Microsoft's documented model: Entra and Exchange RBAC permissions are a
> **union**, and each acts independently. *"If your Service Principal has `Mail.Read`
> granted in Microsoft Entra ID and you configure a resource-scoped `Mail.Read` permission
> in Application RBAC, it's important that you remove the assignment of `Mail.Read` from
> Microsoft Entra ID. Otherwise... **no effective resource scoping**."*
>
> **Granting the Entra permission would void the management scope and expose all nine
> personal mailboxes.** All authorization belongs in Exchange RBAC. **[VTF]** The current
> zero-app-role posture is correct and must be preserved as a standing invariant — nothing
> in Entra prevents an administrator adding one later, and the effect would be a silent
> widening of access with no error raised anywhere.

---

## 7. Recommended implementation sequence

### Phase 0 — Close and unblock (no application code)

| Step | Action | Type |
| --- | --- | --- |
| 0a | Run **T10** (inbox rules, forwarding, per-folder counts); close **T8** via `GET /settings/mailbox`; retry **T3** | **[IR]** |
| 0b | PW answers decisions **1, 2, 4, 11, 14**; supplies Entra group object ids | **[PWD]** |
| 0c | Apply the pending taxonomy and `org_id` migrations | **[SSOT]** |
| 0d | Create a management scope admitting **exactly the six mailboxes**; assign `Application Mail.Read` **and** `Application Mail.Send` to it. **Grant nothing in Entra** | **[IR]** |
| 0e | Spike **T4**, with a live `sendMail` confirming the RBAC-only Graph path | **[IR]** |

**[IR]** Write the scope filter as **explicit address equality**, as the existing one
already does (`PrimarySmtpAddress -eq '…'`), not a class predicate — a filter such as
`RecipientTypeDetails -eq 'SharedMailbox'` is evaluated dynamically and would silently
absorb any future shared mailbox.

**[VTF] Four documented behaviours that will otherwise cost a day each:** app permission
changes are cached **30 min – 2 h**, and `Test-ServicePrincipalAuthorization` *bypasses*
that cache — configuration can test correct while live calls still fail; **exclusive**
management scopes do not restrict app access; `MemberOfGroup` filters ignore **nested**
groups; `New-ServicePrincipal` needs Enterprise Applications IDs, not App Registrations
IDs.

> **[IR] Gate before Phase 1d:** verify the scope two ways — `Get-Recipient
> -RecipientPreviewFilter` returns **exactly six**, *and* `Test-ServicePrincipalAuthorization`
> passes a positive case **and a negative case against a personal mailbox** such as
> `arti@`. Under RBAC-only that scope is the entire authorization boundary; the negative
> test is the one that matters.

### Phases 1a–1g

| Phase | Work | Notes |
| --- | --- | --- |
| **1a** Capture | Extend Graph `$select`; add the five `emails` columns as nullable; begin populating | **[SSOT]** No new permission needed. Data accumulates while the rest is built |
| **1b** Authorization | Five tables, sign-in resolver, `MailboxScope` — **with no mailboxes seeded** | Exercisable before load-bearing |
| **1c** Enforcement | `mailbox_scope` on every route; route-coverage test | **[SSOT]** Must complete **before** 1d |
| **1d** Multi-mailbox | Seed **6 mailboxes + 4 aliases** (+ the 4 `.onmicrosoft.com` rows); per-mailbox cursors; migration steps 3–4 | See the three additions below |
| **1e** Sending identity | Derive From from mailbox + alias; `can_send` enforcement | **[IR]** Retire IMAP/SMTP/SendGrid only after Graph is proven on all **six** |
| **1f** Frontend | `GET /mailboxes`, switcher, mailbox context, remove the Settings mailbox form | |
| **1g** Safeguarding | Clearance table, second filter clause, scope-aware alerts | Last — **[PWD]** 3 gates it |

### Three additions required in 1d by the T6 volumetrics

**[IR] 1. Per-mailbox fetch budget.** **[VRF]** `fetch_per_run` is one global value. Under
six mailboxes, **[VTF]** `info@` (78.8% of the corpus) would starve `referrals@` (0.08%) —
the mailbox the business actually runs on. The plan's promise of an independent cursor per
mailbox must extend to the **budget**.

**[IR] 2. An enforced first-sync ceiling** that halts and alerts. **[VTF]** 205,316
reachable items against a single seeded date means a typo is a six-figure LLM run. A
mis-set `monitor_from` should fail loudly and cheaply.

**[IR] 3. Default every mailbox to cutover** (`monitor_from` = go-live), and treat any
backfill as a deliberate per-mailbox act. **[VTF]** The distribution makes this nearly
free where it matters: `referrals@` + `coordinator@` hold **316 items between them**, so
full history there costs under a million tokens — do that, and start `info@` (161,833) and
`reports@` (24,162) at go-live. At the current 480 messages/day a full drain would take
**~428 days**.

**[IR] Bring up `referrals@` and `coordinator@` first.** The entire multi-mailbox path is
provable end to end in under a day at negligible cost, before `info@` is ever pointed at
the pipeline.

### Document corrections (not made — analysis only)

**[IR]** `MAILBOX_AUTHORIZATION_PLAN.md`: seven mailboxes → six; three aliases → four; six
users → nine; replace `ApplicationAccessPolicy` with RBAC for Applications in all five
places; mark §16 decision 10 resolved against its own conclusion. **[IR]** Correct
`frontend-mail-agent/.env.local:11` to say "Staging" — **[PWD]** and leave the Entra
registration itself alone pending decision 16.

### Acceptance-test implications

**[VTF]** Every test requiring ingestion is undemonstrable until B1 clears — most of tests
1–14, not a subset. **[VTF]** The send side is demonstrable today for `referrals@` only.
**[SSOT]** Test 9 (safeguarding) is double-blocked, additionally by **[PWD]** 3. Any test
treating `admin@` as a distinct mailbox is **invalid as written**.

---

**Nothing was implemented. No code, migrations, Exchange, Entra or database configuration
was created or modified.**
