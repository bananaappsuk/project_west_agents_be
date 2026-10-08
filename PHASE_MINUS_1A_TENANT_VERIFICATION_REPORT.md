# Phase -1A — Tenant Verification Report

**Project West Email Agent — live Microsoft 365 / Exchange verification**

| | |
| --- | --- |
| Date | 2026-10-06 |
| Scope | Verification only. No application code, schema, migration, or configuration changed. Nothing committed. No RBAC created or modified. |
| Tenant | `projectwest27.onmicrosoft.com` / `projectwest.co.uk` — **`98b19873-116e-481c-8f5b-fd536f44f02c`** |
| Authenticated as | `DevAI@ProjectWest27.onmicrosoft.com` (interactive, delegated) |
| Evidence | `backend/mail_output.txt` (902 lines, live EXO v3 transcript) |
| **Tenant-configuration gate** | ❌ **FAIL** — with one item still UNVERIFIED (§7.3) |

---

## 0. Headline

Live verification ran. The mailbox, alias, and permission dimensions are now
`VERIFIED (live)`. **Two findings break the plan as written:**

1. **There are six physical shared mailboxes, not seven.** `admin@` is an alias of
   `management@`. The SSOT's seven-mailbox premise is wrong (§3).
2. **The service principal has `Mail.Send` only — no `Mail.Read` anywhere in the
   tenant.** Ingestion, the entire left-hand plane of the architecture, has no Exchange
   grant behind it (§7.2).

The gate is **FAIL**, not UNVERIFIED: these are verified facts about the live tenant, and
the tenant as configured cannot run the system as designed. One item — the resource scope
bound to the existing assignment — remains genuinely unverified because the query that
would have shown it asked for a property that does not exist on the output object (§7.3).
I have not treated that blank as evidence of an empty scope.

Nothing was created or modified, per your instruction.

---

## 1. Evidence standard

| Tag | Meaning |
| --- | --- |
| `VERIFIED (live)` | Observed in the Exchange Online PowerShell transcript. |
| `VERIFIED (repo)` | Established from files in this working tree — a statement about our code, never the tenant. |
| `NOT FOUND` | Queried live and confirmed absent. |
| `UNABLE TO VERIFY` | Not establishable from the transcript, with the reason stated. |

Per requirement 8, raw facts were collected first; comparison against the SSOT is
confined to §8. No business requirement is inferred from tenant configuration.

**Tenant identity resolved.** `VERIFIED (live)` — tenant `98b19873…` *is* Project West.
This closes the open question from the previous report, where the "PW Agents (Dev)" label
and a stray `@ittalenthub.co.uk` test default in `graph_mail_test.py` made the target
ambiguous. The sign-in registration and the mail service principal are in this same
tenant. The `ittalenthub` reference is an unrelated leftover test value.

---

## 2. Mailbox inventory — requirement 1

`VERIFIED (live)` — `Get-EXOMailbox -RecipientTypeDetails SharedMailbox`

| # | Mailbox | Display name | `ExternalDirectoryObjectId` | Type |
| --- | --- | --- | --- | --- |
| 1 | `info@projectwest.co.uk` | Project West | `9609fee8-549a-47b2-a2fc-e2e1c6d77e6d` | SharedMailbox |
| 2 | `referrals@projectwest.co.uk` | referrals | `cbe6ae6b-9f76-4b02-84ea-0088f561e62d` | SharedMailbox |
| 3 | `coordinator@projectwest.co.uk` | coordinator | `29bad478-47bd-4c04-9143-c511a6b1dfcf` | SharedMailbox |
| 4 | `finance@projectwest.co.uk` | finance | `9c4588ef-287d-4f6f-8d11-5f62d2ae29d1` | SharedMailbox |
| 5 | `reports@projectwest.co.uk` | reports | `70e3685c-3a77-40d5-9d85-e9bb5e1ef4a3` | SharedMailbox |
| 6 | `management@projectwest.co.uk` | management | `188f91f4-b916-4fed-8b88-5d30a81f5cde` | SharedMailbox |

**Six, not seven.** `admin@projectwest.co.uk` is `NOT FOUND` as a mailbox — it resolves to
the `management@` object (same `ExternalDirectoryObjectId`, display name "management").

---

## 3. Alias → physical mailbox mapping — requirement 2

