FROM python:3.11-slim

WORKDIR /app
COPY libs/platform_common /app/libs/platform_common
COPY services/agent_factory /app/services/agent_factory

WORKDIR /app/services/agent_factory
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

EXPOSE 8002
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8002}"]
