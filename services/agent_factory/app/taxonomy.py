"""The controlled Project West classification taxonomy — shared by mail and voice.

Single source of truth for Reference 1 of the Project West Email Agent Capability
Brief v1.1 (`docs/Project_West_Email_Agent_Capability_Brief_v1_1.docx`). The brief
defines it as an email taxonomy; PW has confirmed the same vocabulary is used for
call recordings too, so both `analyze_email` and `analyze_call` classify into it.
One vocabulary across channels is also what makes the brief's §8.3 cross-channel
matching between email and voice possible later.

Two independent axes, per Reference 1:

  * exactly one `PRIMARY` category — the message's business purpose, and
  * zero or more `FLAGS` — cross-cutting urgency/sensitivity markers that do not
    replace the primary category. "Safeguarding indicator" is the important one:
    the brief requires the restricted safeguarding route to apply *even when
    another category is primary* (e.g. a complaint that discloses a concern).

Categories are stored as the stable snake_case codes below, never as display
labels — labels are presentation and may be reworded without a data migration.

Routing deliberately stops at a *queue* name (`ROUTE`), not a mailbox address.
Mapping a queue onto a physical mailbox is the routing layer's job, because PW's
alias structure means several queues land in one mailbox (payments@ -> finance@,
complaints@ and safeguarding@ -> coordinator@). Keeping that mapping separate
also lets PW re-point a queue without touching classification.
"""

from __future__ import annotations

from typing import Literal

# ─────────────────────────────────────────────────────────────────────────────
# Primary categories (Reference 1, "Primary classification")
# ─────────────────────────────────────────────────────────────────────────────

Category = Literal[
    "general_enquiry",
    "new_referral",
    "referral_update",
    "returned_part_2",
    "case_correspondence",
    "payment_or_finance",
    "complaint",
    "safeguarding_concern",
    "report_or_document",
    "scheduling_change",
    "court_or_legal",
    "supplier_or_admin",
    "unclassified",
]

#: Display label and the brief's own "Typical meaning", which doubles as the
#: prompt text the model is given — one definition, not two that can drift.
CATEGORY_LABELS: dict[str, str] = {
    "general_enquiry": "General enquiry",
    "new_referral": "New referral",
    "referral_update": "Referral update",
    "returned_part_2": "Returned Part 2",
    "case_correspondence": "Existing Case correspondence",
    "payment_or_finance": "Payment or finance",
    "complaint": "Complaint",
    "safeguarding_concern": "Safeguarding concern",
    "report_or_document": "Report or document",
    "scheduling_change": "Scheduling change",
    "court_or_legal": "Court or legal correspondence",
    "supplier_or_admin": "Supplier or administration",
    "unclassified": "Unclassified or uncertain",
}

CATEGORY_MEANINGS: dict[str, str] = {
    "general_enquiry": "Initial questions about PW, services, availability or contact",
    "new_referral": "A request to refer a person or family into a PW service",
    "referral_update": "Further information about an open Referral Intake",
    "returned_part_2": "A completed or partially completed service-specific Part 2 form",
    "case_correspondence": "Communication about an established Case",
    "payment_or_finance": "Payment evidence, invoice, refund or finance query",
    "complaint": "Dissatisfaction or a request for complaint handling",
    "safeguarding_concern": "Information indicating a possible safeguarding issue",
    "report_or_document": "Family Time report, court document or other Case document",
    "scheduling_change": "Cancellation, absence, delay, room or session change",
    "court_or_legal": "Court direction, solicitor correspondence or legal deadline",
    "supplier_or_admin": "Administrative, supplier or operational correspondence",
    "unclassified": "No safe classification or competing categories",
}

CATEGORIES: tuple[str, ...] = tuple(CATEGORY_LABELS)

# ─────────────────────────────────────────────────────────────────────────────
# Default routing (Reference 1, "Default route")
# ─────────────────────────────────────────────────────────────────────────────

Queue = Literal[
    "info",
    "referrals",
    "coordinator",
    "finance",
    "reports",
    "management",
    "admin",
    "safeguarding_restricted",
    "exception",
]

