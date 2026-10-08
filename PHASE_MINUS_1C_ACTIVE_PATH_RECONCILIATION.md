# Phase -1C — Active Mail Path Reconciliation

**Project West Email Agent — the POC's live IMAP/SMTP path vs the SSOT**

| | |
| --- | --- |
| Date | 2026-10-06 |
| Scope | **Read-only trace and comparison.** No code, database, migrations, config, Exchange, Entra or Azure changed. No services started. No mail fetched or sent. |
| Trigger | T8 closed: the active path is **not** Microsoft Graph |

**Labels:** **[VTF]** verified tenant fact · **[VRF]** verified repository fact ·
**[SSOT]** binding requirement · **[PWD]** PW decision

---

## 1. The active path, traced

### 1.1 Provider selection

**[VRF]** One `Mailbox` row, enforced by `ux_mailboxes_singleton`
(`migrate_drop_org_columns.sql:45`), loaded by `select(Mailbox)` scalar
(`pipeline.py:120`, `api.py:152`). Its `provider` column is **`imap`**.

**Read dispatch** — `pipeline.py:481`:

```
provider == "graph"  →  graph_client.fetch_latest     (NOT taken)
else                 →  mail_client.fetch_latest      ← active
```

**Send dispatch** — `pipeline._send_fn_for()`, `pipeline.py:38–50`:

```
provider == "graph"          →  graph_client.send_reply    (NOT taken)
elif settings.sendgrid_api_key →  sendgrid_client.send_reply (NOT taken — see below)
else                         →  mail_client.send_reply     ← active (raw SMTP)
```

**[VRF] `SENDGRID_API_KEY` is not set** in `services/apps/mail_agent/.env` (presence
checked; no value read). **The active send path is therefore raw SMTP via
`mail_client.send_reply`, not SendGrid.**

### 1.2 IMAP fetch

**[VRF]** `mail_client.fetch_latest` (`mail_client.py:95`):

1. `imaplib.IMAP4_SSL(imap_host, imap_port, timeout=20)` — line 121
2. `conn.login(username, password)` — line 123 — **Basic Auth**
3. `conn.select("INBOX")` — line 124 — **Inbox only**
4. `conn.uid("search", None, criteria)` where `criteria` is
   `UID {since_uid+1}:*`, or **`"ALL"` when `since_uid` is unset** — lines 130–131
5. `conn.uid("fetch", uid, "(RFC822)")` — line 146 — **the complete raw message**
6. Per message, builds: `uid`, `from`, `fromEmail`, `subject`, `body`, `contentType`,
   `receivedAt`, `attachments` — lines 158–167

### 1.3 SMTP send

**[VRF]** `mail_client.send_reply` (`mail_client.py:190–204`): builds an `EmailMessage`
with `msg["From"] = username`, then `SMTP_SSL` if `smtp_port == 465`, else `SMTP` +
`starttls()`, then `server.login(username, password)` and `send_message`.

### 1.4 Pipeline invocation

**[VRF]** `fetch_and_process` → `mail_client.fetch_latest` via `run_in_threadpool`
(`pipeline.py:487`) → dedup against `known_uids` → insert as `skipped` →
`analyze_and_persist` (`pipeline.py:175`) → LLM → `maybe_send` (`pipeline.py:231`), which
sends **only** when `auto_reply_enabled AND auto_sendable AND needs_reply`; any exception
falls back to a draft (`pipeline.py:243–245`). Manual path: `POST /emails/{id}/reply`
(`api.py:150`).

---

## 2. Configuration fields that control the active path

**[VRF]** No passwords, secrets or decrypted values were read or printed.

| Controls | Field | Location |
| --- | --- | --- |
| IMAP host | `Mailbox.imap_host` | `models.py:130` |
| IMAP port | `Mailbox.imap_port` (default 993) | `models.py:131` |
| IMAP username | `Mailbox.username` | `models.py:134` |
| IMAP authentication | `Mailbox.password_enc` → `crypto.decrypt` → `conn.login()` | `models.py:135`, `mail_client.py:123` |
| Provider selection | `Mailbox.provider` + `settings.sendgrid_api_key` | `models.py:142`, `pipeline.py:38–50` |
| SMTP host | `Mailbox.smtp_host` | `models.py:132` |
| SMTP port | `Mailbox.smtp_port` (default 587; 465 ⇒ implicit TLS) | `models.py:133` |
| **Sender identity** | **`Mailbox.username` — the IMAP login address** | `mail_client.py:193` (`msg["From"] = username`); SendGrid equivalent `sendgrid_client.py:45` |

