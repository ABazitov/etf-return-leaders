from pathlib import Path

import pytest

from app.config import ConfigError, load_config, sec_user_agent, YAHOO_USER_AGENT


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text)
    return p


def test_loads_full_config(tmp_path):
    cfg = load_config(write(tmp_path, '''
sec_contact_email = "someone@example.org"
openfigi_api_key = "abc"
senate_enabled = true
senate_headed = false
port = 8765
'''))
    assert cfg.sec_contact_email == "someone@example.org"
    assert cfg.openfigi_api_key == "abc"
    assert cfg.senate_enabled is True
    assert cfg.senate_headed is False
    assert cfg.port == 8765
    assert cfg.data_dir.name == "data"


def test_defaults_for_optional_fields(tmp_path):
    cfg = load_config(write(tmp_path, 'sec_contact_email = "a@b.co"\n'))
    assert cfg.openfigi_api_key == ""
    assert cfg.senate_enabled is False
    assert cfg.senate_headed is False
    assert cfg.port == 8765


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "a b@c.de"])
def test_rejects_bad_email(tmp_path, email):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, f'sec_contact_email = "{email}"\n'))


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.toml")


def test_user_agents(tmp_path):
    cfg = load_config(write(tmp_path, 'sec_contact_email = "a@b.co"\n'))
    assert sec_user_agent(cfg) == "ETF-Return-Leaders/1.0 a@b.co"
    assert YAHOO_USER_AGENT == "ETF-Return-Leaders/1.0"
    assert "Mozilla" not in YAHOO_USER_AGENT