#: The queue a category routes to when nothing else overrides it. Where the brief
#: gives two ("Coordinator or Management", "Reports or Case owner") the first is
#: the default and the case owner, if one is known, wins at routing time.
#:
#: NOTE on `supplier_or_admin`: there was a documented conflict over admin@ — the
#: M365 Mailbox & Access Report (September 2026) recorded it as an alias onto
#: management@, while Reference 1 and Appendix A treat Admin as its own mailbox.
#: The M365 Mailbox Access Map v1 (developer configuration view) settles it: admin@
#: is a shared mailbox, not an alias, and it is listed as its own column alongside
#: the other six. So `admin` stays a queue of its own, and keeping it separate from
#: `management` turns out to matter — Avis, Jeffrey and Deshaun have no access to
#: Management, so routing supplier mail there would have hidden it from them.
CATEGORY_ROUTE: dict[str, str] = {
    "general_enquiry": "info",
    "new_referral": "referrals",
    "referral_update": "referrals",
    "returned_part_2": "referrals",
    "case_correspondence": "coordinator",
    "payment_or_finance": "finance",
    "complaint": "coordinator",
    "safeguarding_concern": "safeguarding_restricted",
    "report_or_document": "reports",
    "scheduling_change": "coordinator",
    "court_or_legal": "coordinator",
    "supplier_or_admin": "admin",
    "unclassified": "exception",
}

# ─────────────────────────────────────────────────────────────────────────────
# Cross-cutting flags (Reference 1, "Cross-cutting flags")
# ─────────────────────────────────────────────────────────────────────────────

#: Flag name -> the brief's definition, reused as prompt text.
FLAGS: dict[str, str] = {
    "urgent": "immediate or time-critical action may be required",
    "sensitive": "restricted handling or limited visibility is required",
    "safeguarding_indicator": (
        "apply the restricted safeguarding route even when another category is primary"
    ),
    "court_deadline": "record and alert the responsible staff member",
    "uncertain": "staff confirmation is required before record updates or sending",
}

FLAG_NAMES: tuple[str, ...] = tuple(FLAGS)

# ─────────────────────────────────────────────────────────────────────────────
# Reply-control derived from the taxonomy
# ─────────────────────────────────────────────────────────────────────────────

# Two distinct controls, which the brief keeps separate and so must we:
#
#   NEVER_AUTO_SEND — the agent may *prepare* a draft, but a person approves it
#     before it goes out. This is most of PW's work: §5.1 explicitly wants a
#     drafted acknowledgement for a new referral and a drafted information
#     request for an incomplete one; it just must not send either itself.
#
#   NEVER_DRAFT — the agent must not compose a substantive reply at all, because
#     deciding *whether* to respond is itself the staff decision. §5.5 and
#     demonstration test 9 require a safeguarding message to produce a restricted
#     alert "and no routine reply"; §5.8 sends unclassified items to the exception
#     queue rather than answering them.
#
# Collapsing these two into one check would silently drop the referral
# acknowledgement drafts the brief asks for, so they stay separate.

#: Categories where sending is always a human decision, however confident the
#: model is (brief §5.4, §5.5, §7.1). Replaces the old template-era
#: {"Security", "Billing"} pair, which had no PW meaning.
NEVER_AUTO_SEND: frozenset[str] = frozenset({
    "new_referral",            # §5.1 — the agent must not accept or reject a referral
    "referral_update",
    "returned_part_2",
    "payment_or_finance",      # §5.4 — Finance retain payment decisions
    "complaint",               # §5.5 — authorised staff determine the formal response
    "safeguarding_concern",    # §5.5 — no autonomous substantive response, ever
    "court_or_legal",          # §5.7 — legal responses are confirmed by staff
    "scheduling_change",       # §5.7 — schedule amendments are confirmed by staff
    "unclassified",            # §5.8 — goes to the exception queue instead
})

#: Flags that independently block an automatic send regardless of category.
NEVER_AUTO_SEND_FLAGS: frozenset[str] = frozenset({
    "safeguarding_indicator",
    "sensitive",
    "uncertain",
})

#: Categories where the agent must not compose a substantive reply at all.
NEVER_DRAFT: frozenset[str] = frozenset({
    "safeguarding_concern",   # §5.5 / test 9 — restricted alert, no routine reply
    "unclassified",           # §5.8 — exception queue, not an answer
})

#: Ditto, as a flag — a safeguarding disclosure inside a complaint or a case
#: email is still a safeguarding matter, so it suppresses the draft too.
NEVER_DRAFT_FLAGS: frozenset[str] = frozenset({"safeguarding_indicator"})


def is_safeguarding(category: str, flags: list[str] | set[str] | None) -> bool:
    """True when the restricted safeguarding route applies — either as the primary
    category or via the cross-cutting indicator on some other category."""
    return category == "safeguarding_concern" or "safeguarding_indicator" in (flags or ())


def blocks_auto_send(category: str, flags: list[str] | set[str] | None) -> bool:
    """True when *sending* must be a human decision. Deliberately independent of
    confidence: these are categories and flags where a correct, high-confidence
    classification still does not authorise the agent to send on its own. A draft
    may still be prepared — see `blocks_draft`."""
    return category in NEVER_AUTO_SEND or bool(
        NEVER_AUTO_SEND_FLAGS & set(flags or ())
    )


