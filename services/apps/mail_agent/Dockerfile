FROM python:3.11-slim

WORKDIR /app
COPY libs/platform_common /app/libs/platform_common
COPY services/apps/mail_agent /app/services/apps/mail_agent

WORKDIR /app/services/apps/mail_agent
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

EXPOSE 8012
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8012}"]
