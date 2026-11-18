"""Review page, browser-mode queries, unsubscribe and the AI step (no network)."""

import json
from types import SimpleNamespace

import pytest

from inbox_sweep.ai import (
    AnthropicClassifier,
    OpenAICompatibleClassifier,
    build_groups,
    describe_egress,
    is_local_url,
    make_classifier,
    parse_response,
    run_ai,
)
from inbox_sweep.classify import classify
from inbox_sweep.config import CONFIG_TEMPLATE, load_config
from inbox_sweep.model import CATEGORY_PROMOTIONS
from inbox_sweep.queries import MAX_QUERY_CHARS, build_queries, label_query_name, or_groups
from inbox_sweep.review import build_review_data, write_review
from inbox_sweep.store import Store
from inbox_sweep.unsubscribe import candidates, is_safe_url, run

from conftest import NOW, make


def test_review_page_escapes_hostile_subjects(tmp_path, cfg):
    evil = make(sender="x@evil.example", subject="</script><script>alert(1)</script>", days_ago=900)
    data = build_review_data([evil], classify([evil], cfg, now=NOW), {}, "me@example.com")
    html = write_review(tmp_path / "r.html", data).read_text()
    payload = html.split('id="data">', 1)[1].split("</script>", 1)[0]
    assert "<script>" not in payload and "\\u003c/script\\u003e" in payload
    assert json.loads(payload)["senders"][0]["subjects"] == ["</script><script>alert(1)</script>"]


def test_queries_from_config(tmp_path):
    p = tmp_path / "sweep.toml"
    p.write_text(CONFIG_TEMPLATE.replace('me = ["you@example.com"]', 'me = ["you@gmail.com", "you@work.example"]')
                 .replace("domains = []", 'domains = ["work.example"]')
                 .replace('labels = []', 'labels = ["Taxes and Records"]'))
    q = build_queries(load_config(p))
    assert q["trash"] == "-label:keep -in:chats -in:drafts"
    first = q["protect"][0]["query"]
    assert first.startswith("from:(you@gmail.com OR you@work.example)") and "from:me" not in first
    assert any("label:taxes-and-records" in item["query"] for item in q["protect"])
    assert all(item["query"].endswith("-label:keep") for item in q["protect"])
    assert all(len(item["query"]) <= MAX_QUERY_CHARS + 120 for item in q["protect"])
    assert "in:trash is:starred" in q["verify"]


def test_or_groups_split_long_lists():
    values = [f"person{i}@example.org" for i in range(200)]
    groups = or_groups("from", values, " -label:keep", max_chars=500)
    assert len(groups) > 1 and all(len(g) <= 500 for g in groups)
    assert sum(g.count("@example.org") for g in groups) == 200
    assert label_query_name("Job Hunt/2024") == "job-hunt-2024"


def test_unsubscribe_candidates_and_safety(cfg):
    news = [make(sender="digest@news.example", subject="Weekly", bulk=True, list_id="<n>", days_ago=d,
                 lu="<mailto:u@news.example>, <https://news.example/u>", lup="List-Unsubscribe=One-Click")
            for d in (5, 300, 600)]
    shop = [make(sender="deals@shop.example", subject="Sale", labels=[CATEGORY_PROMOTIONS], bulk=True,
                 lu="<mailto:u@shop.example>", days_ago=400)]
    cfg.protect_people = ["friend@news.example"]
    cands = {c["sender"]: c for c in candidates(news + shop, classify(news + shop, cfg, now=NOW))}
    assert cands["digest@news.example"]["method"] == "one-click"  # recent mail doesn't block it
    assert cands["deals@shop.example"]["method"] == "mailto"
    assert is_safe_url("https://news.example/u")
    for bad in ("http://news.example/u", "https://127.0.0.1/u", "https://localhost/u",
                "https://user:pw@news.example/u", "https://intranet/u"):
        assert not is_safe_url(bad)


def test_unsubscribe_run_posts_one_click(tmp_path):
    calls = []

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class GoodOpener:
        def open(self, req, timeout):
            calls.append((req.full_url, req.get_method(), req.data))
            return Resp()

    cands = [{"sender": "a@x.example", "method": "one-click", "target": "https://x.example/u"},
             {"sender": "b@y.example", "method": "mailto", "target": "mailto:u@y.example"}]
    result = run(cands, tmp_path / "log.jsonl", opener=GoodOpener())
    assert result == {"ok": 1, "failed": 0, "manual": 1}
    assert calls == [("https://x.example/u", "POST", b"List-Unsubscribe=One-Click")]


