"""Building blocks shared by all drivers: HTTP session, metric accumulator, driver interface."""
from __future__ import annotations

import math

import requests
import urllib3
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .config import Config


class LoginError(RuntimeError):
    """Login was rejected. `retry_after` (seconds) may be set when the router says how long it blocks."""

    retry_after: float = 0.0


def new_session(cfg: Config) -> requests.Session:
    s = requests.Session()
    s.verify = cfg.verify_tls
    if not cfg.verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    if cfg.proxy:
        s.proxies = {"http": cfg.proxy, "https": cfg.proxy}
        s.trust_env = False
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) modem-exporter/1.0",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    return s


class Families:
    """Accumulates samples per metric, de-duplicating identical label sets.

    Samples of one metric may carry different label names; missing labels are
    filled with "" on output so every sample of a metric has the same label set.
    """

    def __init__(self, modem: str):
        self.modem = modem
        self.defs: dict[str, tuple[str, str, list[str]]] = {}
        self.samples: dict[str, dict[tuple, float]] = {}

    def add(self, name: str, help_: str, labels: dict[str, object], value, kind: str = "gauge") -> None:
        if value is None:
            return
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return
        labels = {"modem": self.modem, **{k: str(v)[:128] for k, v in labels.items()}}
        if name not in self.defs:
            self.defs[name] = (kind, help_, [])
            self.samples[name] = {}
        names = self.defs[name][2]
        names.extend(n for n in labels if n not in names)
        key = tuple(sorted(labels.items()))
        self.samples[name].setdefault(key, value)

    def families(self):
        for name, (kind, help_, names) in self.defs.items():
            cls = CounterMetricFamily if kind == "counter" else GaugeMetricFamily
            fam = cls(name, help_, labels=names)
            for key, value in self.samples[name].items():
                labels = dict(key)
                fam.add_metric([labels.get(n, "") for n in names], value)
            yield fam


class Driver:
    """Interface every router/modem driver implements."""

    name = "base"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @staticmethod
    def detect(session: requests.Session, cfg: Config) -> bool:
        """Return True if the device at cfg.url speaks this driver's API (no login required)."""
        raise NotImplementedError

    def scrape(self, fam: Families) -> int:
        """Add metrics to `fam`; return the number of API objects read (0 = failure)."""
        raise NotImplementedError

    def logout(self) -> None:
        pass

    def reset(self) -> None:
        """Forget the session after a connection error."""

    def dump(self) -> dict:
        """Raw (secret-masked) API responses, for the `discover` command."""
        raise NotImplementedError
