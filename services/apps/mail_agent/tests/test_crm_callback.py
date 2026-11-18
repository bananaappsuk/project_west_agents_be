"""The endpoint the CRM uses to tell us what a coordinator decided.

It is the one route that can change an email's CRM state without a signed-in
user, so the secret check is the whole security of it. These call the handler
directly with a fake session: the suite has no database, and the behaviour
worth pinning is the authorisation and the status mapping, neither of which
needs one.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import crm_callback
from app.config import settings
from app.models import Email

pytestmark = pytest.mark.asyncio

SECRET = "a-test-callback-secret"


class FakeSession:
    """Answers `.scalar()` with one email, and records whether we committed."""

    def __init__(self, email: Email | None):
        self._email = email
        self.committed = False

    async def scalar(self, _stmt):
        return self._email

    async def commit(self):
        self.committed = True


def an_email(**over) -> Email:
    email = Email()
    email.uid = over.get("uid", "AAMkAGMxNTJ-message-id")
    email.crm_status = over.get("crm_status", "pending_approval")
    email.crm_reference = over.get("crm_reference", None)
    return email


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(settings, "crm_callback_secret", SECRET)


def body(**over) -> dict:
    return {
        "activity_ref": "PW-A-2026-0001",
        "external_ref": "AAMkAGMxNTJ-message-id",
        "review_status": "APPLIED",
        "applied_reference": "PW-R-2026-0123",
        **over,
    }


class TestItRefusesAnythingButTheCrm:
    async def test_a_wrong_secret_is_refused(self):
        session = FakeSession(an_email())
        with pytest.raises(HTTPException) as caught:
            await crm_callback(body(), x_callback_secret="not-it", session=session)
        assert caught.value.status_code == 401
        assert not session.committed

    async def test_no_secret_is_refused(self):
        session = FakeSession(an_email())
        with pytest.raises(HTTPException) as caught:
            await crm_callback(body(), x_callback_secret="", session=session)
        assert caught.value.status_code == 401
        assert not session.committed

    async def test_an_unconfigured_service_refuses_everyone(self, monkeypatch):
        """Fails closed. An empty configured secret must not mean "any secret
        will do" -- this endpoint can change what an email's CRM state says,
        so a deployment that has not been wired up has to refuse rather than
        wave callers through, including a caller sending an empty secret."""
        monkeypatch.setattr(settings, "crm_callback_secret", "")
        session = FakeSession(an_email())
        for attempt in ("", "anything"):
            with pytest.raises(HTTPException) as caught:
                await crm_callback(body(), x_callback_secret=attempt, session=session)
            assert caught.value.status_code == 401
        assert not session.committed


class TestItRecordsTheDecision:
    @pytest.mark.parametrize(
        "review_status,expected",
        [
            ("APPLIED", "sent"),
            ("REJECTED", "rejected"),
            ("APPLY_FAILED", "failed"),
            ("PENDING_REVIEW", "pending_approval"),
        ],
    )
    async def test_each_decision_maps_to_a_status(self, review_status, expected):
        email = an_email()
        session = FakeSession(email)
        await crm_callback(
            body(review_status=review_status), x_callback_secret=SECRET, session=session
        )
        assert email.crm_status == expected
        assert session.committed

    async def test_approval_records_the_reference_the_crm_created(self):
        email = an_email()
        session = FakeSession(email)
        await crm_callback(body(), x_callback_secret=SECRET, session=session)
        assert email.crm_reference == "PW-R-2026-0123"

    async def test_a_decision_without_a_reference_keeps_the_one_we_had(self):
        email = an_email(crm_reference="PW-A-2026-0001")
        session = FakeSession(email)
        await crm_callback(
            body(applied_reference=None), x_callback_secret=SECRET, session=session
        )
        assert email.crm_reference == "PW-A-2026-0001"

    async def test_an_unknown_status_is_treated_as_still_waiting(self):
        """Never guess that something was created. An ack this agent does not
        recognise means the CRM knows something it does not, and the safe
        reading is that the decision has not been taken."""
        email = an_email(crm_status="sent")
        session = FakeSession(email)
        await crm_callback(
            body(review_status="SOMETHING_NEW"), x_callback_secret=SECRET, session=session
        )
        assert email.crm_status == "pending_approval"


class TestItIsQuietAboutWhatIsNotOurs:
    async def test_an_email_we_do_not_have_is_not_an_error(self):
        """The CRM holds submissions from both agents and only one of them
        owns any given external_ref, so the other must not fail the callback
        and have the CRM retry it for ever."""
        session = FakeSession(None)
        response = await crm_callback(body(), x_callback_secret=SECRET, session=session)
        assert response.status_code == 204
        assert not session.committed

    async def test_a_callback_naming_nothing_is_rejected(self):
        session = FakeSession(an_email())
        with pytest.raises(HTTPException) as caught:
            await crm_callback(
                body(external_ref=""), x_callback_secret=SECRET, session=session
            )
        assert caught.value.status_code == 400
        assert not session.committed
