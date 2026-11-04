FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY src/tiersim/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/tiersim /app/tiersim

EXPOSE 8001

CMD ["uvicorn", "tiersim.app:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
