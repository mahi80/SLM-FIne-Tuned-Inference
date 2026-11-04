"""QLoRA fine-tuning of the Qwen3-4B base for support-ticket triage.

The 4-bit NF4 base is loaded from the official BF16 weights, not from the AWQ
artifact: bitsandbytes quantises during load, and the resulting adapter is
quantisation-agnostic, so it also loads onto the AWQ weights served by vLLM.

Run inside the training image (see docker/train.Dockerfile), e.g.:

    python3 -m finetune.train_qlora \
        --data finetune/data/train-v1.jsonl --out adapters/triage-v1 --epochs 4

Loss is computed on the assistant answer only; the system prompt and ticket are
masked out.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from pathlib import Path

import torch
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    get_cosine_schedule_with_warmup,
)

from finetune.prompt import prompt_messages


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-model", default="Qwen/Qwen3-4B-Instruct-2507")
    ap.add_argument("--base-revision", default="cdbee75f17c01a7cc42f958dc650907174af0554")
    ap.add_argument("--epochs", type=float, default=4.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--dropout", type=float, default=0.05)
    ap.add_argument("--max-seq-len", type=int, default=768)
    ap.add_argument("--grad-accum", type=int, default=4)
    ap.add_argument("--warmup-ratio", type=float, default=0.05)
    ap.add_argument("--max-steps", type=int, default=0, help="0 = derive from epochs")
    ap.add_argument("--limit", type=int, default=0, help="debug: use the first N rows")
    ap.add_argument("--seed", type=int, default=13)
    return ap.parse_args()


def encode_example(tokenizer, ticket: str, target: dict, max_len: int):
    messages = prompt_messages(ticket)
    answer = json.dumps(target, ensure_ascii=False)
    full_text = tokenizer.apply_chat_template(
        messages + [{"role": "assistant", "content": answer}], tokenize=False
    )
    prefix_text = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    if not full_text.startswith(prefix_text):
        raise ValueError("chat template prefix mismatch; refusing to guess the mask")

    ids = tokenizer(full_text, add_special_tokens=False)["input_ids"]
    prefix_len = len(tokenizer(prefix_text, add_special_tokens=False)["input_ids"])
    if len(ids) > max_len:
        raise ValueError(f"sample longer than {max_len} tokens ({len(ids)})")
    labels = [-100] * prefix_len + ids[prefix_len:]
    return torch.tensor(ids), torch.tensor(labels)


def main() -> None:
    args = parse_args()
    random.seed(args.seed)
    torch.manual_seed(args.seed)

    rows = [json.loads(line) for line in Path(args.data).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        rows = rows[: args.limit]

    tokenizer = AutoTokenizer.from_pretrained(args.base_model, revision=args.base_revision)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    examples = []
    skipped = 0
    for row in rows:
        try:
            examples.append(encode_example(tokenizer, row["ticket"], row["target"], args.max_seq_len))
        except ValueError:
            skipped += 1
    if not examples:
        raise SystemExit("no usable examples")
    lengths = [len(x[0]) for x in examples]
    print(f"examples {len(examples)} skipped {skipped} tokens min/mean/max "
          f"{min(lengths)}/{sum(lengths)//len(lengths)}/{max(lengths)}", flush=True)

    quant = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        revision=args.base_revision,
        quantization_config=quant,
        device_map={"": 0},
        attn_implementation="sdpa",
    )
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model.config.use_cache = False
    model = get_peft_model(
        model,
        LoraConfig(
            r=args.rank,
            lora_alpha=args.alpha,
            lora_dropout=args.dropout,
            target_modules="all-linear",
            bias="none",
            task_type="CAUSAL_LM",
        ),
    )
    model.print_trainable_parameters()

    trainable = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=args.lr, weight_decay=0.0, betas=(0.9, 0.95))
    steps_per_epoch = math.ceil(len(examples) / args.grad_accum)
    total_steps = args.max_steps or math.ceil(steps_per_epoch * args.epochs)
    scheduler = get_cosine_schedule_with_warmup(
        optimizer,
        num_warmup_steps=max(1, int(total_steps * args.warmup_ratio)),
        num_training_steps=total_steps,
    )

    order = list(range(len(examples)))
    step = 0
    micro = 0
    running = 0.0
    started = time.time()
    curve = []
    model.train()

    while step < total_steps:
        random.shuffle(order)
        for idx in order:
            ids, labels = examples[idx]
            ids = ids.unsqueeze(0).to("cuda")
            labels = labels.unsqueeze(0).to("cuda")
            loss = model(input_ids=ids, labels=labels).loss
            (loss / args.grad_accum).backward()
            running += loss.item()
            micro += 1
            if micro % args.grad_accum:
                continue

            torch.nn.utils.clip_grad_norm_(trainable, 1.0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad(set_to_none=True)
            step += 1
            if step % 10 == 0 or step == 1:
                elapsed = time.time() - started
                curve.append({"step": step, "loss": running / args.grad_accum, "lr": scheduler.get_last_lr()[0]})
                print(f"step {step}/{total_steps} loss {running/args.grad_accum:.4f} "
                      f"lr {scheduler.get_last_lr()[0]:.2e} {step/elapsed:.2f} step/s", flush=True)
            running = 0.0
            if step >= total_steps:
                break

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out, safe_serialization=True)
    (out / "training.json").write_text(json.dumps({
        "base_model": args.base_model,
        "base_revision": args.base_revision,
        "data": str(args.data),
        "rows": len(examples),
        "skipped": skipped,
        "epochs": args.epochs,
        "steps": step,
        "lr": args.lr,
        "rank": args.rank,
        "alpha": args.alpha,
        "dropout": args.dropout,
        "max_seq_len": args.max_seq_len,
        "grad_accum": args.grad_accum,
        "seed": args.seed,
        "duration_s": round(time.time() - started, 1),
        "loss_curve": curve,
        "torch": torch.__version__,
    }, indent=2), encoding="utf-8")
    print(f"saved adapter to {out} in {time.time()-started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
