FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY src/gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/gateway /app/gateway

EXPOSE 8000

CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
