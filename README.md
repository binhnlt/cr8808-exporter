# Xiaomi CR8808 Wi-Fi Router / AP — Prometheus Exporter

A Prometheus exporter for the **Xiaomi CR8808** (the ISP-branded AX3000 sold by
Chinese carriers, platform `CR8818`) and other Xiaomi / Redmi routers running
the stock **MiWiFi (LuCI)** firmware.

Cheap ISP routers like this have no SNMP. This exporter logs in to the router's
web API the same way the browser does, reads the system, LAN, Wi-Fi and client
status, and republishes it as Prometheus metrics on `/metrics`. Point your own
Prometheus at it and graph it in Grafana.

```
Xiaomi router ──HTTP JSON API──► modem_exporter :9878/metrics ──► your Prometheus ──► your Grafana
               (optionally via SOCKS5)
```

- Runs as a container or a bare Python process (2 small dependencies).
- Identity, uptime, CPU and memory, clock skew (to spot a dead NTP).
- Uplink byte counters and **per-client** download/upload counters, so traffic can be shown in **bits/sec**, per device and per connection type.
- Online clients per band (wired / 2.4 GHz / 5 GHz / guest), Wi-Fi signal per station, how long each client has been connected.
- Wi-Fi radios (channel, width, standard, security, TX power), Ethernet port links, LAN/DHCP settings, WAN and IPv6 state.
- Security-relevant settings: firewall, UPnP (and its mappings), port forwards, telnet/SSH, remote web access, ...
- Read-only: it only calls a fixed allow-list of GET endpoints. It stops for a while after a failed login so the router doesn't lock you out. Wi-Fi passwords are never exported.
- One exporter per router; configure each through its own `.env` file.

The metric names (`modem_*`, `modem` label) match the
[viettel-dasan exporter](../viettel-dasan-monitoring), so the GPON modem and the Wi-Fi AP can share one Prometheus.

---

## Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `modem_up` | gauge | `1` if login + scrape succeeded this cycle, else `0` |
| `modem_info{model,hardware,isp_model,software_version,display_version,serial_number,mac_address,router_name,country,timezone}` | gauge | Router identity. The value is always `1`; the data is in the labels |
| `modem_uptime_seconds` | gauge | Router uptime |
| `modem_cpu_usage_percent` / `modem_cpu_cores` / `modem_cpu_frequency_hertz` | gauge | CPU load and hardware |
| `modem_memory_usage_percent` / `modem_memory_total_bytes` / `modem_memory_used_bytes` | gauge | Memory |
| `modem_temperature_celsius` | gauge | Only exported when the model has a sensor (the CR8808 reports `0` = none) |
| `modem_clock_skew_seconds` | gauge | Router clock minus exporter clock; large values mean NTP isn't working |
| `modem_network_mode` | gauge | Operating mode code: `0` = router, non-zero = AP / relay / mesh (`3` on a CR8808 in wired-AP mode) |
| `modem_interface_{rx,tx}_bytes_total{interface="wan"}` | counter | Bytes through the uplink since boot |
| `modem_interface_{rx,tx}_rate_bytes_per_second` / `_peak_rate_bytes_per_second` | gauge | The router's own current / highest rate |
| `modem_clients{state="online"\|"known"}` | gauge | Clients connected now / seen since boot |
| `modem_clients_by_connection{connection}` | gauge | Online clients per `wired`, `wifi_2.4ghz`, `wifi_5ghz`, `wifi_guest` |
| `modem_wifi_clients{band}` | gauge | Associated stations per band |
| `modem_client_info{mac,name,ip,connection,is_ap}` | gauge | Client details (value is always `1`); join other client metrics on `mac` |
| `modem_client_online{mac}` | gauge | `1` if the client is connected now |
| `modem_client_{download,upload}_bytes_total{mac}` | counter | Per-client traffic since boot |
| `modem_client_{download,upload}_rate_bytes_per_second{mac}` | gauge | Per-client rate as computed by the router |
| `modem_client_connected_seconds{mac}` | gauge | Current session length (last session for offline clients) |
| `modem_client_wifi_signal{mac,band}` | gauge | Station signal as reported by the router (raw value, higher = better, **not dBm**) |
| `modem_lan_up` / `modem_lan_uptime_seconds` / `modem_lan_info{ip,netmask,mac}` | gauge | LAN bridge |
| `modem_lan_port_up{port,label,service}` / `modem_lan_port_max_speed_mbps` | gauge | Ethernet port link state and port capability |
| `modem_dhcp_server_enabled` / `_pool_size` / `_lease_time_seconds` / `_static_leases` | gauge | DHCP server settings |
| `modem_wan_link_up` / `modem_wan_status` / `modem_wan_info{proto,ip,gateway,dns}` / `modem_wan_ipv6_up` | gauge | WAN state (mostly `0` in AP mode) |
| `modem_wifi_radio_enabled{band,ifname}` / `modem_wifi_channel` / `modem_wifi_channel_width_mhz` | gauge | Radio state, channel, width in use |
| `modem_wifi_ssid_info{band,ifname,ssid,encryption,standard,tx_power,hidden,bandwidth_setting}` | gauge | SSID settings (value is always `1`) |
| `modem_wifi_roam_rssi_threshold_dbm{band}` | gauge | Signal below which clients are steered to another AP |
| `modem_feature_enabled{feature}` | gauge | `firewall`, `dos_protection`, `https_only`, `upnp`, `qos`, `band_steering`, `remote_web_access`, `mac_filter`, `ip_filter`, `telnet`, `ssh`, `uart`, `secure_boot`, `elink` |
| `modem_upnp_mappings` / `modem_nat_rules{type}` / `modem_vpn_status` | gauge | UPnP mappings, port-forward rules, VPN client state |
| `modem_mesh_nodes{role}` / `modem_messages` | gauge | Mesh nodes, pending UI notifications |
| `modem_scrape_duration_seconds` / `modem_scrape_objects` / `modem_login_backoff_seconds` / `modem_exporter_info{driver}` | gauge | Exporter self-monitoring |

