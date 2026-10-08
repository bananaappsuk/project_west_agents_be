"""Where a classified email ends up, and whether it is restricted.

Two independent signals can mark an email as safeguarding material:

  * the address it arrived at -- safeguarding@ is an alias into coordinator@, and
    the sender's choice of address is itself a signal;
  * the classifier -- a message to info@ that the model flags
    `safeguarding_indicator` is just as restricted as one sent to safeguarding@.

**The restricted route wins whenever either fires.** Belt and braces, in both
directions, because the cost of a false negative is a safeguarding disclosure
sitting in an open queue and the cost of a false positive is one extra item for a
cleared user to look at.

The consequence that drives this module: classification runs *after* ingestion and
writes the same columns, so a naive assignment lets the model silently undo a
restriction the arrival address already established. Restriction only ever ratchets
up here; nothing in this file can clear it.
"""

from __future__ import annotations

#: Taxonomy route for safeguarding material. Matches agent_factory's taxonomy.
ROUTE_SAFEGUARDING = "safeguarding_restricted"
#: Taxonomy route for an item with no safe classification.
ROUTE_EXCEPTION = "exception"
#: Cross-cutting flag the classifier raises on a possible safeguarding issue. It
#: applies *in addition to* the primary category -- a complaint that discloses a
#: safeguarding concern is still a complaint, and must carry both.
FLAG_SAFEGUARDING = "safeguarding_indicator"


def apply_classification(
    *,
    current_route: str | None,
    current_restricted: bool,
    alias_restricted: bool,
    model_route: str | None,
    model_flags: list[str] | None,
) -> tuple[str, bool, list[str]]:
    """Merge a classifier result with what ingestion already established.

    `alias_restricted` is whether the address the message ARRIVED at is restricted.
    That is a fact about delivery, not a model opinion, so it cannot be overruled.

    Returns (route, restricted, flags).
    """
    flags = list(model_flags or [])

    model_says_safeguarding = (
        FLAG_SAFEGUARDING in flags or model_route == ROUTE_SAFEGUARDING
    )

    # Ratchet only. An email already restricted at ingestion stays restricted even
    # if the model disagrees -- the model does not get a vote on what address the
    # message was sent to.
    restricted = bool(current_restricted or alias_restricted or model_says_safeguarding)

    if restricted:
        route = ROUTE_SAFEGUARDING
        # Make the flag explicit even when the restriction came from the arrival
        # address alone, so "why is this restricted?" is answerable from the row
        # without re-deriving it from the alias table.
        if FLAG_SAFEGUARDING not in flags:
            flags.append(FLAG_SAFEGUARDING)
    elif current_route == ROUTE_EXCEPTION:
        # Ingestion could not determine the business entry point. A classifier that
        # never saw that ambiguity must not quietly route the item as though it had
        # been understood -- it stays in the exception queue for a human.
        route = ROUTE_EXCEPTION
    else:
        route = model_route or current_route or ROUTE_EXCEPTION

    return route, restricted, flags
