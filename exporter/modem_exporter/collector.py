"""Prometheus collector: picks a driver, scrapes it, caches the result, adds self-monitoring metrics."""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlparse

import requests
from prometheus_client.registry import Collector

from .base import Driver, Families, LoginError, new_session
from .config import Config
from .miwifi import MiWifiDriver

log = logging.getLogger(__name__)

# Add new router/modem families here. `auto` tries them in order.
DRIVERS: dict[str, type[Driver]] = {
    MiWifiDriver.name: MiWifiDriver,
}


def select_driver(cfg: Config) -> Driver | None:
    if cfg.driver != "auto":
        cls = DRIVERS.get(cfg.driver)
        if not cls:
            raise SystemExit(f"Unknown DRIVER={cfg.driver!r}; choose one of: auto, {', '.join(DRIVERS)}")
        return cls(cfg)
    session = new_session(cfg)
    for cls in DRIVERS.values():
        if cls.detect(session, cfg):
            return cls(cfg)
    session.get(cfg.url, timeout=cfg.timeout)  # unreachable -> RequestException, not "unknown device"
    return None


class ModemCollector(Collector):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.modem = cfg.name or urlparse(cfg.url).hostname or cfg.url
        self.lock = threading.Lock()
        self.cached: list = []
        self.cached_at = 0.0
        self.scrapes_total = 0
        self.errors_total = 0
        self.driver: Driver | None = None
        self.login_blocked_until = 0.0

    def scrape(self) -> list:
        fam = Families(self.modem)
        start = time.time()
        up, count = 0, 0
        if time.time() < self.login_blocked_until:
            log.info("Login backoff active for %.0fs more; not contacting the router", self.login_blocked_until - time.time())
        else:
            try:
                if self.driver is None:
                    self.driver = select_driver(self.cfg)
                    if self.driver:
                        log.info("Using %s driver for %s", self.driver.name, self.cfg.url)
                if self.driver is None:
                    log.error("%s answers, but no driver recognises it (DRIVER=auto tried: %s)",
                              self.cfg.url, ", ".join(DRIVERS))
                    self.errors_total += 1
                else:
                    count = self.driver.scrape(fam)
                    up = 1 if count else 0
            except LoginError as exc:
                wait = max(exc.retry_after, self.cfg.login_backoff_seconds)
                self.login_blocked_until = time.time() + wait
                log.error("Login failed: %s (next attempt in %.0fs)", exc, wait)
                self.errors_total += 1
            except requests.RequestException as exc:
                log.error("Router unreachable: %s", exc)
                self.errors_total += 1
                if self.driver:
                    self.driver.reset()
            except Exception:
                log.exception("Unexpected scrape error")
                self.errors_total += 1
            finally:
                if self.cfg.logout_after_scrape and self.driver:
                    self.driver.logout()
        if not up:
            fam = Families(self.modem)  # drop partial data; don't publish half a scrape
        self.scrapes_total += 1
        fam.add("modem_up", "1 if the router was scraped successfully", {}, up)
        fam.add("modem_exporter_info", "Exporter driver in use",
                {"driver": self.driver.name if self.driver else "unknown"}, 1)
        fam.add("modem_scrape_duration_seconds", "Time spent scraping the router", {}, time.time() - start)
        fam.add("modem_scrape_objects", "Number of API objects read", {}, count)
        fam.add("modem_scrape_last_timestamp_seconds", "Unix time of the last scrape", {}, time.time())
        fam.add("modem_login_backoff_seconds", "Seconds until the next login attempt is allowed (0 = none)", {},
                max(0.0, self.login_blocked_until - time.time()))
        fam.add("modem_exporter_scrapes", "Scrapes performed by the exporter", {}, self.scrapes_total, kind="counter")
        fam.add("modem_exporter_scrape_errors", "Failed scrapes", {}, self.errors_total, kind="counter")
        return list(fam.families())

    def collect(self):
        with self.lock:
            if not self.cached or time.time() - self.cached_at >= self.cfg.cache_seconds:
                self.cached = self.scrape()
                self.cached_at = time.time()
            yield from self.cached
