"""Fakes for the orchestration integration tests.

No database: this service's suite has never had one (there is no sqlite driver in
its venv) and the orchestration work does not introduce the need for one. The
adapter's database access all goes through `orchestration.SessionLocal`, so a fake
session factory is enough to exercise the authorization adapter and the step
handlers against real `Email`, `Mailbox` and `MailboxAlias` model instances.

The `Email`/`Mailbox` objects here are the genuine SQLAlchemy models, constructed
unattached. Column defaults are not applied without a flush, so every attribute a
test depends on is set explicitly -- which is also what keeps these tests honest
about what the adapter actually reads.
"""

from __future__ import annotations

import pytest

from app.models import Email, Mailbox, MailboxAlias


class _Scalars(list):
    """A list that also answers `.all()`, as SQLAlchemy's ScalarResult does."""

    def all(self):
        return list(self)


def _entity_of(stmt) -> str:
    """The name of the model a `select()` targets, or "" if it cannot be told."""
    try:
        descriptions = stmt.column_descriptions
    except Exception:  # pragma: no cover - not a selectable
        return ""
    if not descriptions:
        return ""
    entity = descriptions[0].get("entity")
    return entity.__name__ if entity is not None else ""


class FakeSession:
    """Enough of `AsyncSession` for the adapter: `get`, `scalar`, `add`, `commit`.

    `scalar` dispatches on which model the statement selects, and does no more than
    that. It deliberately does not evaluate the WHERE clause: a fake that tried to
    interpret SQLAlchemy statements would be a second implementation of the query
    planner and would fail in ways the real database does not.

    The consequence worth knowing: a scoped query (`api._get_email` filters on
    `mailbox_id IN readable`) comes back here unfiltered, so no test may use this
    fake to show that mailbox scoping works. That is `test_mailbox_scope.py`'s job,
    and the authorization port's -- both of which run against the real logic.

    Dispatching at all is not a nicety. Returning the configured alias for every
    query, which this used to do, meant `_get_email` received a `MailboxAlias` and
    the route then set reply fields on it.
    """

    def __init__(self, store: FakeStore) -> None:
        self._store = store

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, model, pk):
        return self._store.rows.get((model.__name__, pk))

    async def scalar(self, stmt):
        entity = _entity_of(stmt)
        if entity == "MailboxAlias":
            # Configured directly rather than stored, because `prepare_reply`
            # looks it up by address and a test cares only whether one matched.
            return self._store.alias
        return self._store.first(entity)

    async def scalars(self, stmt):
        """The one `scalars` the analysis pipeline issues inside a batch: which
        of these emails are already restricted.

        Answered from the store's own rows rather than by reading the statement,
        for the same reason as `scalar` above. Returned wrapped in an object with
        `.all()`, which is the part of SQLAlchemy's ScalarResult the caller uses.
        """
        entity = _entity_of(stmt)
        rows = [r for (t, _pk), r in self._store.rows.items() if t == entity]
        if entity == "Email":
            return _Scalars([e.id for e in rows if getattr(e, "restricted", False)])
        return _Scalars([])

    def add(self, obj) -> None:
        self._store.added.append(obj)

    async def commit(self) -> None:
        self._store.commits += 1


class FakeStore:
    """What the fake sessions share. One per test."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], object] = {}
        self.alias: MailboxAlias | None = None
        self.added: list[object] = []
        self.commits = 0

    def put(self, obj) -> None:
        self.rows[(type(obj).__name__, obj.id)] = obj

    def __call__(self) -> FakeSession:
        """Stands in for `SessionLocal`, which is called to open a session."""
        return FakeSession(self)

    def first(self, type_name: str):
        """The one stored row of this type, or None. The fakes hold a single
        mailbox and a single email, which is what the adapter's queries expect."""
        for (stored_type, _pk), row in self.rows.items():
            if stored_type == type_name:
                return row
        return None

    def added_of(self, type_name: str) -> list[object]:
        return [o for o in self.added if type(o).__name__ == type_name]