def blocks_draft(category: str, flags: list[str] | set[str] | None) -> bool:
    """True when the agent must not compose a substantive reply at all, because
    whether to respond is itself the staff decision (safeguarding, unclassified)."""
    return category in NEVER_DRAFT or bool(NEVER_DRAFT_FLAGS & set(flags or ()))


def route_for(category: str, flags: list[str] | set[str] | None = None) -> str:
    """The queue this item belongs in. A safeguarding indicator overrides the
    category's own route, per Reference 1's cross-cutting flag rule."""
    if is_safeguarding(category, flags):
        return "safeguarding_restricted"
    return CATEGORY_ROUTE.get(category, "exception")


# ─────────────────────────────────────────────────────────────────────────────
# Keyword fallback (no-API-key stub mode only)
# ─────────────────────────────────────────────────────────────────────────────

# Deliberately coarse — this exists so the pipeline is runnable end to end without
# a paid key, matching the stub-vs-LLM quality gradient the rest of llm.py follows.
# It is never the production classifier. Order matters: the most consequential
# categories are tested first, so a message that reads as several things at once
# lands on the one with the most careful handling rather than the first keyword hit.

_SAFEGUARDING_WORDS = (
    "safeguarding", "child protection", "at risk", "disclosure", "abuse",
    "neglect", "bruise", "unsafe", "welfare concern", "harm",
)
_COURT_WORDS = ("court", "solicitor", "hearing", "cafcass", "court order", "legal", "barrister")
_COMPLAINT_WORDS = ("complaint", "complain", "unhappy", "dissatisfied", "escalate", "poor service")
_FINANCE_WORDS = ("invoice", "payment", "remittance", "refund", "bacs", "receipt", "paid")
_SCHEDULING_WORDS = (
    "cancel", "reschedule", "postpone", "absence", "running late", "can't make",
    "cannot make", "move the session", "change the time",
)
_REFERRAL_WORDS = ("referral", "refer ", "please refer", "referring", "refer a")
_PART_2_WORDS = ("part 2", "part two", "part-2")
_REPORT_WORDS = ("report", "assessment", "court report", "family time report")
_ADMIN_WORDS = ("supplier", "renewal", "quotation", "quote", "contract", "subscription")
_URGENT_WORDS = ("urgent", "asap", "immediately", "emergency", "today", "critical")
_ENQUIRY_WORDS = ("enquiry", "enquire", "information about", "how do", "could you", "interested")


def classify_stub(text: str, *, case_ref: str = "") -> tuple[str, list[str]]:
    """Returns `(category, flags)` from keywords alone. Shared by the mail and call
    stubs so both fall back to the same vocabulary rather than two private guesses."""
    t = (text or "").lower()
    hit = lambda words: any(w in t for w in words)  # noqa: E731

    safeguarding = hit(_SAFEGUARDING_WORDS)
    referral = hit(_REFERRAL_WORDS)

    if safeguarding:
        category = "safeguarding_concern"
    elif referral and hit(_PART_2_WORDS):
        category = "returned_part_2"
    elif referral:
        # An existing reference in the text makes this an update to a referral we
        # already hold rather than a brand-new one.
        category = "referral_update" if case_ref else "new_referral"
    elif hit(_COMPLAINT_WORDS):
        category = "complaint"
    elif hit(_COURT_WORDS):
        category = "court_or_legal"
    elif hit(_SCHEDULING_WORDS):
        category = "scheduling_change"
    elif hit(_FINANCE_WORDS):
        category = "payment_or_finance"
    elif hit(_REPORT_WORDS):
        category = "report_or_document"
    elif case_ref:
        category = "case_correspondence"
    elif hit(_ADMIN_WORDS):
        category = "supplier_or_admin"
    elif hit(_ENQUIRY_WORDS) or "?" in (text or ""):
        category = "general_enquiry"
    else:
        category = "unclassified"

    flags: list[str] = []
    if safeguarding:
        flags.append("safeguarding_indicator")
    if hit(_URGENT_WORDS):
        flags.append("urgent")
    if category in ("safeguarding_concern", "complaint", "court_or_legal"):
        flags.append("sensitive")
    if category == "court_or_legal" and (hit(_URGENT_WORDS) or "deadline" in t):
        flags.append("court_deadline")
    if category == "unclassified":
        flags.append("uncertain")
    return category, flags


def prompt_category_list() -> str:
    """The category menu as the models see it — code, label and the brief's meaning,
    so the prompt and the stored codes can never drift apart."""
    return "\n".join(
        f"- {code} ({CATEGORY_LABELS[code]}): {CATEGORY_MEANINGS[code]}"
        for code in CATEGORIES
    )


def prompt_flag_list() -> str:
    return "\n".join(f"- {name}: {meaning}" for name, meaning in FLAGS.items())
