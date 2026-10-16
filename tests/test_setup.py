import tomllib

from app.config import load_config
from app.setup import run_setup


def answers(*xs):
    it = iter(xs)
    return lambda prompt: next(it)


def test_setup_writes_valid_config(tmp_path):
    p = tmp_path / "config.toml"
    run_setup(p, answers("bad", "me@example.org", "", "yes"))
    cfg = load_config(p)
    assert cfg.sec_contact_email == "me@example.org" and cfg.openfigi_api_key == "" and cfg.senate_enabled


def test_senate_disabled_unless_yes(tmp_path):
    p = tmp_path / "config.toml"
    run_setup(p, answers("me@example.org", "KEY", "no"))
    raw = tomllib.loads(p.read_text())
    assert raw["senate_enabled"] is False and raw["openfigi_api_key"] == "KEY"