`VERIFIED (live)` — proxy-address enumeration plus `Get-EXORecipient` resolution of each
business entry point.

### 3.1 Business entry point → physical mailbox

| Entry point | Resolves to | Kind |
| --- | --- | --- |
| `info@` | `info@` | **primary SMTP** |
| `referrals@` | `referrals@` | **primary SMTP** |
| `coordinator@` | `coordinator@` | **primary SMTP** |
| `finance@` | `finance@` | **primary SMTP** |
| `reports@` | `reports@` | **primary SMTP** |
| `management@` | `management@` | **primary SMTP** |
| **`admin@`** | **`management@`** | **alias** ⚠️ |
| `payments@` | `finance@` | alias |
| `complaints@` | `coordinator@` | alias |
| `safeguarding@` | `coordinator@` | alias |

**Four aliases, not three.** The SSOT expected three; `admin@` is a fourth.

### 3.2 Per-mailbox proxy addresses

| Mailbox | Primary (`SMTP:`) | Aliases (`smtp:`) | Other |
| --- | --- | --- | --- |
| `info@` | `info@projectwest.co.uk` | — | `SIP:info@` |
| `referrals@` | `referrals@projectwest.co.uk` | `referrals6151@ProjectWest27.onmicrosoft.com` | — |
| `coordinator@` | `coordinator@projectwest.co.uk` | **`complaints@`**, **`safeguarding@`**, `coordinator2744@ProjectWest27.onmicrosoft.com` | — |
| `finance@` | `finance@projectwest.co.uk` | **`payments@`**, `finance@ProjectWest27.onmicrosoft.com` | — |
| `reports@` | `reports@projectwest.co.uk` | — | `SIP:reports@` |
| `management@` | `management@projectwest.co.uk` | **`admin@`**, `management@ProjectWest27.onmicrosoft.com` | — |

All three SSOT aliases land exactly where the Access Map said. `safeguarding@` →
`coordinator@` is confirmed, which means the §13 restriction model rests on a correct
premise — see §9.2 for what that premise costs.

**Five `.onmicrosoft.com` routing addresses** are present and absent from the SSOT. They
are tenant-default addresses, not a misconfiguration, but they *can* receive external
mail. Under the plan's §6 derivation they would not match any known address, so such mail
falls to the "no match" branch, is stamped with the polled mailbox and flagged. That is
the correct failure direction, but the seed should catalogue them rather than let every
such message land in the ambiguous bucket.

### 3.3 Residual gap — **closed**

`VERIFIED (live)` — `Get-TransportRule` returns **zero rows**. No mail-flow rule exists
that could redirect or blind-copy any of these addresses, so the alias map above is
complete and uncontradicted at the organisation level. `safeguarding@ → coordinator@` is
confirmed by two independent means, which matters because the §13 safeguarding model
depends on it.

⚠️ **Still open at the *mailbox* level.** Transport rules are org-wide; per-mailbox
redirection is a different mechanism and was never queried — `Get-InboxRule`,
`ForwardingSmtpAddress`, `ForwardingAddress` and `DeliverToMailboxAndForward` all return
0 occurrences in the transcript. This matters because ingestion polls
`mailFolders/inbox` **only** (`graph_client.py:104`), so an inbox rule filing mail into a
subfolder would hide it from the agent with no error raised. Tracked as gate T10 in the
Phase -1B baseline.

`UNABLE TO VERIFY` — only shared mailboxes were enumerated. User mailboxes were not, so
"no unexpected mailbox exists" (requirement 3) is confirmed **for shared mailboxes only**.

---

## 4. Permission matrix — requirement 4

`VERIFIED (live)` — `Get-MailboxPermission` (non-inherited FullAccess),
`Get-RecipientPermission` (SendAs), `GrantSendOnBehalfTo`. `NT AUTHORITY\SELF` omitted.