Throughput is derived at query time, e.g.

```promql
# uplink download in bit/s
rate(modem_interface_rx_bytes_total{interface="wan"}[5m]) * 8

# top 10 clients by download, with their names
topk(10, rate(modem_client_download_bytes_total[5m]) * 8
  * on(modem, mac) group_left(name, ip, connection) modem_client_info)

# traffic per connection type (wired / 2.4 GHz / 5 GHz)
sum by (connection) (rate(modem_client_download_bytes_total[5m]) * 8
  * on(modem, mac) group_left(connection) modem_client_info)
```

Per-client counters are labelled only by `mac`, so a client that is renamed or
gets a new IP keeps one continuous series. Its name and IP come from `modem_client_info`.

---

## Quick start

```bash
cd cr8808-monitoring
cp .env.example .env
nano .env # set MODEM_URL and MODEM_PASSWORD (the router's admin password)
docker compose up -d
```

Metrics are then served at `http://<host>:9878/metrics`. Check with:

```bash
curl -s localhost:9878/metrics | grep -E '^modem_(up|info|uptime)'
```

To run without Docker: `pip install -r exporter/requirements.txt && cd exporter && python -m modem_exporter --env-file ../.env`.

### Scrape it with Prometheus

Add a job to your Prometheus config (full snippet in
[`examples/prometheus-scrape.yml`](examples/prometheus-scrape.yml)):

```yaml
scrape_configs:
  - job_name: cr8808
    scrape_interval: 60s
    scrape_timeout: 45s
    static_configs:
      - targets: ['EXPORTER_HOST:9878']
```

Every API request costs the router ~0.3 s of CPU, and parallel requests
don't help. So the exporter reads the 7 live endpoints (status, clients,
Wi-Fi stations, LAN/WAN, clock) on every scrape. The ~27 settings endpoints are
cached for `SLOW_REFRESH_SECONDS` and refreshed a few at a time. A scrape takes
**~4 s**. The first one after start-up reads everything (~25–30 s); the exporter
runs it in the background as soon as it starts.

### Grafana dashboard (optional)

Import [`examples/grafana-dashboard.json`](examples/grafana-dashboard.json) into
Grafana (Dashboards → Import). Variables at the top of the dashboard let you choose:

| Variable | Purpose |
|----------|---------|
| **Data source** | Any Prometheus data source; no editing of the JSON needed |
| **Job** | Prometheus `job` label(s) to show (multi-select, default *All*) |
| **Router** | `modem` label(s), i.e. `MODEM_NAME`, when you monitor several routers |
| **Filters** | Ad-hoc `label = value` filters for any other label, e.g. the `device` label from the scrape config |

It shows:
- reachability, uptime, CPU, memory, online clients, Ethernet links and clock skew
- device information
- uplink throughput and traffic per connection type
- top clients by download and upload
- clients per connection type, Wi-Fi signal per client and channel history
- tables of clients (status, traffic, session), Wi-Fi radios and Ethernet ports
- CPU and memory history, scrape duration, and the router's feature switches