> **[VRF] Sender identity is not independently configurable.** There is no "from" field in
> the data model, the API, or the send call. The From header is always the address the
> IMAP client logs in with. Changing it means changing the monitored mailbox.

---

## 3. What the active POC can and cannot do

| Capability | Status | Evidence |
| --- | --- | --- |
| Read from the personal Gmail mailbox | **Structurally yes** | `IMAP4_SSL` + `login` + `SELECT INBOX` + `UID SEARCH/FETCH` (`mail_client.py:121–146`). Requires Gmail IMAP enabled and an app password. **Not confirmed live** — connecting is out of scope |
| Send from that mailbox | **Structurally yes, deployment-dependent** | Raw SMTP active (`SENDGRID_API_KEY` unset). **[VRF]** `sendgrid_client.py:1–5` documents raw SMTP failing on Railway (*"Network is unreachable"*), which is why SendGrid exists. On such a host the active send path would fail — safely, falling back to a draft |
| Process attachments | **Yes, in flight only** | `_extract_attachments` (`mail_client.py:73–94`) decodes them; `pipeline.py:471–473` holds them in memory for CRM forwarding and **never persists bytes** — deliberate |
| **Preserve the original receiving address** | **❌ No** | The fetch reads `From`, `Date`, `Subject`, body and attachments **only** (`mail_client.py:149–167`). `To`, `Cc` and `Delivered-To` are never read, and `Email` has no recipient column (`models.py:28–73`) |
| Support multiple monitored mailboxes | **❌ No** | `ux_mailboxes_singleton`; single-row `select(Mailbox)` |
| Support aliases | **❌ No** | No column, field or code path anywhere — only two prose mentions of future work (`models.py:121`, `migrate_drop_org_columns.sql:44`) |

> **[VRF] A notable detail about the receiving address.** The IMAP fetch retrieves
> `(RFC822)` — the **complete raw message, every header included**. `To`, `Cc` and
> `Delivered-To` are therefore *present in the bytes already held in memory* and are
> discarded unread. This differs from the Graph path, where the field must be explicitly
> requested via `$select`. On the active path the gap is purely in our parsing, not in
> what the protocol makes available.

---

## 4. Active architecture vs SSOT

| Capability | Current POC behaviour **[VRF]** | SSOT requirement **[SSOT]** | Gap |
| --- | --- | --- | --- |
| **Approved shared mailboxes** | One mailbox, a personal Gmail, via IMAP Basic Auth | Six PW shared mailboxes **[VTF]**, Graph app-only | **Total.** Wrong mailbox, wrong tenant, wrong protocol, wrong count |
| **Aliases** | No representation at all | Four aliases routing to physical mailboxes, each carrying business meaning | **Total** |
| **Original receiving address** | Never read; never stored | Immutable `receiving_address` + `original_recipient` on every row; *"if it is not captured then, it is gone"* | **Total** — and unrecoverable for everything collected so far |
| **Mailbox identity** | Implicit — one mailbox, no `mailbox_id` on `emails` | `mailbox_id` on every row; the authorization key; immutable by DB trigger | **Total** |
| **Human approval** | Partial — `reply_status` (`none\|draft\|sent\|rejected`), `draft_reply`, and auto-send gated on `auto_reply_enabled AND auto_sendable AND needs_reply` | Approval as a per-mailbox right (`can_approve`), distinct from send | **Partial** — the draft/approve concept exists; the authorization dimension does not |
| **Reply identity** | Always `Mailbox.username`, the IMAP login | Derived from mailbox + alias + the user's send rights; *"derived, never chosen"* | **Total** — cannot express per-mailbox or per-alias identity |
| **Attachment preservation** | Extracted in memory, forwarded to CRM, never persisted | Same — *"don't store the binary"* | **✅ None** — aligned |
| **Audit trail** | `agent_runs` table only (run-level). No per-action audit of who sent what as which identity | Audit record capturing approving user, identity used, alias decision | **Substantial** — explicitly deferred to the audit's Phase 2 |
| **CRM / SharePoint** | `crm_sync.py` present, `CRM_ENABLED` set, attachments forwarded | CRM integration unchanged by the mailbox work; SharePoint out of scope (Phase 2–5) | **Low** — CRM path exists |
| **Safeguarding** | Taxonomy exists — `safeguarding_concern` flag and `safeguarding_restricted` route (`agent_factory/app/taxonomy.py:44,100,124`). **No clearance model, no visibility restriction** | Restricted items invisible without clearance — absent from list, counts, search *and* alerts; clearance a separate axis | **Severe.** Classification without restriction: the route is computed and then ignored for access purposes |
| **Referral handling** | Taxonomy + CRM forwarding present | Unchanged by this work; referral depth is audit Phase 3 | **Low** |
| **Access control** | **Flat** `{app_key}:emails.read` / `emails.write` (`api.py:30–31`). No mailbox dimension | Per-mailbox `can_read`/`can_send`/`can_approve`, enforced on every query; 404 not 403 | **Total** — the audit's most serious finding, unaddressed |
| **Failure / retry** | Reasonably mature: bounded socket timeouts, per-message parse isolation, `known_uids` dedup independent of the watermark, UIDVALIDITY namespacing by `sync_epoch`, send failure → draft fallback | Not specifically constrained beyond operational hardening (Phase 5) | **✅ Low** — the strongest part of the current implementation |

