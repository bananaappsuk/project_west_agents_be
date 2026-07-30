"""Request models (camelCase) + serializers that emit the exact frontend shapes."""

from __future__ import annotations

from pydantic import BaseModel

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
    imapHost: str
    imapPort: int = 993
    smtpHost: str
    smtpPort: int = 587
    username: str
    password: str


class AgentConfigIn(BaseModel):
    cronInterval: str | None = None
    fetchPerRun: int | None = None
    enabled: bool | None = None


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
        "summary": e.summary or "",
        "category": e.category or "",
        "priority": e.priority or "Medium",
        "read": e.read,
        "draftReply": e.draft_reply,
        "summaryStatus": e.summary_status,
        "replyStatus": e.reply_status,
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
