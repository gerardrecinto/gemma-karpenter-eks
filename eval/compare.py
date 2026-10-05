"""Send the same held-out prompts to the base model and the tuned adapter and
compare their answers with the reference answers.

The metric is unigram F1 between an answer and the reference. It is cheap and
easy to explain, and it is a weak proxy for quality: it rewards word overlap,
not correctness. Read the side-by-side file before trusting the number.
"""

import argparse
import json
import re
import sys
import urllib.request
from collections import Counter

TOKEN = re.compile(r"\w+")


def tokens(text):
    return TOKEN.findall(text.lower())


def token_f1(answer, reference):
    """Unigram F1 between two texts, 0.0 when either is empty or nothing overlaps."""
    a, r = tokens(answer), tokens(reference)
    if not a or not r:
        return 0.0
    overlap = sum((Counter(a) & Counter(r)).values())
    if overlap == 0:
        return 0.0
    precision, recall = overlap / len(a), overlap / len(r)
    return 2 * precision * recall / (precision + recall)


def load_heldout(path, limit):
    rows = []
    with open(path) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
            if len(rows) >= limit:
                break
    return rows


def ask(endpoint, model, prompt, max_tokens, timeout):
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["choices"][0]["message"]["content"]


def summarize(rows):
    """Mean score and mean answer length for each model, over rows that have both."""
    out = {}
    for model in ("base", "tuned"):
        scores = [r[model]["f1"] for r in rows]
        words = [len(tokens(r[model]["answer"])) for r in rows]
        out[model] = {
            "mean_f1": round(sum(scores) / len(scores), 4) if scores else None,
            "mean_words": round(sum(words) / len(words), 1) if words else None,
        }
    out["prompts"] = len(rows)
    if rows:
        wins = sum(1 for r in rows if r["tuned"]["f1"] > r["base"]["f1"])
        ties = sum(1 for r in rows if r["tuned"]["f1"] == r["base"]["f1"])
        out["tuned_better"] = wins
        out["ties"] = ties
        out["base_better"] = len(rows) - wins - ties
    return out


def run(endpoint, heldout, limit, max_tokens, timeout):
    rows = []
    for item in load_heldout(heldout, limit):
        row = {"instruction": item["instruction"], "reference": item["response"]}
        for model in ("base", "tuned"):
            answer = ask(endpoint, model, item["instruction"], max_tokens, timeout)
            row[model] = {"answer": answer, "f1": token_f1(answer, item["response"])}
        rows.append(row)
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--endpoint", required=True, help="e.g. http://localhost:8000")
    p.add_argument("--heldout", required=True, help="heldout.jsonl written by training")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--out", default="eval-results.jsonl")
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    rows = run(args.endpoint, args.heldout, args.limit, args.max_tokens, args.timeout)
    with open(args.out, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(json.dumps(summarize(rows), indent=2))
    print(f"side-by-side answers written to {args.out}")


if __name__ == "__main__":
    main()
