"""Runtime configuration, read from environment variables (and optional .env files)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_CALLBACK_URL = "https://127.0.0.1:8182"


def config_dir() -> Path:
    """Per-user config directory, e.g. ``~/.config/schwab-mcp``."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "schwab-mcp"


def load_env_files(explicit: str | os.PathLike[str] | None = None) -> list[Path]:
    """Load .env files without overriding variables that are already set.

    Order (first one wins for any given key): real environment, ``explicit`` /
    ``$SCHWAB_MCP_ENV_FILE``, ``./.env``, ``<config_dir>/.env``.
    """
    candidates: list[Path] = []
    explicit = explicit or os.environ.get("SCHWAB_MCP_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates += [Path.cwd() / ".env", config_dir() / ".env"]

    loaded: list[Path] = []
    for path in candidates:
        if path.is_file():
            load_dotenv(path, override=False)
            loaded.append(path)
    return loaded


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    client_id: str | None
    client_secret: str | None
    callback_url: str = DEFAULT_CALLBACK_URL
    token_path: Path = config_dir() / "token.json"
    mask_accounts: bool = True
    http_token: str | None = None
    request_timeout: float = 20.0

    @classmethod
    def from_env(cls) -> Settings:
        token_path = os.environ.get("SCHWAB_TOKEN_PATH")
        return cls(
            client_id=os.environ.get("SCHWAB_CLIENT_ID") or None,
            client_secret=os.environ.get("SCHWAB_CLIENT_SECRET") or None,
            callback_url=os.environ.get("SCHWAB_CALLBACK_URL") or DEFAULT_CALLBACK_URL,
            token_path=Path(token_path).expanduser() if token_path else config_dir() / "token.json",
            mask_accounts=_env_bool("SCHWAB_MCP_MASK_ACCOUNTS", True),
            http_token=os.environ.get("SCHWAB_MCP_HTTP_TOKEN") or None,
            request_timeout=float(os.environ.get("SCHWAB_MCP_TIMEOUT") or 20.0),
        )

    def require_credentials(self) -> tuple[str, str]:
        if not self.client_id or not self.client_secret:
            raise ConfigError(
                "SCHWAB_CLIENT_ID and SCHWAB_CLIENT_SECRET must be set (the App Key and Secret of your app "
                "on https://developer.schwab.com). Put them in the environment, in ./.env, or in "
                f"{config_dir() / '.env'}."
            )
        return self.client_id, self.client_secret


class ConfigError(RuntimeError):
    pass
