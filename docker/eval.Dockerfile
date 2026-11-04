FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app

WORKDIR /app

# Only what the scorer and its HTTP client need. This image is the release gate,
# so it stays small and reproducible rather than reusing the gateway image.
RUN pip install --no-cache-dir httpx==0.28.1 pyyaml==6.0.3

COPY src/eval /app/src/eval
COPY finetune /app/finetune
COPY config/eval-gates.yaml /app/config/eval-gates.yaml

USER 1001

ENTRYPOINT ["python", "-m", "eval"]