class FakeClient:
    def __init__(self, verdicts=None, refuse=False):
        self.verdicts = verdicts or {}
        self.refuse = refuse
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        groups = json.loads(kwargs["messages"][0]["content"])["groups"]
        if self.refuse:
            return SimpleNamespace(stop_reason="refusal", content=[])
        out = {"decisions": [{"id": g["id"], "action": self.verdicts.get(g["sender"], "review"),
                              "category": "misc", "reason": "test"} for g in groups]}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))])


def test_ai_groups_only_ambiguous_and_caches(tmp_path, cfg):
    cfg.data_dir = str(tmp_path)
    msgs = [make(sender="pat@vendor.example", subject="Following up", days_ago=900),
            make(sender="deals@shop.example", subject="Sale", labels=[CATEGORY_PROMOTIONS], bulk=True, days_ago=900),
            make(sender="grandma@gmail.com", subject="Hi", days_ago=900)]
    decisions = classify(msgs, cfg, now=NOW)
    groups = build_groups(msgs, decisions, set(), {})
    assert [g["sender"] for g in groups] == ["pat@vendor.example"]

    store = Store(cfg.db_path)
    client = FakeClient({"pat@vendor.example": "trash"})
    classifier = AnthropicClassifier("claude-opus-5-5", client=client)
    assert run_ai(store, cfg, msgs, decisions, classifier=classifier) == 1
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert "Following up" in req["messages"][0]["content"]  # subjects, never bodies
    assert store.ai_decisions()["pat@vendor.example"]["action"] == "trash"
    # Second run: cached, nothing re-sent.
    assert run_ai(store, cfg, msgs, decisions, classifier=classifier) == 0
    store.close()


def test_local_provider_is_default_and_stays_on_this_machine(tmp_path, cfg):
    assert cfg.ai_provider == "local"
    assert isinstance(make_classifier(cfg), OpenAICompatibleClassifier)
    assert "nothing leaves this machine" in describe_egress(cfg)
    with pytest.raises(RuntimeError):
        OpenAICompatibleClassifier("https://api.example.com/v1", "m", local_only=True)
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:1234/v1", "http://[::1]:8080/v1"):
        assert is_local_url(url)
    cfg.ai_provider, cfg.ai_base_url = "openai-compatible", "https://api.example.com/v1"
    assert "api.example.com" in describe_egress(cfg)


def test_openai_compatible_protocol_and_json_mode_fallback(tmp_path, cfg):
    calls = []

    def post(url, body, headers):
        calls.append((url, body, headers))
        if body["response_format"]["type"] == "json_schema":
            return 400, {"error": "json_schema unsupported"}
        groups = json.loads(body["messages"][1]["content"])["groups"]
        content = json.dumps({"decisions": [{"id": g["id"], "action": "trash", "category": "promo", "reason": "r"}
                                            for g in groups]})
        return 200, {"choices": [{"message": {"content": "```json\n" + content + "\n```"}}]}

    clf = OpenAICompatibleClassifier("http://localhost:11434/v1/", "llama3.1:8b", api_key="k", post=post)
    out = clf.classify([{"id": "g0", "sender": "a@x.example"}])
    assert out == {"a@x.example": {"action": "trash", "category": "promo", "reason": "r"}}
    assert calls[0][0] == "http://localhost:11434/v1/chat/completions"
    assert calls[0][1]["model"] == "llama3.1:8b" and calls[0][2]["Authorization"] == "Bearer k"
    assert calls[1][1]["response_format"] == {"type": "json_object"}


def test_ai_refusal_and_unknown_ids_are_ignored():
    groups = [{"id": "g0", "sender": "a@x.example"}]
    assert parse_response(SimpleNamespace(stop_reason="refusal", content=[]), groups) == {}
    bogus = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(
        type="text", text=json.dumps({"decisions": [{"id": "g9", "action": "trash", "category": "x", "reason": "y"}]}))])
    assert parse_response(bogus, groups) == {}


def test_example_config_matches_template_and_loads():
    from pathlib import Path
    example = Path(__file__).resolve().parent.parent / "sweep.example.toml"
    assert example.read_text() == CONFIG_TEMPLATE
    cfg = load_config(example)
    assert cfg.me == ["you@example.com"] and cfg.retention["documents"] == -1