| Mailbox | Full Access | Send As | Send on Behalf |
| --- | --- | --- | --- |
| `info@` | DevAI, arti, pauline, avis, shawana, angela, sarah, praveena | DevAI, arti, pauline, avis, shawana, angela, sarah, praveena | — |
| `referrals@` | DevAI, arti, pauline, avis, shawana, angela, sarah, praveena | arti, pauline, avis, shawana, angela, sarah, praveena | — |
| `coordinator@` | DevAI, arti, pauline, avis, shawana, **jeffrey**, **deshaun**, angela, sarah, praveena | arti, pauline, avis, shawana, **jeffrey**, **deshaun**, angela, sarah, praveena | — |
| `finance@` | DevAI, arti, pauline, shawana, jeffrey, deshaun | arti, pauline, shawana, jeffrey, deshaun | — |
| `reports@` | DevAI, arti, pauline, avis, shawana, deshaun, angela, praveena | arti, pauline, avis, shawana, deshaun, angela, praveena | — |
| `management@` | **arti, pauline, shawana only** | arti, pauline, shawana | — |

**Send on Behalf is empty on all six mailboxes** — `NOT FOUND`. The plan models it as a
distinct axis; the tenant does not use it.

**Full Access and Send As are granted together in every cell**, exactly as plan §4
predicted. That validates starting with one Entra group per mailbox rather than splitting
read and send (decision 6).

### 4.1 Nine named users, not six

`VERIFIED (live)`: **arti, pauline, shawana, avis, angela, sarah, praveena, jeffrey,
deshaun**. The plan repeatedly says "the six named users in the Access Map". The tenant
has nine. The Access Map is stale, and the Entra group seeding in §7 of the plan must be
built from the live set.

### 4.2 `management@` is the narrow one

Only three people (arti, pauline, shawana) hold it — and because `admin@` is an alias
onto it, **`admin@` mail is visible to three people, not to everyone with "admin"
duties**. The plan treats `admin@` as its own mailbox with its own access group; in
reality it inherits Management's much tighter permissions. Any user expecting to work
`admin@` correspondence who is not one of those three will see nothing.

### 4.3 The `DevAI` admin account holds standing access

`DevAI@ProjectWest27.onmicrosoft.com` has Full Access on five of six mailboxes
(all but `management@`) and Send As on `info@`. This is a service/admin account with
permanent read access to live correspondence, including everything arriving at
`safeguarding@`. It is not in the Access Map. See §9.4.

---

## 5. Service principal — requirement 5

`VERIFIED (live)` — `Get-ServicePrincipal` returns exactly one:

| Property | Value |
| --- | --- |
| DisplayName | **Project West Mail Sender** |
| AppId | `d1687ded-fc1c-4003-a047-f0966bef6087` |
| ObjectId | `30b62c54-4953-44fa-bd95-43ed7d412a1f` |

This is **a different registration** from the sign-in app
(`49dc5530-53c6-4cbc-8c90-8f7686cf02e3`, "PW Agents (Dev)") found in `services/auth/.env`
and `frontend-mail-agent/.env.local`. Both live in this tenant. The separation is correct
and resolves Finding B of the previous report: the sign-in registration is *not* the mail
registration.

