"""Entry point.

    python -m modem_exporter            # serve /metrics (default)
    python -m modem_exporter once       # scrape once and print metrics to stdout
    python -m modem_exporter discover   # dump raw (secret-masked) API responses to ./dump
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.registry import CollectorRegistry

from .collector import ModemCollector, select_driver
from .config import Config, load_dotenv

log = logging.getLogger("modem_exporter")


def make_handler(registry: CollectorRegistry):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/metrics":
                body, ctype, code = generate_latest(registry), CONTENT_TYPE_LATEST, 200
            elif path in ("/health", "/-/healthy"):
                body, ctype, code = b"ok\n", "text/plain", 200
            elif path == "/":
                body = b"<html><body><h1>Router exporter</h1><p><a href='/metrics'>Metrics</a></p></body></html>"
                ctype, code = "text/html", 200
            else:
                body, ctype, code = b"not found\n", "text/plain", 404
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def serve(cfg: Config) -> None:
    registry = CollectorRegistry()
    collector = ModemCollector(cfg)
    registry.register(collector)
    # Warm up in the background: the first scrape (detect + login + every endpoint) is the slowest.
    threading.Thread(target=lambda: list(collector.collect()), daemon=True).start()
    server = ThreadingHTTPServer((cfg.listen_address, cfg.listen_port), make_handler(registry))
    # Raise out of serve_forever(); calling server.shutdown() here would deadlock (same thread).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("Exporter for %s listening on http://%s:%d/metrics", cfg.url, cfg.listen_address, cfg.listen_port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def once(cfg: Config) -> None:
    registry = CollectorRegistry()
    registry.register(ModemCollector(cfg))
    sys.stdout.write(generate_latest(registry).decode())


def discover(cfg: Config, out_dir: str) -> None:
    driver = select_driver(cfg)
    if driver is None:
        raise SystemExit(f"No driver recognises {cfg.url}")
    try:
        data = driver.dump()
    finally:
        driver.logout()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{driver.name}_endpoints.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print(f"{driver.name}: saved {len(data)} endpoint response(s) to {path} (secrets masked)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="modem_exporter", description="Prometheus exporter for home routers / APs")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "once", "discover"])
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--out", default="dump", help="output directory for `discover`")
    args = parser.parse_args()

    load_dotenv(args.env_file)
    cfg = Config.from_env()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    {"serve": lambda: serve(cfg), "once": lambda: once(cfg), "discover": lambda: discover(cfg, args.out)}[args.command]()


if __name__ == "__main__":
    main()
