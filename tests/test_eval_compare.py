import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "eval"))
import compare as c  # noqa: E402


def test_token_f1_identical_text_is_one():
    assert c.token_f1("the cat sat", "The cat sat!") == 1.0


def test_token_f1_no_overlap_or_empty_is_zero():
    assert c.token_f1("alpha beta", "gamma delta") == 0.0
    assert c.token_f1("", "gamma") == 0.0
    assert c.token_f1("alpha", "") == 0.0


def test_token_f1_partial_overlap():
    # answer has 2 tokens, reference 4, 2 overlap: precision 1.0, recall 0.5
    assert c.token_f1("red car", "a red car here") == pytest.approx(2 * 1.0 * 0.5 / 1.5)


def test_token_f1_counts_repeats_once_per_reference_occurrence():
    # "a a a" against "a b": only one "a" can match
    assert c.token_f1("a a a", "a b") == pytest.approx(2 * (1 / 3) * (1 / 2) / (1 / 3 + 1 / 2))


def test_summarize_counts_wins_ties_and_losses():
    def row(b, t):
        return {"base": {"answer": "x y", "f1": b}, "tuned": {"answer": "x y z", "f1": t}}

    s = c.summarize([row(0.1, 0.5), row(0.3, 0.3), row(0.6, 0.2)])
    assert s["prompts"] == 3
    assert (s["tuned_better"], s["ties"], s["base_better"]) == (1, 1, 1)
    assert s["base"]["mean_f1"] == pytest.approx(0.3333, abs=1e-4)
    assert s["tuned"]["mean_words"] == 3.0


def test_summarize_empty_does_not_divide_by_zero():
    s = c.summarize([])
    assert s["prompts"] == 0 and s["base"]["mean_f1"] is None


class FakeVLLM(BaseHTTPRequestHandler):
    answers = {"base": "paris is the capital", "tuned": "the capital of france is paris"}
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeVLLM.seen.append((self.path, body["model"], body["temperature"]))
        out = {"choices": [{"message": {"content": FakeVLLM.answers[body["model"]]}}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_end_to_end_against_a_local_server(tmp_path, capsys):
    FakeVLLM.seen.clear()
    server = HTTPServer(("127.0.0.1", 0), FakeVLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        heldout = tmp_path / "heldout.jsonl"
        heldout.write_text(
            json.dumps({"instruction": "capital of france?", "response": "the capital of france is paris"})
            + "\n"
            + json.dumps({"instruction": "again", "response": "paris"})
            + "\n"
        )
        out = tmp_path / "out.jsonl"
        c.main(
            [
                "--endpoint", f"http://127.0.0.1:{server.server_port}",
                "--heldout", str(heldout),
                "--limit", "2",
                "--out", str(out),
            ]
        )
    finally:
        server.shutdown()

    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["tuned"]["f1"] == 1.0 and lines[0]["base"]["f1"] < 1.0
    # both models were asked every prompt, greedy decoding, on the chat endpoint
    assert {m for _, m, _ in FakeVLLM.seen} == {"base", "tuned"}
    assert all(p == "/v1/chat/completions" and temp == 0 for p, _, temp in FakeVLLM.seen)
    printed = json.loads(capsys.readouterr().out.split("side-by-side")[0])
    assert printed["prompts"] == 2 and printed["tuned_better"] >= 1


def test_limit_caps_the_prompts_read(tmp_path):
    f = tmp_path / "h.jsonl"
    f.write_text("".join(json.dumps({"instruction": str(i), "response": "r"}) + "\n" for i in range(10)))
    assert len(c.load_heldout(str(f), 3)) == 3
