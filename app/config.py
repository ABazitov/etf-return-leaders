"""User settings from config.toml (written by run.sh on first run)."""
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
YAHOO_USER_AGENT = "ETF-Return-Leaders/1.0"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    sec_contact_email: str
    openfigi_api_key: str
    senate_enabled: bool
    senate_headed: bool
    port: int
    data_dir: Path


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"{path} not found; run ./run.sh to create it") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} is not valid TOML: {e}") from e
    email = str(raw.get("sec_contact_email", "")).strip()
    if not _EMAIL.match(email):
        raise ConfigError("sec_contact_email must be a valid email address (the SEC requires one)")
    return Config(
        sec_contact_email=email,
        openfigi_api_key=str(raw.get("openfigi_api_key", "")).strip(),
        senate_enabled=bool(raw.get("senate_enabled", False)),
        senate_headed=bool(raw.get("senate_headed", False)),
        port=int(raw.get("port", 8765)),
        data_dir=PROJECT_ROOT / "data",
    )


def sec_user_agent(cfg: Config) -> str:
    return f"ETF-Return-Leaders/1.0 {cfg.sec_contact_email}"
