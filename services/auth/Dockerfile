FROM python:3.11-slim

WORKDIR /app
COPY libs/platform_common /app/libs/platform_common
COPY services/auth /app/services/auth

WORKDIR /app/services/auth
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

EXPOSE 8001
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8001}"]
