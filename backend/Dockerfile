FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 curl tzdata && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --prefer-binary --retries 10 --timeout 60 -r /app/backend/requirements.txt

COPY backend /app/backend
COPY web /app/web
WORKDIR /app/backend

RUN useradd -m -u 1000 yonexis && mkdir -p /app/data /sandbox_queue && chown -R yonexis /app /sandbox_queue
USER yonexis

EXPOSE 8000
CMD ["uvicorn", "yonexis.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
