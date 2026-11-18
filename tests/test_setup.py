"""Setup: the init wizard writes a config that loads, and doctor says what's wrong."""

import pytest

from inbox_sweep.cli import main
from inbox_sweep.config import load_config
from inbox_sweep.doctor import FAIL, OK, WARN, render, run_checks
from inbox_sweep.wizard import _set, interview, render_config, suggest


@pytest.mark.parametrize("email,provider,host,port,tls", [
    ("a@gmail.com", "gmail", "imap.gmail.com", 993, "ssl"),
    ("a@hotmail.co.uk", "outlook", "imap.hotmail.co.uk", 993, "ssl"),
    ("a@icloud.com", "imap", "imap.mail.me.com", 993, "ssl"),
    ("a@proton.me", "imap", "127.0.0.1", 1143, "starttls"),
    ("a@my-isp.example", "imap", "imap.my-isp.example", 993, "ssl"),
])
def test_suggest(email, provider, host, port, tls):
    s = suggest(email)
    assert s["provider"] == provider
    if provider == "imap":
        assert (s["imap_host"], s["imap_port"], s["imap_tls"]) == (host, port, tls)


def test_rendered_config_loads_with_every_answer(tmp_path):
    text = render_config(provider="imap", me=["me@proton.me", "old@gmail.com"], imap_host="127.0.0.1",
                         imap_port=1143, imap_tls="starttls", people=["Okonkwo", "mom@family.example"],
                         domains=["employer.example"], labels=["Taxes", "Family photos"],
                         keywords=["visa"], private=True)
    path = tmp_path / "sweep.toml"
    path.write_text(text, encoding="utf-8")
    cfg = load_config(path)
    assert cfg.backend == "imap" and cfg.me == ["me@proton.me", "old@gmail.com"]
    assert (cfg.imap_host, cfg.imap_port, cfg.imap_tls, cfg.imap_user) == ("127.0.0.1", 1143, "starttls",
                                                                           "me@proton.me")
    assert cfg.imap_verify is False  # local bridge, self-signed certificate
    assert cfg.protect_people == ["Okonkwo", "mom@family.example"]
    assert cfg.protect_domains == ["employer.example"] and cfg.protect_labels == ["Taxes", "Family photos"]
    assert cfg.protect_keywords == ["visa"] and cfg.private is True


def test_non_ascii_answers_survive(tmp_path):
    text = render_config(provider="gmail", me=["me@gmail.com"], people=["Müller", "Zoë", "王"])
    path = tmp_path / "sweep.toml"
    path.write_text(text, encoding="utf-8")
    assert load_config(path).protect_people == ["Müller", "Zoë", "王"]


def test_set_keeps_comments_and_rejects_unknown_keys():
    text = '[a]\nx = 1   # explain x\n\n[b]\nx = 2\n'
    assert _set(text, "b", "x", 5) == '[a]\nx = 1   # explain x\n\n[b]\nx = 5\n'
    assert _set(text, "a", "x", True) == '[a]\nx = true   # explain x\n\n[b]\nx = 2\n'
    with pytest.raises(ValueError):
        _set(text, "a", "nope", 1)


def test_interview_with_scripted_answers(tmp_path):
    answers = iter(["", "me@fastmail.com, me@old.example", "", "", "Okonkwo, mom@family.example",
                    "employer.example", "Taxes", "y"])
    said = []
    result = interview(ask=lambda _q: next(answers), say=said.append)
    assert result["me"] == ["me@fastmail.com", "me@old.example"]  # re-asked after a blank answer
    assert result["provider"] == "imap" and result["imap_host"] == "imap.fastmail.com"
    assert result["people"] == ["Okonkwo", "mom@family.example"] and result["private"] is True
    path = tmp_path / "sweep.toml"
    path.write_text(render_config(**result), encoding="utf-8")
    assert load_config(path).imap_host == "imap.fastmail.com"


def test_cli_init_with_flags_then_refuses_to_overwrite(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--me", "me@gmail.com", "--protect", "Okonkwo", "--domains", "employer.example"]) == 0
    cfg = load_config(tmp_path / "sweep.toml")
    assert cfg.backend == "gmail" and cfg.me == ["me@gmail.com"] and cfg.protect_people == ["Okonkwo"]
    assert main(["init", "--me", "x@gmail.com"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["init", "--me", "x@outlook.com", "--force"]) == 0
    assert load_config(tmp_path / "sweep.toml").backend == "outlook"


def test_cli_init_without_a_terminal_writes_the_template(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert main(["init"]) == 0
    assert load_config(tmp_path / "sweep.toml").backend == "gmail"


def _statuses(checks):
    return {c.name: c.status for c in checks}


def test_doctor_without_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checks = run_checks(None, online=False)
    assert _statuses(checks)["Config"] == FAIL
    assert main(["doctor", "--offline"]) == 1


def test_doctor_flags_placeholder_address_and_missing_password(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INBOX_SWEEP_IMAP_PASSWORD", raising=False)
    path = tmp_path / "sweep.toml"
    path.write_text(render_config(provider="imap", me=["you@example.com"], imap_host="imap.example.org"),
                    encoding="utf-8")
    s = _statuses(run_checks(str(path), online=False))
    assert s["Your addresses"] == FAIL and s["IMAP password"] == WARN and s["Index"] == WARN
    assert "problem(s) to fix" in render(run_checks(str(path), online=False))


def test_doctor_all_good_on_demo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--dir", "d"]) == 0
    path = tmp_path / "d" / "sweep.toml"
    path.write_text(_set(path.read_text(encoding="utf-8"), "account", "me", ["me@real.example"]), encoding="utf-8")
    path.chmod(0o600)
    s = _statuses(run_checks(str(path), online=False))
    assert s["Config"] == OK and s["Backend"] == OK and s["Index"] == OK and s["Your addresses"] == OK
    assert FAIL not in s.values()


def test_doctor_reports_a_bad_config_line(tmp_path):
    path = tmp_path / "sweep.toml"
    path.write_text("[backend]\ntype = 'carrier-pigeon'\n", encoding="utf-8")
    assert _statuses(run_checks(str(path), online=False))["Config"] == FAIL


@pytest.mark.parametrize("text,needle", [
    ("[backend\ntype = 'gmail'\n", "not valid TOML"),
    ("[privacy]\nprivate = \"false\"\n", "true or false"),
    ("[imap]\nport = \"nine\"\n", "wrong type"),
    ("account = \"me@x.example\"\n", "wrong type"),
    ("[retention]\npromotion = true\n", "integer"),
    ("[backend]\ntype = 'carrier-pigeon'\n", "backend.type"),
])
def test_bad_configs_give_a_clear_error_not_a_traceback(tmp_path, monkeypatch, capsys, text, needle):
    from inbox_sweep.config import ConfigError

    path = tmp_path / "sweep.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=needle):
        load_config(path)
    monkeypatch.chdir(tmp_path)
    assert main(["stats"]) == 2
    assert needle in capsys.readouterr().err
