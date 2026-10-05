import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "train"))
import train_lora as t  # noqa: E402


def rows(n, with_context_every=5):
    out = []
    for i in range(n):
        out.append(
            {
                "instruction": f"question {i}",
                "response": f"answer {i}",
                "context": "some passage" if i % with_context_every == 0 else "",
            }
        )
    return out


def test_split_drops_rows_with_context_or_missing_text():
    data = rows(50) + [
        {"instruction": "", "response": "x", "context": ""},
        {"instruction": "q", "response": "  ", "context": ""},
    ]
    train, held = t.split_examples(data, 10, 5, seed=1)
    assert len(train) == 10 and len(held) == 5
    for e in train + held:
        assert e["instruction"] and e["response"]
        # every kept row came from a row with no context
        assert int(e["instruction"].split()[-1]) % 5 != 0


def test_split_is_deterministic_and_disjoint():
    data = rows(100)
    a_train, a_held = t.split_examples(data, 20, 10, seed=7)
    b_train, b_held = t.split_examples(data, 20, 10, seed=7)
    assert a_train == b_train and a_held == b_held
    assert not {e["instruction"] for e in a_train} & {e["instruction"] for e in a_held}


def test_different_seed_gives_a_different_split():
    data = rows(100)
    a, _ = t.split_examples(data, 20, 10, seed=1)
    b, _ = t.split_examples(data, 20, 10, seed=2)
    assert a != b


def test_split_refuses_when_there_are_too_few_rows():
    with pytest.raises(ValueError, match="need 30 usable rows"):
        t.split_examples(rows(10), 20, 10, seed=1)


class FakeTokenizer:
    def apply_chat_template(self, messages, tokenize):
        assert tokenize is False
        return "|".join(f"{m['role']}:{m['content']}" for m in messages)


def test_format_example_uses_the_chat_template_with_both_turns():
    text = t.format_example(FakeTokenizer(), {"instruction": "hi", "response": "hello"})
    assert text == "user:hi|assistant:hello"


def test_split_s3_uri():
    assert t.split_s3_uri("s3://bkt/adapters") == ("bkt", "adapters")
    assert t.split_s3_uri("s3://bkt/a/b/") == ("bkt", "a/b")
    assert t.split_s3_uri("s3://bkt") == ("bkt", "")


@pytest.mark.parametrize("bad", ["bkt/adapters", "https://x/y", "s3://", "s3:///x"])
def test_split_s3_uri_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        t.split_s3_uri(bad)


class FakeS3:
    def __init__(self):
        self.uploads = []

    def upload_file(self, path, bucket, key):
        self.uploads.append((os.path.basename(path), bucket, key))


def test_upload_dir_keeps_relative_paths(tmp_path):
    (tmp_path / "adapter_config.json").write_text("{}")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "w.bin").write_text("x")
    client = FakeS3()
    n = t.upload_dir(client, str(tmp_path), "bkt", "adapters/latest")
    assert n == 2
    keys = {k for _, _, k in client.uploads}
    assert keys == {"adapters/latest/adapter_config.json", "adapters/latest/sub/w.bin"}


def test_parse_args_requires_output_and_run_id():
    with pytest.raises(SystemExit):
        t.parse_args([])
    a = t.parse_args(["--output", "s3://b/p", "--run-id", "1"])
    assert a.max_train == 1000 and a.heldout == 100 and a.base_model == "google/gemma-2-2b-it"
