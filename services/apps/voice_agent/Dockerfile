FROM python:3.11-slim

WORKDIR /app
COPY libs/platform_common /app/libs/platform_common
COPY services/apps/voice_agent /app/services/apps/voice_agent

WORKDIR /app/services/apps/voice_agent
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

EXPOSE 8013
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8013}"]