---

## Configuration

All configuration is via environment variables (usually the `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEM_URL` | *(required)* | Router web UI URL, e.g. `http://192.168.31.1` |
| `MODEM_PASSWORD` | *(required)* | Web UI admin password (plaintext; it is hashed the way the browser does before sending) |
| `MODEM_USERNAME` | `admin` | Xiaomi firmwares only have `admin` |
| `MODEM_PROXY` | *(none)* | `socks5://user:pass@host:port`, `socks5h://…` (DNS via proxy) or `http://…` |
| `MODEM_NAME` | host of `MODEM_URL` | Value of the `modem` label on every metric |
| `DRIVER` | `auto` | `miwifi`, or `auto`-detect |
| `LISTEN_PORT` | `9878` | Port to serve `/metrics` on |
| `SCRAPE_TIMEOUT` | `10` | Per-HTTP-request timeout (seconds) |
| `CACHE_SECONDS` | `30` | Reuse the last result if Prometheus scrapes again within this window |
| `SLOW_REFRESH_SECONDS` | `300` | How often settings endpoints (Wi-Fi config, firewall, DHCP, ...) are re-read |
| `CLIENT_METRICS` | `true` | Per-client series (a handful of series per MAC). Set `false` on a network with many transient devices |
| `VERIFY_TLS` | `false` | Only matters if you use `https://` |
| `LOGOUT_AFTER_SCRAPE` | `false` | The session token is kept between scrapes and renewed when it expires. `true` logs in on every scrape |
| `LOGIN_BACKOFF_SECONDS` | `300` | Wait this long after a failed login before trying again |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every request |

`URL`, `USERNAME`, `PASSWORD` and `PROXY` (without the `MODEM_` prefix) are accepted too.

### Monitoring more than one router

Run one exporter per router, each with its own env file, `MODEM_NAME` and host
port. There's a commented example in [`docker-compose.yml`](docker-compose.yml).
Then add each one as a target in Prometheus. The dashboard's **Router** variable
switches between them.

---

## How it works

**1. Detection and login.** `GET /cgi-bin/luci/api/xqsystem/init_info` needs no
login. It returns the model, ROM version and `newEncryptMode`. The exporter reads
the password salt (`key`) from the login page `/cgi-bin/luci/web` and builds a
nonce `0_<deviceId>_<time>_<random>`. It then `POST`s to `/cgi-bin/luci/api/xqsystem/login`
with `password = SHA1(nonce + SHA1(password + key))` (SHA-256 when
`newEncryptMode=1`), just like the web UI. The response carries a session token
(`stok`). All later requests go to `/cgi-bin/luci/;stok=<token>/api/<module>/<function>`.
An expired token (`{"code":401,"msg":"Invalid token"}`) triggers one new login.

**2. Scraping.** Only these endpoints are called:

| Endpoint | Provides | Tier |
|----------|----------|------|
| `misystem/status` | uptime, CPU, memory, uplink counters, per-client counters, client counts | live |
| `misystem/newstatus` | stations per band, mesh nodes, band steering | live |
| `misystem/devicelist` | online clients: name, IP, connection type | live |
| `xqnetwork/wifi_connect_devices` | signal per Wi-Fi station | live |
| `xqnetwork/lan_info`, `xqnetwork/wan_info` | LAN bridge, Ethernet links, WAN link | live |
| `misystem/sys_time` | router clock (skew) | live |
| `xqnetwork/wifi_detail_all`, `xqsystem/information` | radios, SSIDs, channel width | settings |
| `xqnetwork/wifi_macfilter_info` | every known client (names/IPs of offline ones) | settings |
| `xqnetwork/lan_dhcp`, `xqnetwork/macbind_info`, `misystem/get_ps_map` | DHCP, static leases, port map | settings |
| `xqnetwork/pppoe_status`, `xqnetwork/get_wan6_info_v2`, `xqnetwork/get_netmode` | WAN, IPv6, operating mode | settings |
| `xqsystem/get_*_firewall`, `get_access_force_https`, `get_*filter_info`, `get_pt_rules`, `get_vs_rules`, `upnp`, `fac_info`, `vpn_status`, `misystem/qos2_list`, `web_access_info`, `get_elink`, `router_name`, `messages`, `topo_graph`, `xqnetwork/get_roam_rssi_threshold` | settings and feature switches | settings |

This API also has **state-changing GET endpoints** (`set_*`, `reboot`, `reset`,
`pppoe_start`, `wps`, and `misystem/sys_log`, which packs a log archive on the
router). The exporter never calls anything outside its allow-list, and the test
suite checks that. Endpoints that return 404 on a firmware are skipped for the
rest of the session.

**3. Publishing.** Values are exposed through a `prometheus_client` custom
collector and cached for `CACHE_SECONDS`. If a scrape fails, `modem_up` goes to
`0` and the other series are omitted for that cycle rather than showing stale data.

### Other models / discovering endpoints

To see what a different Xiaomi firmware returns:

```bash
mkdir -p dump && chmod 777 dump
docker compose run --rm -v "$PWD/dump:/app/dump" exporter discover
```

This saves every allow-listed endpoint's raw response (passwords masked) to
`./dump/miwifi_endpoints.json`. `docker compose run --rm exporter once` prints one scrape to stdout.

Support for another router family goes in a new driver: implement the small
`Driver` interface in `exporter/modem_exporter/base.py` (`detect`, `scrape`,
`logout`, `dump`) and register it in `DRIVERS` in `collector.py`. `DRIVER=auto`
then tries it too.

---

## Limitations

- **AP mode.** In wired-AP mode (how this CR8808 is used) the router does no
  routing. WAN/PPPoE/IPv6 metrics are `0`, and the "WAN" counters count traffic
  through the uplink port.
- **Wi-Fi signal is a raw number.** `wifi_connect_devices` reports values like
  `92`–`136` rather than dBm. Use it for trends and comparisons, not absolute levels.
- **Router-computed rates** (`*_rate_bytes_per_second`) are coarse snapshots.
  Prefer `rate()` over the byte counters.
- **Counters reset** when the router reboots. Prometheus' `rate()` handles this.
  Per-client counters are also lost when the router forgets a client.
- **CPU load** is the router's own figure. On an idle AP it often reads `0`.
- **Codes without documentation.** `modem_network_mode`, `modem_wan_status` and
  `modem_vpn_status` are exported as the raw numbers the firmware returns.

---

## Troubleshooting

**`modem_up` is `0`**: check the exporter logs with `docker logs cr8808-exporter`.

- **`Login rejected (code 401)`**: wrong password. It is the router admin
  password, not the Wi-Fi password. The exporter now waits `LOGIN_BACKOFF_SECONDS`
  before trying again (see `modem_login_backoff_seconds`). The web UI blocks
  logins after several wrong attempts, so don't lower the backoff too far.
- **`Router unreachable`**: the exporter host can't reach `MODEM_URL`. If the
  router is at another site, set `MODEM_PROXY`. With WSL2 or Docker Desktop,
  check that the container can route to the router's subnet.
- **`answers, but no driver recognises it`**: `/cgi-bin/luci/api/xqsystem/init_info`
  didn't return MiWiFi JSON. The device isn't running MiWiFi firmware (OpenWrt,
  another vendor).
- **Scrapes time out in Prometheus**: increase `scrape_timeout`, or raise
  `SLOW_REFRESH_SECONDS`.

**No data in Grafana**: confirm the Prometheus target is `UP`
(`Status → Targets`) and that `modem_up` exists in Prometheus first.

---

## Security notes

- `.env` holds the router password (and proxy credentials) and is git-ignored.
  Keep it `chmod 600`.
- Wi-Fi passwords are never exported, and are masked in `discover` output.
- Metrics include SSIDs, client names, MACs and IPs. `/metrics` has no
  authentication, so restrict it to your monitoring network.
- The login travels over plain HTTP (the router has no HTTPS). The password
  itself isn't sent, only a salted one-time hash, but the session token is.
  Keep the management interface on a trusted network.

---

## Development

```bash
pip install -r exporter/requirements.txt pytest ruff
ruff check exporter && pytest -q exporter
python exporter/fake_router.py 8080     # fake router for manual runs (password: secret)
```

## Compatibility

Developed and tested against a **Xiaomi CR8808** (platform `CR8818`, ROM
`6.20.92`, ISP ROM `1.4.01`, China Mobile firmware) running as a wired AP. The
same API is used by most Xiaomi / Redmi routers with stock firmware (AX3000,
AX3600, AX6000, RA/RB/CR series). Endpoints a model lacks are skipped, so the
exporter should work there too, with fewer metrics.

## License

MIT
