"""Configuration loaded from environment variables (typically via a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(*names: str, default: str | None = None) -> str | None:
    """Return the first non-empty environment variable among `names`."""
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return default


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so the exporter also runs outside Docker without extra deps.

    Existing environment variables win over values from the file.
    """
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


@dataclass
class Config:
    url: str
    password: str
    # Xiaomi firmwares only have the `admin` account, but keep it configurable.
    username: str = "admin"
    name: str = ""
    verify_tls: bool = False
    timeout: float = 10.0
    listen_address: str = "0.0.0.0"
    listen_port: int = 9878
    cache_seconds: float = 30.0
    # Settings-type endpoints (Wi-Fi config, firewall, DHCP, ...) are re-read at most this often.
    slow_refresh_seconds: float = 300.0
    # Keep the session token between scrapes (re-login only when it expires).
    # Set to true to log out after every scrape instead.
    logout_after_scrape: bool = False
    # Per-client metrics (one series set per MAC address). Disable on busy networks.
    client_metrics: bool = True
    # e.g. socks5://user:pass@host:1080 (socks5h:// resolves DNS through the proxy)
    proxy: str = ""
    driver: str = "auto"  # auto | miwifi
    # Wait this long after a failed login before trying again (routers lock the login after a few failures).
    login_backoff_seconds: float = 300.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Config:
        url = _env("MODEM_URL", "URL")
        password = _env("MODEM_PASSWORD", "PASSWORD")
        missing = [n for n, v in (("MODEM_URL", url), ("MODEM_PASSWORD", password)) if v is None]
        if missing:
            raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")
        url = url.rstrip("/")
        if "://" not in url:
            url = "http://" + url
        return cls(
            url=url,
            password=password,
            username=_env("MODEM_USERNAME", "USERNAME", default="admin"),
            name=_env("MODEM_NAME", default="") or "",
            verify_tls=_bool(_env("VERIFY_TLS"), False),
            timeout=float(_env("SCRAPE_TIMEOUT", default="10")),
            listen_address=_env("LISTEN_ADDRESS", default="0.0.0.0"),
            listen_port=int(_env("LISTEN_PORT", default="9878")),
            cache_seconds=float(_env("CACHE_SECONDS", default="30")),
            slow_refresh_seconds=float(_env("SLOW_REFRESH_SECONDS", default="300")),
            logout_after_scrape=_bool(_env("LOGOUT_AFTER_SCRAPE"), False),
            client_metrics=_bool(_env("CLIENT_METRICS"), True),
            proxy=_env("MODEM_PROXY", "PROXY", default="") or "",
            driver=(_env("DRIVER", default="auto") or "auto").lower(),
            login_backoff_seconds=float(_env("LOGIN_BACKOFF_SECONDS", default="300")),
            log_level=(_env("LOG_LEVEL", default="INFO") or "INFO").upper(),
        )
