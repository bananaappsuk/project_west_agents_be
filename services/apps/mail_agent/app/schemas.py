"""Request models (camelCase) + serializers that emit the exact frontend shapes."""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from .models import Email, Folder


# ---- request bodies (match frontend camelCase) ----
class MoveIn(BaseModel):
    ids: list[str]
    folderId: str | None = None


class IdsIn(BaseModel):
    ids: list[str]


class ReplyIn(BaseModel):
    body: str


class FolderIn(BaseModel):
    name: str
    color: str


class FolderPatch(BaseModel):
    name: str | None = None
    color: str | None = None


class MailboxIn(BaseModel):
    provider: str = "imap"  # "imap" | "graph"
    imapHost: str = ""
    imapPort: int = 993
    smtpHost: str
    smtpPort: int = 587
    username: str
    password: str
    # Graph (app-only) fields — required when provider == "graph"
    tenantId: str | None = None
    clientId: str | None = None
    clientSecret: str | None = None

    # A stray leading/trailing space (easy to introduce via copy-paste into the
    # Settings form) changes the string without changing what the user meant —
    # for `username` specifically, that's also what save_mailbox compares to
    # decide whether the mailbox identity changed (see api.py), so an
    # unstripped space there falsely looks like a switch to a different
    # account and forces a full watermark reset/resync. IMAP/Graph servers
    # vary in how leniently they treat a padded username, so better to never
    # send one at all.
    @field_validator("imapHost", "smtpHost", "username", mode="after")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class AgentConfigIn(BaseModel):
    cronInterval: str | None = None
    fetchPerRun: int | None = None
    enabled: bool | None = None
    autoReplyEnabled: bool | None = None


# ---- serializers (ORM -> frontend JSON) ----
def serialize_email(e: Email) -> dict:
    return {
        "id": e.id,
        "uid": e.uid,
        "from": e.sender,
        "fromEmail": e.from_email,
        "subject": e.subject,
        "receivedAt": e.received_at.isoformat() if e.received_at else None,
        "body": e.body,
        "contentType": e.content_type or "text",
        "summary": e.summary or "",
        "category": e.category or "",
        "priority": e.priority or "Medium",
        "confidence": e.confidence or 0.0,
        "needsReply": e.needs_reply,
        "read": e.read,
        "draftReply": e.draft_reply,
        "summaryStatus": e.summary_status,
        "replyStatus": e.reply_status,
        "autoSent": e.auto_sent,
        "intent": e.intent,
        "crmStatus": e.crm_status,
        "crmReference": e.crm_reference,
        "folderId": e.folder_id,
        "archivedAt": e.archived_at.isoformat() if e.archived_at else None,
        "archivedBy": e.archived_by,
    }


def serialize_folder(f: Folder) -> dict:
    return {
        "id": f.id,
        "name": f.name,
        "color": f.color,
        "createdAt": f.created_at.isoformat() if f.created_at else None,
    }
