"""Settings, read from the environment (and a local .env file if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Settings:
    rep_name: str
    rep_email: str
    company_name: str
    hubspot_token: str
    hubspot_owner_id: str
    fathom_api_key: str
    google_client_secrets: Path
    google_token_file: Path
    force_mock: bool
    playbook_path: Path

    @property
    def hubspot_live(self) -> bool:
        return bool(self.hubspot_token) and not self.force_mock

    @property
    def fathom_live(self) -> bool:
        return bool(self.fathom_api_key) and not self.force_mock

    @property
    def google_live(self) -> bool:
        have_file = self.google_client_secrets.exists() or self.google_token_file.exists()
        return have_file and not self.force_mock


def get_settings() -> Settings:
    load_dotenv()
    env = os.environ.get

    def path(name: str, default: str) -> Path:
        p = Path(env(name) or default)
        return p if p.is_absolute() else ROOT / p

    return Settings(
        rep_name=env("REP_NAME", ""),
        rep_email=env("REP_EMAIL", ""),
        company_name=env("COMPANY_NAME", ""),
        hubspot_token=env("HUBSPOT_ACCESS_TOKEN", ""),
        hubspot_owner_id=env("HUBSPOT_OWNER_ID", ""),
        fathom_api_key=env("FATHOM_API_KEY", ""),
        google_client_secrets=path("GOOGLE_CLIENT_SECRETS", "credentials.json"),
        google_token_file=path("GOOGLE_TOKEN_FILE", "token.json"),
        force_mock=env("SALES_ASSISTANT_MOCK", "") not in ("", "0", "false"),
        playbook_path=path("SALES_PLAYBOOK", "playbook.md"),
    )
