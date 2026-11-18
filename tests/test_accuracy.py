"""Accuracy against labeled mail: the corpus, plus the eval plumbing people use on their own mail."""

import csv
import json

import pytest

from inbox_sweep.classify import classify
from inbox_sweep.cli import main
from inbox_sweep.corpus import corpus_config, labeled_corpus
from inbox_sweep.evaluate import evaluate, export_sample, truth_from_file, truth_from_label

from conftest import NOW, make


@pytest.mark.parametrize("seed", [11, 1, 2, 3, 4])
def test_corpus_never_trashes_mail_worth_keeping(seed):
    messages, truth, scenario = labeled_corpus(now=NOW, seed=seed)
    report = evaluate(messages, corpus_config(), truth, now=NOW, scenario=scenario)
    assert report["scored"] == len(truth) > 200
    assert report["false_trash"] == 0, report["false_trash_senders"]
    assert report["clutter_rate"] < 0.05, report["clutter_senders"]


def test_every_corpus_scenario_is_scored():
    messages, truth, scenario = labeled_corpus(now=NOW)
    report = evaluate(messages, corpus_config(), truth, now=NOW, scenario=scenario)
    assert set(report["by_scenario"]) == set(scenario.values())
    assert len(report["by_scenario"]) >= 25


def test_truth_label_is_hidden_from_the_classifier(cfg):
    newsletter = make("news@list.example", "Weekly digest", 900, bulk=True, labels=["Keep"])
    cfg.protect_labels = ["Keep"]
    truth = truth_from_label([newsletter], "Keep")
    assert truth == {newsletter.id: "keep"}
    # Seeing the label would make this trivially "keep"; hidden, the rules decide.
    hidden = evaluate([newsletter], cfg, truth, hide_label="Keep", now=NOW)
    assert hidden["false_trash"] == 1
    assert cfg.protect_labels == ["Keep"]  # the caller's config is untouched
    assert evaluate([newsletter], cfg, truth, now=NOW)["false_trash"] == 0


def test_truth_from_decisions_json_and_csv(tmp_path):
    a = make("a@shop.example", "Sale")
    b = make("b@shop.example", "Sale")
    c = make("friend@gmail.com", "Hi")
    j = tmp_path / "decisions.json"
    j.write_text(json.dumps({"version": 1, "overrides": {"@shop.example": "trash", "friend@gmail.com": "keep",
                                                         "x@y.example": "maybe"}}), encoding="utf-8")
    assert truth_from_file(j, [a, b, c]) == {a.id: "trash", b.id: "trash", c.id: "keep"}
    s = tmp_path / "sample.csv"
    with s.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "truth"])
        w.writerows([[a.id, "keep"], [b.id, " Trash "], [c.id, ""], ["", "keep"]])
    assert truth_from_file(s, [a, b, c]) == {a.id: "keep", b.id: "trash"}


def test_export_sample_is_stratified_and_reproducible(tmp_path, cfg):
    messages, _, _ = labeled_corpus(now=NOW)
    decisions = classify(messages, cfg, now=NOW)
    strata = {(d.category, d.action) for d in decisions}
    n = export_sample(messages, decisions, 200, tmp_path / "a.csv")
    export_sample(messages, decisions, 200, tmp_path / "b.csv")
    rows = list(csv.DictReader((tmp_path / "a.csv").open(encoding="utf-8")))
    assert n == len(rows) <= 200
    assert {(r["category"], r["proposed"]) for r in rows} == strata  # every stratum represented
    assert (tmp_path / "a.csv").read_text(encoding="utf-8") == (tmp_path / "b.csv").read_text(encoding="utf-8")


def test_cli_eval_corpus_gate(capsys):
    assert main(["eval", "--corpus", "--max-false-trash", "0"]) == 0
    assert main(["eval", "--corpus", "--max-false-trash", "-1"]) == 1  # the gate can fail


def test_cli_eval_corpus_json_is_parseable(capsys):
    assert main(["eval", "--corpus", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["false_trash"] == 0 and report["scored"] > 200


def test_cli_eval_on_an_index_with_a_truth_file(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0
    (tmp_path / "t.json").write_text(json.dumps({"overrides": {"mom.demo@gmail.com": "keep"}}), encoding="utf-8")
    capsys.readouterr()
    assert main(["--config", "d/sweep.toml", "eval", "--truth-file", "t.json", "--max-false-trash", "0"]) == 0
    assert main(["--config", "d/sweep.toml", "eval", "--export-sample", "30", "--out", "s.csv"]) == 0
    assert (tmp_path / "s.csv").exists()
