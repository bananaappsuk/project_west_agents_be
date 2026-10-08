from platform_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "mail_agent"
    app_key: str = "mail-agent"  # token audience this service accepts

    encryption_key: str | None = None  # Fernet key for mailbox passwords

    agent_factory_url: str = "http://localhost:8002"
    agent_factory_internal_key: str | None = None

    cron_enabled: bool = True     # master switch for the background scheduler
    cron_tick_minutes: int = 5    # how often the scheduler checks each org's schedule
    fetch_per_run: int = 20

    # CRM intake (pw-crm-be) — referrals, case communications, session change requests.
    # Off by default so environments without real CRM credentials never call out.
    crm_enabled: bool = False
    crm_base_url: str = "http://localhost:8000/api/v1"
    crm_client_id: str = ""
    crm_api_key: str = ""

    # SendGrid — replaces raw SMTP for sending replies on IMAP-provider mailboxes
    # when set (see sendgrid_client.py / pipeline.py's _send_fn_for). Needed on
    # Railway specifically: its network doesn't route outbound SMTP at all
    # (confirmed live: connecting to smtp.gmail.com:587 fails immediately with
    # "[Errno 101] Network is unreachable"), so raw SMTP can never succeed there
    # regardless of credentials. SendGrid is plain HTTPS, unaffected. Leave unset
    # to keep using raw SMTP (e.g. for local dev, where it does work).
    #
    # No separate "from" setting here — sendgrid_client sends as whichever
    # mailbox's own address is currently configured (same as raw SMTP always
    # did), which must be a Single Sender verified in the SendGrid dashboard.
    sendgrid_api_key: str | None = None

    # ── Microsoft Graph, app-only (Project West) ─────────────────────────────
    # ONE app registration serves all six shared mailboxes, so these live in
    # deployment configuration rather than per-mailbox database rows. That takes the
    # application out of the business of storing credentials entirely — a meaningful
    # reduction in what a database compromise would yield, and the reason crypto.py
    # can go once the IMAP path is retired.
    #
    # Authorization is NOT granted here. The service principal holds no Microsoft
    # Graph application permissions at all; its access comes from Exchange RBAC for
    # Applications, scoped to exactly the six mailboxes. That is deliberate and must
    # stay that way: Entra and Exchange RBAC permissions are a UNION, so adding a
    # tenant-wide Graph grant here would override the management scope and expose
    # every personal mailbox in the tenant. The fix for "the agent cannot read" is
    # never to grant it Mail.Read in Entra.
    graph_tenant_id: str | None = None
    graph_client_id: str | None = None
    graph_client_secret: str | None = None

    @property
    def graph_configured(self) -> bool:
        return bool(self.graph_tenant_id and self.graph_client_id
                    and self.graph_client_secret)


settings = Settings()
