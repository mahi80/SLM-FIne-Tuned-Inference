# Training image: reuses the pinned serving runtime so torch, CUDA and the
# tokenizer stack are the same artifacts the adapters will be served on.
FROM vllm/vllm-openai:v0.33.0

RUN pip install --no-cache-dir peft==0.21.2 bitsandbytes==0.50.2

WORKDIR /app
COPY finetune /app/finetune

ENV PYTHONPATH=/app \
    HF_HOME=/root/.cache/huggingface \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python3"]