---

## 5. Phase -1 gate reassessment

### 5.1 T8 — CLOSED

**[VRF]** The POC is configured against a third application identity
(`e18a9374-…`) in a non-Project-West tenant (`d868ea1b-…` ≠ `98b19873-…`), with
`provider = imap` and a personal Gmail mailbox. Its stored Graph credentials are inert on
the active path.

### 5.2 Findings now **obsolete as descriptions of the POC** (but intact as target-state facts)

None of the tenant verification is wrong. What changes is what it describes — **the
Phase -1A/-1B gates characterise the *target* architecture, not anything currently
running.**

| Finding | Restatement |
| --- | --- |
| **B1** "No `Mail.Read` → ingestion cannot run" | **Does not block the POC**, which does not use Graph. It blocks the *target*. The POC is not waiting on a tenant change |
| **B2** send scope = `referrals@` only | Same — constrains the target; the active path never reaches Exchange |
| **T4** alias sending under app-only Graph | Target-only. The active path's sender problem is different and more absolute (§2) |
| **T5** alias recoverability from Graph | **Re-framed, not obsolete.** On the active IMAP path the headers are already retrieved and discarded (§3) — the question becomes a parsing gap, not a protocol one |
| RBAC scope / allow-deny evidence | Valid and useful — currently exercised by nothing |

### 5.3 Findings that remain fully valid

- **[VTF]** Six-mailbox inventory, four aliases, the nine-user permission matrix, zero
  Graph app-role assignments on both PW service principals, the `Mail.Send`-to-`referrals@`
  scope with its allow/deny evidence, zero transport rules. All are tenant facts,
  independent of what the POC does.
- **[VTF]** T6 volumetrics — 205,316 items, `info@` at 161,833. These are the real
  mailboxes and the first-sync risk is unchanged.
- **T10** — **strengthened.** **[VRF]** The IMAP path also binds to the Inbox
  (`conn.select("INBOX")`, `mail_client.py:124`), so the Inbox-only blind spot is now
  confirmed on *both* code paths, not one.
- **The `monitor_from` / first-sync risk — worse than stated.** **[VRF]** With no
  `since_uid`, the IMAP search criteria is literally `"ALL"` (`mail_client.py:130`), and
  `models.py:147–150` confirms a first sync "naturally pulls everything, however large the
  backlog." There is still no `monitor_from` field anywhere.
- **[SSOT]** Every requirement in the Phase -1 summary §5 stands unchanged.

### 5.4 New blockers caused by the active architecture

| # | Blocker |
| --- | --- |
| **N1** | **The active path cannot be pointed at Project West at all.** **[VRF]** `models.py:138–141` states Graph exists "for tenants with IMAP Basic Auth disabled" — which is why the Graph client was written. IMAP Basic Auth is the mechanism PW's tenant does not offer. This is not a configuration change; the IMAP path is architecturally incapable of serving PW |
| **N2** | **No Project West mail has ever been processed by this system.** **[VRF]** The only configured mailbox is a personal Gmail in an unrelated tenant. Any demonstration, metric, or acceptance evidence produced to date reflects that mailbox — not PW mail, not PW taxonomy distribution, not PW volumes |
| **N3** | **The receiving address is being discarded at collection, today.** **[VRF]** Every message ingested so far lost its `To`/`Cc`/`Delivered-To` at parse time. Per **[SSOT]** §11 that information "exists only in the headers at the moment of collection" — it cannot be reconstructed from stored rows |
| **N4** | **The active send path is raw SMTP with no SendGrid fallback configured.** **[VRF]** `SENDGRID_API_KEY` unset; `sendgrid_client.py:1–5` documents raw SMTP being blocked on Railway. Whether sending works at all is deployment-dependent and unverified |
| **N5** | **Sender identity is structurally fixed** to the IMAP login address (§2). No per-mailbox or per-alias identity is expressible without changing which mailbox is monitored |
| **N6** | **Client correspondence in a personal mailbox.** **[VRF]** The configured mailbox is a personal Gmail account outside PW's tenant and outside its data-protection boundary. Whether any real PW data has passed through it is **unknown from the repository** and is a **[PWD]** question |

