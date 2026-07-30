# Platform Backend (microservices)

Monorepo for the platform described in [`../ARCHITECTURE.md`](../ARCHITECTURE.md).
Every application (mail-agent, voice-agent, Rekroot, Educare, …) shares these services.

## Services

| Path | Service | Port (dev) | Purpose |
|---|---|---|---|
| `services/gateway` | API Gateway | 8000 | Thin: routing + coarse token check (JWKS) |
| `services/auth` | Auth | 8001 | Identity, RBAC, JWT/JWKS, shared common APIs |
| `services/agent_factory` | Agent Factory | 8002 | Runs all agents (LangGraph). Code-defined agents |
| `services/billing` | Billing | 8003 | Plans, metering, quotas |
| `services/apps/mail_agent` | mail-agent | 8010 | Email product APIs (IMAP/SMTP fetch, AI triage, cron) |
| `services/apps/voice_agent` | voice-agent | 8011 | Call-recording product APIs (BT Cloud fetch, AI analysis, cron) |
| `libs/platform_common` | shared lib | — | Token verification + scope checks (pip-installed by every service) |

## Conventions

- **Python 3.10+**, `pip` + `venv` + `requirements.txt` per service.
- **No Docker, no local infra.** Databases are **Neon Postgres** (cloud),
  **one database per service** (`DATABASE_URL` in each service's `.env`).
- **Auth**: email/password + RS256 JWT + JWKS + rotating refresh tokens (in Postgres).
  OAuth2 (SSO / client-credentials) is a later phase.

## Running a service (from the service directory)

```bash
python -m venv .venv
.venv\Scripts\activate                # Windows
pip install -r requirements.txt
copy .env.example .env                 # then fill in DATABASE_URL etc.
uvicorn app.main:app --reload --port 8001
```

`platform_common` is installed as an editable local dependency via each service's
`requirements.txt` (`-e ../../libs/platform_common`), so changes to the shared lib are
picked up without reinstalling.

## Database

Each service owns its Neon database. Use the **asyncpg** URL form:

```
DATABASE_URL=postgresql+asyncpg://<user>:<password>@<host>/<db>
```

(SSL is enabled automatically for `postgresql+asyncpg` URLs.) Tables are auto-created
on startup for the MVP; Alembic migrations are a later addition.
