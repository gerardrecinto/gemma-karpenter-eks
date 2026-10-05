"""LoRA fine-tune of a Gemma model on a small instruction dataset.

Writes the adapter, the held-out prompts and a small metrics file to S3. The
heavy imports (torch, transformers) happen inside main() so the data helpers
can be tested on a machine without a GPU.
"""

import argparse
import json
import os
import random
import sys

DATASET = "databricks/databricks-dolly-15k"


def split_examples(rows, max_train, heldout, seed):
    """Keep rows that have an instruction and a response and no context, then
    shuffle with a fixed seed and split into train and held-out lists.

    The split is deterministic, so a run can be repeated and the held-out
    prompts never overlap the training prompts.
    """
    usable = [
        {"instruction": r["instruction"].strip(), "response": r["response"].strip()}
        for r in rows
        if r.get("instruction", "").strip()
        and r.get("response", "").strip()
        and not r.get("context", "").strip()
    ]
    random.Random(seed).shuffle(usable)
    if len(usable) < max_train + heldout:
        raise ValueError(f"need {max_train + heldout} usable rows, found {len(usable)}")
    return usable[:max_train], usable[max_train : max_train + heldout]


def format_example(tokenizer, example):
    """One training text in the model's own chat format."""
    messages = [
        {"role": "user", "content": example["instruction"]},
        {"role": "assistant", "content": example["response"]},
    ]
    return tokenizer.apply_chat_template(messages, tokenize=False)


def split_s3_uri(uri):
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3 uri: {uri}")
    bucket, _, prefix = uri[len("s3://") :].partition("/")
    if not bucket:
        raise ValueError(f"no bucket in: {uri}")
    return bucket, prefix.strip("/")


def upload_dir(client, local_dir, bucket, prefix):
    """Upload every file under local_dir to bucket/prefix, keeping relative paths."""
    count = 0
    for root, _, files in os.walk(local_dir):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, local_dir)
            client.upload_file(path, bucket, f"{prefix}/{rel}")
            count += 1
    return count


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-model", default="google/gemma-2-2b-it")
    p.add_argument("--max-train", type=int, default=1000)
    p.add_argument("--heldout", type=int, default=100)
    p.add_argument("--epochs", type=float, default=1.0)
    p.add_argument("--max-length", type=int, default=512)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", required=True, help="s3://bucket/adapters")
    p.add_argument("--run-id", required=True)
    p.add_argument("--workdir", default="/tmp/run")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    bucket, prefix = split_s3_uri(args.output)

    import boto3
    import torch
    from datasets import load_dataset
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
    )

    train_rows, heldout_rows = split_examples(
        load_dataset(DATASET, split="train"), args.max_train, args.heldout, args.seed
    )

    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    model = AutoModelForCausalLM.from_pretrained(args.base_model, torch_dtype=torch.bfloat16)
    model = get_peft_model(
        model,
        LoraConfig(
            r=16,
            lora_alpha=32,
            lora_dropout=0.05,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
            task_type="CAUSAL_LM",
        ),
    )
    model.print_trainable_parameters()

    def encode(example):
        text = format_example(tokenizer, example)
        return tokenizer(text, truncation=True, max_length=args.max_length)

    from datasets import Dataset

    train_ds = Dataset.from_list(train_rows).map(encode, remove_columns=["instruction", "response"])

    out_dir = os.path.join(args.workdir, "adapter")
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=os.path.join(args.workdir, "checkpoints"),
            num_train_epochs=args.epochs,
            per_device_train_batch_size=4,
            gradient_accumulation_steps=4,
            learning_rate=2e-4,
            bf16=True,
            logging_steps=10,
            save_strategy="no",
            report_to=[],
            seed=args.seed,
        ),
        train_dataset=train_ds,
        # Loss is taken over the whole text, prompt included. Masking the
        # prompt is a common refinement that this small run does not do.
        data_collator=DataCollatorForLanguageModeling(tokenizer, mlm=False),
    )
    result = trainer.train()
    model.save_pretrained(out_dir)

    with open(os.path.join(out_dir, "heldout.jsonl"), "w") as f:
        for row in heldout_rows:
            f.write(json.dumps(row) + "\n")
    with open(os.path.join(out_dir, "metrics.json"), "w") as f:
        json.dump(
            {
                "base_model": args.base_model,
                "dataset": DATASET,
                "train_examples": len(train_rows),
                "heldout_examples": len(heldout_rows),
                "epochs": args.epochs,
                "train_loss": result.training_loss,
                "seed": args.seed,
            },
            f,
            indent=2,
        )

    client = boto3.client("s3")
    # A numbered copy for the record, and "latest" for the serving pod to fetch.
    for name in (f"run-{args.run_id}", "latest"):
        n = upload_dir(client, out_dir, bucket, f"{prefix}/{name}")
        print(f"uploaded {n} files to s3://{bucket}/{prefix}/{name}")


if __name__ == "__main__":
    main()