---

## A. What is proven

- **[VRF]** The active read path is IMAP Basic Auth against `INBOX`; the active send path
  is raw SMTP. Graph is present in the codebase and **not exercised**.
- **[VRF]** Sender identity is always the IMAP login address; there is no `from` field
  anywhere in the model, API or send call.
- **[VRF]** The receiving address is retrieved in the raw message and discarded unread; no
  recipient column exists on `emails`.
- **[VRF]** One mailbox only, enforced by a unique index. No alias representation anywhere.
- **[VRF]** Attachments are handled in memory and never persisted — aligned with the SSOT.
- **[VRF]** Access control is flat `emails.read` / `emails.write` with no mailbox dimension.
- **[VRF]** Safeguarding is classified but not restricted: the taxonomy computes a
  `safeguarding_restricted` route that nothing then enforces.
- **[VRF]** Failure handling is genuinely solid — bounded timeouts, per-message isolation,
  watermark-independent dedup, UIDVALIDITY namespacing, draft fallback on send failure.
- **[VTF]** All Phase -1A/-1B tenant facts stand; they describe the target, which nothing
  currently implements.

## B. What remains unverified

- Whether the POC can actually connect to, read from, or send via the configured Gmail
  account — connecting was out of scope.
- Whether the active send path functions in the deployed environment (N4).
- Whether any real Project West correspondence has passed through the personal mailbox
  (N6).
- **T3** legacy Application Access Policies — query failed, not proven absent.
- **T10** inbox rules, mailbox forwarding, per-folder distribution — never queried.
- **T4** alias sending under app-only Graph; **T5** alias recoverability — both target-path.
- The identity `e18a9374-…` and tenant `d868ea1b-…` — not investigated, per instruction.

## C. Implementation blockers

**Carried forward, target-path:** B3 (no Entra groups), B4 (no `monitor_from` mechanism),
B5 (migrations unapplied), B6 (safeguarding leads unnamed), B8 (Inbox-only unvalidated).
**B1/B2** block the target but not the POC (§5.2).

**New, from the active architecture:** **N1** (IMAP cannot serve PW — architectural, not
configurational), **N2** (no PW mail ever processed), **N3** (receiving address being lost
now), **N4** (send path possibly non-functional in deployment), **N5** (sender identity
structurally fixed), **N6** (client data in a personal mailbox — scope unknown).

**[VRF]** N1 is the one that reframes the programme: the gap between the POC and the SSOT
is not a configuration change or a credential swap. The active path and the target path
share taxonomy, CRM forwarding, dedup and failure handling — but the mailbox, identity,
protocol and authorization layers have no overlap.

## D. Decisions required from Project West

Carried forward: **1** (Entra groups, nine users), **2** (confirm the agent registration),
**3** (safeguarding leads), **4** (cutover/cutoff per mailbox), **5**–**9**, **11**–**17**
as recorded in the Phase -1 summary.

New, arising from this reconciliation:

| # | Decision |
| --- | --- |
| **18** | **Has any real Project West correspondence passed through `harshithareddy2711l02@gmail.com`?** A data-protection question, not a technical one. Only PW and the developer can answer it |
| **19** | **Is the personal Gmail account an accepted development fixture**, and should it remain one once PW mailboxes are reachable? |
| **20** | **What is the status of everything already ingested?** **[VRF]** Every stored row lacks a receiving address and a mailbox identity, and came from a non-PW mailbox. Whether it is retained, segregated or discarded is PW's call |

---

**No implementation proposed. No code, database, migrations, configuration, Exchange,
Entra or Azure changes were made. No services were started and no mail was fetched or
sent.**