> `UNABLE TO VERIFY` — **whether the POC actually uses this service principal.**
> `VERIFIED (repo)`: [graph_client.py:30](services/apps/mail_agent/app/graph_client.py#L30)
> takes `tenant_id` / `client_id` / `client_secret` as per-call arguments sourced from the
> request body ([api.py:479](services/apps/mail_agent/app/api.py#L479)) and stored
> encrypted on the `mailboxes` row. The identity in use is a database value, not
> configuration. Confirming it means decrypting that row, which is outside a
> verification-only brief. Treat `d1687ded…` as the *intended* registration pending PW
> confirmation.

---

## 6. Exchange RBAC for Applications — requirement 5

`VERIFIED (live)` — one application role assignment names the service principal:

| Assignment | Role | Assignee |
| --- | --- | --- |
| `ProjectWest-Mail-Sender-Referrals` | **Application Mail.Send** | `30b62c54-4953-44fa-bd95-43…` (Project West Mail Sender) |

One management scope exists:

| Scope | Restriction type |
| --- | --- |
| `ProjectWest-Referrals-Mail-Scope` | `RecipientScope` |

### 6.1 Do not misread the 24 "Organization Management" rows

`VERIFIED (live)`. The transcript also lists 24 assignments such as
`Application Exchange Full Access-Organization Management-Delegating` and
`Application Mail.Read-Organization Management-Delegating`.

> **These are built-in `-Delegating` assignments that ship with every Microsoft 365
> tenant.** Their assignee is the *Organization Management* role group, not any
> application, and a delegating assignment confers no access — it only permits an
> administrator to assign that role onward. **They are not a grant of Exchange Full
> Access to anything**, and nobody should read the list as evidence that the app is
> over-permissioned. The only assignment that touches the service principal is the single
> row in the table above.

---

## 7. The three gaps in the RBAC configuration

### 7.1 `Mail.Read` does not exist anywhere — ingestion has no grant ❌

`VERIFIED (live)`. The service principal holds **`Application Mail.Send` and nothing
else**. There is no `Application Mail.Read` assignment to it, scoped or unscoped.

The architecture's ingestion plane — polling six mailboxes on a schedule — requires
`Mail.Read`. As configured, **the application cannot read a single message from any
mailbox in this tenant.** This is the single largest gap, and the name of the existing
assignment ("Mail-Sender") suggests the tenant was configured for a send-only trial
rather than for the agent.

### 7.2 The assignment appears scoped to Referrals only ⚠️

Both names — `ProjectWest-Mail-Sender-Referrals` and `ProjectWest-Referrals-Mail-Scope` —
say *Referrals*. If that reflects the scope's contents, the app can send as `referrals@`
and no other mailbox. This matches your instruction not to assume tenant-wide coverage.
Naming is a strong hint, not proof; §7.3 is how it gets proved.

### 7.3 The scope filter was not actually captured — property-name artifact ⚠️

`UNABLE TO VERIFY`. The transcript shows:

```
Name                       : ProjectWest-Referrals-Mail-Scope
ScopeRestrictionType       : RecipientScope
RecipientRoot              :
RecipientRestrictionFilter :
```

**Both blanks are almost certainly an artifact of the query, not an empty scope.**
`RecipientRestrictionFilter` is the *parameter* name on `New-ManagementScope` /
`Set-ManagementScope`; the *output object* exposes the filter as **`RecipientFilter`**.
`Select-Object` returns an empty column for a property that does not exist rather than
erroring, so the blank is what you would see either way.

The corroborating argument: a scope of type `RecipientScope` **cannot be created with
neither a recipient root nor a filter** — `New-ManagementScope` requires one. A genuinely
empty scope of this type should not exist.

> I am deliberately not concluding "the scope is unrestricted." That reading would be
> alarming and is unsupported — this is a measurement error until re-measured. Equally,
> do not conclude it is safely scoped. It is unknown.

Two read-only commands close this, and the same gap on the assignment's binding:

```powershell
Get-ManagementScope "ProjectWest-Referrals-Mail-Scope" | Format-List *
Get-ManagementRoleAssignment "ProjectWest-Mail-Sender-Referrals" | Format-List *
Get-Recipient -RecipientPreviewFilter (Get-ManagementScope "ProjectWest-Referrals-Mail-Scope").RecipientFilter
```

The third line is the decisive one: it enumerates **exactly which mailboxes the scope
admits**, which is requirement 5's real question and the only way to confirm that named
personal mailboxes are excluded.

The assignment's `CustomResourceScope` was selected in the transcript but **truncated out
of the `Format-Table` output**, so it is also unconfirmed that the assignment is bound to
that scope at all. `Format-List` fixes it.

### 7.4 Application Access Policies — requirement 6

`UNABLE TO VERIFY`. `Get-ApplicationAccessPolicy` was never run. Whether legacy policies
exist is unknown. One command:

```powershell
Get-ApplicationAccessPolicy | Format-List
```

Per your instruction, any result is **legacy / current tenant state**, reported separately
and never as the recommended implementation. The target remains RBAC for Applications.

---

## 8. SSOT deviations

| # | SSOT says | Tenant says | Severity |
| --- | --- | --- | --- |
| 1 | Seven shared mailboxes | **Six.** `admin@` is an alias of `management@` | **High — invalidates the seed** |
| 2 | Three aliases | **Four** (`admin@`, `payments@`, `complaints@`, `safeguarding@`) | **High** |
| 3 | Six named users in the Access Map | **Nine** | Medium |
| 4 | Send on Behalf is a modelled axis | **Unused — empty on all six** | Low (simplifies) |
| 5 | Full Access and Send As granted together | **Confirmed** | — (validates decision 6) |
| 6 | `payments@`→`finance@`, `complaints@`/`safeguarding@`→`coordinator@` | **Confirmed exactly** | — |
| 7 | (silent) | Five `.onmicrosoft.com` addresses uncatalogued | Low |

### 8.1 Decision 10 is resolved — against the plan

Plan §16 decision 10 asked whether `admin@` is a shared mailbox, noting *"Access Map v1
says yes; the September M365 report said alias onto `management@`. v1 taken as
authoritative."*

**The September report was right and Access Map v1 is wrong.** `VERIFIED (live)`.

The plan picked the wrong source, and because it was documented as a decision rather than
an assumption, the error would have reached the migration. Consequences:

- `mailboxes` seeds **6 rows**, not 7.
- `mailbox_aliases` seeds **4 rows**, not 3 — `admin@ → management@`, queue to be
  decided by PW.
- The Entra group model drops from 21 groups to **18** (6 × 3), or **12** if read/send
  stay merged as §4 suggests.
- Anything keyed to "seven mailboxes" in §§1, 7, 17 and 18 needs correcting, including the
  Definition of Done checklist.
- **Access Map v1 should no longer be treated as authoritative** where it conflicts with
  the tenant. That is the broader lesson: it is also stale on user count (deviation 3).

---

## 9. Security concerns

### 9.1 Ingestion has no grant; the send grant is probably one mailbox wide

§7.1 and §7.2 together mean the tenant does not currently support the designed system in
either direction. Not a vulnerability — a shortfall — but it is the critical path.

### 9.2 Nine people can read safeguarding mail in Outlook — now quantified

`VERIFIED (live)`. `safeguarding@` is an alias into `coordinator@`, and **all nine named
users hold Full Access and Send As on `coordinator@`**. Plan §13 predicted this
structurally; the tenant confirms it exactly.

> The application-level restriction in §13 is sound and worth building, but it restricts
> the application only. **Nine people — plus the `DevAI` admin account — can open
> `coordinator@` in Outlook and read every safeguarding disclosure, outside the agent
> entirely.** No application change closes this. Only a separate safeguarding mailbox
> does.

This is plan decision 9. It is now backed by live evidence and names, and for a
safeguarding obligation it should be put to PW explicitly rather than left in a decision
table — people may believe the design closes a gap it cannot reach.

### 9.3 `admin@` inherits Management's permissions, which is tighter than anyone expects

Mail sent to `admin@` is readable by three people. If PW's mental model is "admin is the
general administrative inbox", the live configuration contradicts it. Worth confirming
this is intended before seeding a queue for it (§4.2).

### 9.4 A standing admin account holds Full Access to five mailboxes

`DevAI@ProjectWest27.onmicrosoft.com` can read `info@`, `referrals@`, `coordinator@`,
`finance@` and `reports@` — including all safeguarding correspondence — and send as
`info@`. It is outside the Access Map and appears to be a setup artefact. PW should
confirm it is intended; if it was for provisioning, its mailbox permissions should be
removed.

### 9.5 A loose scope filter would admit future mailboxes silently

Once §7.3 reveals the filter, check its shape. A filter written on a property such as
`RecipientTypeDetails -eq 'SharedMailbox'` is evaluated **dynamically**, so any shared
mailbox created later gains the agent's access with no action and no alert. Enumerate the
six addresses explicitly, or target a group whose membership PW controls deliberately.

### 9.6 App-only access still bypasses every permission in §4

`VERIFIED (repo)` — plan §1. The matrix in §4 governs humans in Outlook. An app-only token
ignores it: nothing in Microsoft 365 stops the application sending as `finance@` for a user
with no Finance rights. The application's own enforcement remains the only defence, and
the §4 data is the *model* to enforce, not a control that enforces itself.

### 9.7 POC credentials are still admin-supplied

`VERIFIED (repo)` — §5. Until the move to one registration in deployment config, any
admin can point the agent at any tenant via the Settings form.

---

## 10. Unresolved Graph runtime questions

Unchanged by this pass — none are answerable from tenant configuration, and §6's
verification does not close them. Question 1 is now sharper, because the tenant shows a
`Mail.Send` assignment whose scope may be a single mailbox.

1. **Does app-only `Mail.Send` honour a custom `message.from` for an alias?** Decision 5.
   Decides whether replies can go out as `payments@` / `complaints@`, or whether the SP
   needs explicit Send As per alias. Now also: does sending as an *alias* of a scoped
   mailbox stay inside the scope?
2. **Is the receiving alias recoverable from a Graph message?** Plan §6 intersects
   `toRecipients` + `ccRecipients` with known addresses. For Bcc, distribution-list
   delivery, or transport-rule redirect the alias may be absent from both, recoverable
   only via `internetMessageHeaders`. **The entire `safeguarding@` restriction depends on
   this** — `safeguarding@` and `complaints@` are indistinguishable once the mail is
   sitting in `coordinator@`.
3. **Does `GET /users/{shared-mailbox}` resolve reliably app-only?** Shared mailboxes are
   unlicensed with sign-in blocked. The six `ExternalDirectoryObjectId` values in §2 are
   the Graph object ids and can seed `mailboxes.graph_user_id` directly — confirm whether
   primary SMTP also works as the key.
4. **Does Graph expose Exchange mailbox delegation?** Plan §1's *"load-bearing
   assumption"* is that it does not. Note the §4 matrix was only obtainable via
   PowerShell, which is consistent with the assumption holding.
5. **Graph throttling across six mailboxes on one registration** — untested; most
   consequential during the first sync of `reports@`.
6. **`monitor_from` per mailbox (decision 4)** — `UNABLE TO VERIFY`;
   `Get-MailboxStatistics` was not run, so the 24,007-item figure for `reports@` is still
   unconfirmed. One command: `Get-EXOMailboxStatistics` over the six.

---

## 11. Gate verdict

| Dimension | Verdict |
| --- | --- |
| Mailbox inventory | ✅ **PASS** (six mailboxes, all SharedMailbox) |
| Alias mapping | ✅ **PASS** (all four resolved; three match SSOT exactly) |
| Permission matrix | ✅ **PASS** (fully enumerated) |
| SSOT alignment | ❌ **FAIL** — seven-mailbox premise wrong; Access Map v1 stale |
| Service principal | ⚠️ exists, but POC's actual identity unconfirmed |
| RBAC — `Mail.Read` | ❌ **FAIL** — no grant exists; ingestion impossible |
| RBAC — scope contents | ⚠️ **UNVERIFIED** (§7.3) |
| Application Access Policies | ⚠️ **UNVERIFIED** — not queried |
| **Overall tenant-configuration gate** | ❌ **FAIL** |

### To clear the gate

**Verification (read-only, ~4 commands — §7.3, §7.4, §10.6):**

1. `Get-ManagementScope … | Format-List *` and the `RecipientPreviewFilter` expansion —
   the exact mailboxes the scope admits.
2. `Get-ManagementRoleAssignment … | Format-List *` — confirm the scope binding.
3. `Get-ApplicationAccessPolicy` — legacy state, reported separately.
4. `Get-EXOMailboxStatistics` over the six — settles decision 4.
5. `Get-TransportRule` — closes the §3.3 redirect gap.

**PW decisions, now evidence-backed:**

6. Confirm `d1687ded-fc1c-4003-a047-f0966bef6087` is the intended agent registration
   (decision 2).
7. Decide `admin@`'s queue, given it lands in `management@` and is visible to three
   people (§4.2, §9.3).
8. Confirm the nine-user set and supply Entra group object ids (decision 1) — the Access
   Map's six is stale.
9. Name the safeguarding leads (decision 3), and take decision 9 — the Outlook gap — as a
   conversation, not a table row (§9.2).
10. Confirm whether `DevAI`'s standing Full Access is intended (§9.4).

**Document corrections (not made — verification-only task):**

11. `MAILBOX_AUTHORIZATION_PLAN.md`: seven mailboxes → six, three aliases → four, six
    users → nine, and replace `ApplicationAccessPolicy` with RBAC for Applications in all
    five places (§1 ×2, §16, §17, §18).
12. Mark Access Map v1 as superseded by the tenant where they conflict.

**Tenant changes required before build — not made, per your instruction:**

13. Grant `Application Mail.Read` to the service principal, scoped to the six mailboxes.
14. Extend the send scope beyond Referrals, once §7.3 confirms its current contents.

**Phase -1A remains incomplete. Nothing was implemented. No RBAC was created or modified.**