def make_email(
    *,
    id: str = "email-1",
    mailbox_id: str | None = "mbx-1",
    restricted: bool = False,
    route: str = "referrals",
    draft_reply: str | None = "Thank you for your referral.",
    reply_status: str = "draft",
    from_email: str = "sender@example.org",
    subject: str = "New referral",
    receiving_address: str | None = "referrals@projectwest.example",
    # Resolved by default: the ordinary case is a row whose receiving address was
    # actually determined. "legacy" and "unresolved" are the exception states, and
    # the tests that care about them pass one explicitly.
    receiving_address_status: str = "resolved_single",
) -> Email:
    email = Email(
        id=id,
        uid="1",
        sender="A Sender",
        from_email=from_email,
        subject=subject,
        mailbox_id=mailbox_id,
        restricted=restricted,
        route=route,
        draft_reply=draft_reply,
        reply_status=reply_status,
        receiving_address=receiving_address,
        receiving_address_status=receiving_address_status,
    )
    return email


def make_mailbox(*, id: str = "mbx-1", provider: str = "graph") -> Mailbox:
    return Mailbox(
        id=id,
        imap_host="", imap_port=993, smtp_host="", smtp_port=587,
        username="referrals@projectwest.example",
        password_enc="",
        provider=provider,
        tenant_id="t", client_id="c", client_secret_enc="enc",
    )


def make_alias(
    *, address: str = "referrals@projectwest.example", reply_allowed: bool = True,
    restricted: bool = False,
) -> MailboxAlias:
    return MailboxAlias(
        id="alias-1", mailbox_id="mbx-1", address=address,
        reply_allowed=reply_allowed, restricted=restricted,
    )


@pytest.fixture
def store():
    """A store pre-populated with one replyable email in one mailbox."""
    s = FakeStore()
    s.put(make_email())
    s.put(make_mailbox())
    s.alias = make_alias()
    return s


@pytest.fixture
def patched_crypto(monkeypatch):
    """Stub credential decryption.

    The fake Mailbox rows carry placeholder ciphertext, which Fernet rightly
    rejects. Decrypting a real stored secret is `crypto`'s own concern and is not
    what any of these tests are about -- leaving it live would only mean asserting
    on a Fernet error instead of on workflow behaviour.
    """
    from app import orchestration
    monkeypatch.setattr(orchestration.crypto, "decrypt", lambda _ct: "decrypted")


@pytest.fixture
def patched_sessions(monkeypatch, store, patched_crypto):
    """Point the adapter's session factory at the fake store."""
    from app import orchestration
    monkeypatch.setattr(orchestration, "SessionLocal", store)
    return store


@pytest.fixture
def patched_pipeline(monkeypatch, store, patched_sessions):
    """Point the analysis pipeline at the fakes too.

    `patched_sessions` only covers `orchestration.SessionLocal`. The pipeline has
    its own import of it, plus two outbound calls -- the agent and the CRM -- that
    a test must not make. Without this a test that calls `analyze_and_persist`
    reaches the configured Neon database and the live agent service, which is how
    the first version of these tests passed while doing nothing.

    The analysis result is deliberately `needs_reply` and `auto_sendable`, so the
    auto-send branch is actually entered and a test that expects nothing to go out
    is testing a refusal rather than an absence.
    """
    from app import pipeline

    monkeypatch.setattr(pipeline, "SessionLocal", store)

    async def fake_analyze(_payload):
        return {
            "analysis": {
                "summary": "A summary.",
                "category": "referrals",
                "priority": "Medium",
                "confidence": 0.95,
                "needs_reply": True,
                "suggested_reply": "Thanks for your referral.",
                "intent": "REFERRAL",
                "flags": [],
            },
            "route": "referrals",
            "restricted": False,
            "auto_sendable": True,
        }

    async def fake_crm(_payload, _analysis, _attachments):
        return "none", None, None

    monkeypatch.setattr(pipeline.agent_client, "analyze_email", fake_analyze)
    monkeypatch.setattr(pipeline.crm_sync, "after_email_analysis", fake_crm)
    return store


# The factories are exposed as fixtures rather than imported directly from this
# module. `tests/` is not a package here (the existing suite has always been
# collected as top-level modules), so a relative import would not resolve, and
# making it a package would change the module names of every existing test for no
# benefit.
@pytest.fixture
def email_factory():
    return make_email


@pytest.fixture
def mailbox_factory():
    return make_mailbox


@pytest.fixture
def alias_factory():
    return make_alias
