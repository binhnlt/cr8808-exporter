"""Driver for Xiaomi routers running MiWiFi (LuCI) firmware, including ISP-branded
units such as the CR8808 / CR8818 (China Mobile / Unicom AX3000) and most
Xiaomi / Redmi AX routers. Endpoints were reverse-engineered from the web UI:

    GET  /cgi-bin/luci/api/xqsystem/init_info          -> model, ROM, newEncryptMode (no login)
    GET  /cgi-bin/luci/web                             -> login page with the password salt `key`
    POST /cgi-bin/luci/api/xqsystem/login              -> {"code":0,"token":"<stok>"}
    GET  /cgi-bin/luci/;stok=<stok>/api/<module>/<fn>  -> JSON data
    GET  /cgi-bin/luci/;stok=<stok>/web/logout

Login: password = SHA1(nonce + SHA1(password + key)) (SHA256 when newEncryptMode=1),
nonce = "0_<deviceId>_<unix time>_<random>".

Only the GET endpoints listed in ENDPOINTS are ever called. The API also has
state-changing GET endpoints (set_*, reboot, reset, pppoe_start, sys_log, ...);
they are never requested.
"""
from __future__ import annotations

import calendar
import hashlib
import logging
import random
import re
import time

import requests

from .base import Driver, Families, LoginError, new_session
from .config import Config

log = logging.getLogger(__name__)

API = "/cgi-bin/luci/api/"
WEB = "/cgi-bin/luci/web"
DEFAULT_KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"
STATUS = "misystem/status"

# Every request costs the router >= 0.3 s of CPU (and parallel requests don't
# help), so endpoints are split in two tiers.
# LIVE: read on every scrape. STATUS must come first: if it fails the router is considered down.
LIVE_ENDPOINTS = [
    STATUS,
    "misystem/newstatus",
    "misystem/devicelist",
    "misystem/sys_time",
    "xqnetwork/lan_info",
    "xqnetwork/wan_info",
    "xqnetwork/wifi_connect_devices",
]
# SLOW: settings and slowly changing state, cached for SLOW_REFRESH_SECONDS.
# At most SLOW_PER_SCRAPE stale ones are refreshed per scrape so scrape time stays flat.
SLOW_ENDPOINTS = [
    "xqnetwork/wifi_detail_all",
    "xqsystem/information",
    "xqnetwork/pppoe_status",
    "xqnetwork/get_wan6_info_v2",
    "misystem/messages",
    "misystem/router_name",
    "misystem/topo_graph",
    "misystem/get_ps_map",
    "misystem/qos2_list",
    "misystem/web_access_info",
    "misystem/get_elink",
    "xqnetwork/lan_dhcp",
    "xqnetwork/get_netmode",
    "xqnetwork/get_roam_rssi_threshold",
    "xqnetwork/macbind_info",
    "xqnetwork/wifi_macfilter_info",  # `flist` = every known client, incl. offline ones (names, IPs)
    "xqsystem/information",
    "xqsystem/fac_info",
    "xqsystem/get_firewall_enable",
    "xqsystem/get_dos_firewall",
    "xqsystem/get_access_force_https",
    "xqsystem/get_macfilter_info",
    "xqsystem/get_ipfilter_info",
    "xqsystem/get_pt_rules",
    "xqsystem/get_vs_rules",
    "xqsystem/upnp",
    "xqsystem/vpn_status",
]
SLOW_PER_SCRAPE = 6
# Extra read-only endpoints included in `discover` output only.
DISCOVER_ONLY = [
    "xqnetwork/wan_statistics",
    "xqnetwork/wifi_diag_detail_all",
    "xqnetwork/ipv6_status",
    "xqnetwork/get_wan6_v2",
    "xqnetwork/get_lan6_v2",
    "xqnetwork/get_mesh_switch",
    "xqnetwork/get_miscan_switch",
    "xqnetwork/wan_speed",
    "xqsystem/system_info",
    "xqsystem/country_code",
    "xqsystem/get_wanping_firewall",
    "xqsystem/get_alg_rules",
    "xqsystem/wps_status",
]
SECRET = re.compile(r"pass|pwd|psk|key|secret|token", re.IGNORECASE)
# Client `type` in misystem/devicelist.
CONNECTION = {0: "wired", 1: "wifi_2.4ghz", 2: "wifi_5ghz", 3: "wifi_guest"}
# `wifiIndex` in xqnetwork/wifi_connect_devices.
WIFI_INDEX = {1: "2.4ghz", 2: "5ghz", 3: "guest"}


class _Unavailable(Exception):
    """Endpoint missing on this firmware (404/500/non-JSON) or returned an error code."""


def detect(session: requests.Session, cfg: Config) -> bool:
    try:
        r = session.get(cfg.url + API + "xqsystem/init_info", timeout=cfg.timeout)
        j = r.json()
        return r.ok and isinstance(j, dict) and j.get("code") == 0 and ("romversion" in j or "hardware" in j)
    except (requests.RequestException, ValueError):
        return False


# ------------------------------------------------------------------ parsing helpers
def _num(v) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _flag(v) -> float | None:
    """'1'/1/True/'on' -> 1, '0'/0/False/'off' -> 0, anything else -> None."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    s = str(v).strip().lower()
    if s in ("1", "true", "on", "yes", "enable", "enabled"):
        return 1.0
    if s in ("0", "false", "off", "no", "disable", "disabled"):
        return 0.0
    return None


_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2, "g": 1024**3, "gb": 1024**3}


def _bytes(v) -> float | None:
    """'256MB' -> 268435456."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmg]?b?)\s*", str(v), re.IGNORECASE)
    return float(m.group(1)) * _UNITS[m.group(2).lower()] if m else None


def _hertz(v) -> float | None:
    """'1000MHz' / '1.4GHz' -> Hz."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmg]?)hz\s*", str(v), re.IGNORECASE)
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()] if m else None


def _mbps(v) -> float | None:
    """'1G' / '2.5G' / '100M' -> Mbit/s."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([mg]?)\s*", str(v), re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1)) * (1000 if m.group(2).lower() == "g" else 1)


def _tz_offset(tz: str) -> float | None:
    """POSIX TZ 'CST-8' -> +28800 (seconds east of UTC). DST rules are ignored."""
    m = re.match(r"^[A-Za-z]{3,}|^<[^>]+>", tz or "")
    if not m:
        return None
    m2 = re.match(r"([+-]?)(\d{1,2})(?::(\d{2}))?", tz[m.end():])
    if not m2:
        return None
    secs = int(m2.group(2)) * 3600 + int(m2.group(3) or 0) * 60
    return float(secs if m2.group(1) == "-" else -secs)


def _band(entry: dict) -> str:
    mode = str(entry.get("wifimode", "")).lower()
    if mode.endswith("g"):
        return "2.4ghz"
    if mode.endswith("a"):
        return "5ghz"
    ch = _num(entry.get("channel")) or _num((entry.get("channelInfo") or {}).get("channel"))
    if ch:
        return "2.4ghz" if ch <= 14 else "5ghz"
    return str(entry.get("ifname", "unknown"))


def mask(obj):
    if isinstance(obj, dict):
        return {k: ("***" if SECRET.search(k) and v not in ("", None) else mask(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(x) for x in obj]
    return obj


# ------------------------------------------------------------------ metric mapping
def build_metrics(fam: Families, d: dict, client_metrics: bool = True, now: float | None = None,
                  offline_clients: list[dict] | None = None) -> None:
    """Map raw endpoint responses (`{endpoint: json}`) to metrics.

    `now`: when misystem/sys_time was read (for the clock skew). `offline_clients`: last-seen
    devicelist entries of clients no longer listed, so their name/IP/connection are kept.
    """
    _system(fam, d, now)
    _clients(fam, d, client_metrics, offline_clients or [])
    _lan(fam, d)
    _wan(fam, d)
    _wifi(fam, d)
    _features(fam, d)


def _system(fam: Families, d: dict, now: float | None) -> None:
    st = d.get(STATUS) or {}
    init = d.get("xqsystem/init_info") or {}
    hw = st.get("hardware") or (d.get("misystem/newstatus") or {}).get("hardware") or {}
    tz = ((d.get("misystem/sys_time") or {}).get("time") or {}).get("timezone", "")
    info = {
        "vendor": "xiaomi",
        "model": init.get("model") or "",
        "hardware": hw.get("platform") or init.get("hardware") or "",
        "isp_model": hw.get("ispName") or "",
        "software_version": hw.get("version") or init.get("romversion") or "",
        "display_version": hw.get("DisplayRomVer") or "",
        "rom_channel": hw.get("channel") or "",
        "serial_number": hw.get("sn") or init.get("id") or "",
        "mac_address": hw.get("mac") or "",
        "router_name": (d.get("misystem/router_name") or {}).get("name") or init.get("routername") or "",
        "country": init.get("countrycode") or "",
        "timezone": tz,
    }
    fam.add("modem_info", "Static router information (value is always 1)", info, 1)
    fam.add("modem_uptime_seconds", "Router uptime in seconds", {}, _num(st.get("upTime")))

    cpu, mem = st.get("cpu") or {}, st.get("mem") or {}
    load = _num(cpu.get("load"))
    fam.add("modem_cpu_usage_percent", "CPU load (%)", {}, None if load is None else load * 100)
    fam.add("modem_cpu_cores", "Number of CPU cores", {}, _num(cpu.get("core")))
    fam.add("modem_cpu_frequency_hertz", "CPU clock frequency", {}, _hertz(cpu.get("hz")))
    usage, total = _num(mem.get("usage")), _bytes(mem.get("total"))
    fam.add("modem_memory_usage_percent", "Memory usage (%)", {}, None if usage is None else usage * 100)
    fam.add("modem_memory_total_bytes", "Total memory", {}, total)
    if usage is not None and total:
        fam.add("modem_memory_used_bytes", "Used memory", {}, usage * total)
    if mem.get("type") or mem.get("hz"):
        fam.add("modem_hardware_info", "Memory type and clock (value is always 1)",
                {"memory_type": mem.get("type", ""), "memory_frequency": mem.get("hz", ""),
                 "cpu_frequency": cpu.get("hz", "")}, 1)
    temp = _num(st.get("temperature"))
    if temp:  # 0 means "no sensor" on these firmwares
        fam.add("modem_temperature_celsius", "Router temperature (°C)", {}, temp)

    t = (d.get("misystem/sys_time") or {}).get("time") or {}
    offset = _tz_offset(tz)
    if offset is not None and all(k in t for k in ("year", "month", "day", "hour", "min", "sec")):
        try:
            local = calendar.timegm((int(t["year"]), int(t["month"]), int(t["day"]),
                                     int(t["hour"]), int(t["min"]), int(t["sec"]), 0, 0, 0))
            fam.add("modem_clock_skew_seconds", "Router clock minus exporter clock (large values = NTP not working)",
                    {}, (local - offset) - (now if now is not None else time.time()))
        except (TypeError, ValueError):
            pass

    msgs = d.get("misystem/messages")
    if msgs is not None:
        fam.add("modem_messages", "Pending notifications shown in the web UI", {}, _num(msgs.get("count")))
    netmode = _num((d.get("xqnetwork/get_netmode") or {}).get("netmode"))
    if netmode is None:
        netmode = _num(((d.get("misystem/topo_graph") or {}).get("graph") or {}).get("mode"))
    fam.add("modem_network_mode", "Operating mode code (0 = router; non-zero = AP / relay / mesh modes)", {}, netmode)
    ns = d.get("misystem/newstatus") or {}
    for role, key in (("cap", "cap_count"), ("re", "re_count")):
        fam.add("modem_mesh_nodes", "Mesh nodes known to this router (cap = controller, re = satellite)",
                {"role": role}, _num(ns.get(key)))


def _clients(fam: Families, d: dict, client_metrics: bool, offline_clients: list[dict]) -> None:
    st = d.get(STATUS) or {}
    count = st.get("count") or {}
    fam.add("modem_clients", "Client devices (online = connected now, known = seen since boot)",
            {"state": "online"}, _num(count.get("online")))
    fam.add("modem_clients", "Client devices (online = connected now, known = seen since boot)",
            {"state": "known"}, _num(count.get("all")))

    wan = st.get("wan") or (d.get("xqnetwork/wan_statistics") or {}).get("statistics") or {}
    for direction, total, speed, peak in (("rx", "download", "downspeed", "maxdownloadspeed"),
                                          ("tx", "upload", "upspeed", "maxuploadspeed")):
        labels = {"interface": "wan"}
        fam.add(f"modem_interface_{direction}_bytes_total", f"Bytes {'received' if direction == 'rx' else 'sent'} "
                "on the uplink since boot", labels, _num(wan.get(total)), kind="counter")
        fam.add(f"modem_interface_{direction}_rate_bytes_per_second", "Current rate as computed by the router",
                labels, _num(wan.get(speed)))
        fam.add(f"modem_interface_{direction}_peak_rate_bytes_per_second", "Highest rate seen by the router since boot",
                labels, _num(wan.get(peak)))

    devlist = d.get("misystem/devicelist")
    listed: dict[str, dict] = {}
    if devlist is not None:
        by_conn = {c: 0 for c in CONNECTION.values()}
        for dev in devlist.get("list") or []:
            mac = str(dev.get("mac", "")).upper()
            if not mac:
                continue
            listed[mac] = dev
            if _flag(dev.get("online")) == 1.0:
                conn = CONNECTION.get(int(_num(dev.get("type")) or 0), "other")
                by_conn[conn] = by_conn.get(conn, 0) + 1
        for conn, n in by_conn.items():
            fam.add("modem_clients_by_connection", "Online clients per connection type", {"connection": conn}, n)

    ns = d.get("misystem/newstatus") or {}
    for key, band in (("2g", "2.4ghz"), ("5g", "5ghz"), ("5g_game", "5ghz_game"), ("guest", "guest")):
        if isinstance(ns.get(key), dict):
            fam.add("modem_wifi_clients", "Associated Wi-Fi stations per band", {"band": band},
                    _num(ns[key].get("online_sta_count")))

    if not client_metrics:
        return
    stats = {str(x.get("mac", "")).upper(): x for x in st.get("dev") or [] if x.get("mac")}
    # Clients not in the live list: take name/IP/connection from the known-devices list, else from memory.
    for dev in ((d.get("xqnetwork/wifi_macfilter_info") or {}).get("flist") or []) + offline_clients:
        if dev.get("mac"):
            listed.setdefault(str(dev["mac"]).upper(), {**dev, "online": 0})
    for mac in sorted(set(stats) | set(listed)):
        s, dev = stats.get(mac, {}), listed.get(mac, {})
        ip = ",".join(i.get("ip", "") for i in dev.get("ip") or [] if i.get("ip"))
        conn = CONNECTION.get(int(_num(dev.get("type")) or 0), "other") if dev else ""
        name = dev.get("name") or dev.get("oname") or s.get("devname") or mac
        fam.add("modem_client_info", "Client device details (value is always 1); join on `mac`",
                {"mac": mac, "name": name, "ip": ip, "connection": conn, "is_ap": dev.get("isap", s.get("isap", 0))}, 1)
        online = _flag(dev.get("online")) if dev else 0.0
        fam.add("modem_client_online", "1 if the client is connected now", {"mac": mac}, online or 0.0)
        if not s:
            continue
        lbl = {"mac": mac}
        fam.add("modem_client_download_bytes_total", "Bytes downloaded by the client since boot", lbl,
                _num(s.get("download")), kind="counter")
        fam.add("modem_client_upload_bytes_total", "Bytes uploaded by the client since boot", lbl,
                _num(s.get("upload")), kind="counter")
        fam.add("modem_client_download_rate_bytes_per_second", "Current download rate (router's own calculation)",
                lbl, _num(s.get("downspeed")))
        fam.add("modem_client_upload_rate_bytes_per_second", "Current upload rate (router's own calculation)",
                lbl, _num(s.get("upspeed")))
        fam.add("modem_client_connected_seconds", "How long the client has been connected (last session if offline)",
                lbl, _num(s.get("online")))

    for sta in (d.get("xqnetwork/wifi_connect_devices") or {}).get("list") or []:
        mac = str(sta.get("mac", "")).upper()
        if mac:
            fam.add("modem_client_wifi_signal", "Wi-Fi signal of an associated station (raw vendor value, higher = better)",
                    {"mac": mac, "band": WIFI_INDEX.get(int(_num(sta.get("wifiIndex")) or 0), "other")},
                    _num(sta.get("signal")))


def _lan(fam: Families, d: dict) -> None:
    lan_resp = d.get("xqnetwork/lan_info") or {}
    lan = lan_resp.get("info") or (d.get("xqsystem/information") or {}).get("lan") or {}
    if lan:
        fam.add("modem_lan_up", "1 if the LAN bridge is up", {}, _flag(lan.get("status")))
        fam.add("modem_lan_uptime_seconds", "LAN interface uptime", {}, _num(lan.get("uptime")))
        for ip in lan.get("ipv4") or []:
            fam.add("modem_lan_info", "LAN addressing (value is always 1)",
                    {"ip": ip.get("ip", ""), "netmask": ip.get("mask", ""), "mac": lan.get("mac", "")}, 1)

    links = lan_resp.get("linkList")
    ports = (d.get("misystem/get_ps_map") or {}).get("ports") or []
    if isinstance(links, list):
        for i, link in enumerate(links):
            p = ports[i] if i < len(ports) else {}
            labels = {"port": p.get("port", str(i + 1)), "label": p.get("label", str(i + 1)),
                      "service": p.get("service", "")}
            fam.add("modem_lan_port_up", "1 if an Ethernet cable is linked on this port", labels, _flag(link))
    for p in ports:
        fam.add("modem_lan_port_max_speed_mbps", "Maximum speed supported by the Ethernet port",
                {"port": p.get("port", ""), "label": p.get("label", ""), "service": p.get("service", "")},
                _mbps(p.get("speed")))

    dhcp = (d.get("xqnetwork/lan_dhcp") or {}).get("info") or {}
    if dhcp:
        ignore = _flag(dhcp.get("ignore"))
        fam.add("modem_dhcp_server_enabled", "1 if the router's DHCP server is enabled", {},
                None if ignore is None else 1 - ignore)
        fam.add("modem_dhcp_pool_size", "Number of addresses in the DHCP pool", {}, _num(dhcp.get("limit")))
        num, unit = _num(dhcp.get("leasetimeNum")), str(dhcp.get("leasetimeUnit", "")).lower()
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
        if num is not None and mult:
            fam.add("modem_dhcp_lease_time_seconds", "DHCP lease duration", {}, num * mult)
    binds = d.get("xqnetwork/macbind_info")
    if binds is not None:
        fam.add("modem_dhcp_static_leases", "Static DHCP (MAC-IP binding) entries", {}, len(binds.get("list") or []))


def _wan(fam: Families, d: dict) -> None:
    wan = (d.get("xqnetwork/wan_info") or {}).get("info") or {}
    if wan:
        fam.add("modem_wan_link_up", "1 if the WAN port has a link and is used as WAN", {}, _flag(wan.get("link")))
        details = wan.get("details") or {}
        ipv4 = wan.get("ipv4") or []
        ip = ipv4[0].get("ip", "") if ipv4 and isinstance(ipv4[0], dict) else ""
        proto = details.get("wanType") or wan.get("wanType") or ""
        if proto or ip:
            fam.add("modem_wan_info", "WAN addressing (value is always 1)",
                    {"proto": proto, "ip": ip, "gateway": wan.get("gateWay") or wan.get("gateway") or "",
                     "dns": ",".join(wan.get("dnsAddrs") or []) if isinstance(wan.get("dnsAddrs"), list)
                     else str(wan.get("dnsAddrs") or ""), "mac": wan.get("mac", "")}, 1)
        if _num(wan.get("uptime")) is not None:
            fam.add("modem_wan_uptime_seconds", "WAN connection uptime", {}, _num(wan.get("uptime")))
    ppp = d.get("xqnetwork/pppoe_status")
    if ppp is not None:
        fam.add("modem_wan_status", "WAN connection status code from the router (2 = connected; 0 = not used, e.g. AP mode)",
                {"proto": ppp.get("proto", "")}, _num(ppp.get("status")))
    v6 = (d.get("xqnetwork/get_wan6_info_v2") or {}).get("wan6_info")
    if isinstance(v6, dict):
        fam.add("modem_wan_ipv6_up", "1 if IPv6 on the WAN is up", {"mode": v6.get("ipv6_mode", "")}, _flag(v6.get("up")))
        fam.add("modem_wan_ipv6_addresses", "IPv6 addresses on the WAN", {}, len(v6.get("ip6addr") or []))


def _wifi(fam: Families, d: dict) -> None:
    detail = (d.get("xqnetwork/wifi_detail_all") or {}).get("info")
    summary = (d.get("xqsystem/information") or {}).get("wifi") or []
    widths = {}
    for w in summary:  # "channel": "40(80M)" carries the width actually in use
        m = re.search(r"\((\d+)M\)", str(w.get("channel", "")))
        if m:
            widths[w.get("ifname")] = float(m.group(1))
    for w in detail if isinstance(detail, list) else summary:
        band, ifname = _band(w), w.get("ifname", "")
        labels = {"band": band, "ifname": ifname}
        fam.add("modem_wifi_radio_enabled", "1 if the Wi-Fi interface is enabled", labels, _flag(w.get("status")))
        ch = _num(w.get("channel")) if not str(w.get("channel", "")).endswith(")") else None
        fam.add("modem_wifi_channel", "Wi-Fi channel in use", labels,
                ch if ch is not None else _num((w.get("channelInfo") or {}).get("channel")))
        width = widths.get(ifname) or _num(w.get("bandwidth")) or None
        fam.add("modem_wifi_channel_width_mhz", "Wi-Fi channel width in use", labels, width)
        fam.add("modem_wifi_ssid_info", "Wi-Fi SSID settings (value is always 1)",
                {**labels, "ssid": w.get("ssid", ""), "encryption": w.get("encryption", ""),
                 "standard": w.get("wifimode", ""), "tx_power": w.get("txpwr", ""), "hidden": w.get("hidden", ""),
                 "bandwidth_setting": "auto" if str(w.get("bandwidth")) == "0" else w.get("bandwidth", ""),
                 "ax": w.get("ax", "")}, 1)
    roam = d.get("xqnetwork/get_roam_rssi_threshold") or {}
    for key, band in (("RoamRssiThreshold2G", "2.4ghz"), ("RoamRssiThreshold5G", "5ghz")):
        fam.add("modem_wifi_roam_rssi_threshold_dbm", "Signal below which clients are steered to another AP",
                {"band": band}, _num(roam.get(key)))


def _features(fam: Families, d: dict) -> None:
    def get(ep, *path):
        v = d.get(ep)
        for p in path:
            v = v.get(p) if isinstance(v, dict) else None
        return v

    help_ = "1 if the feature is enabled on the router"
    for feature, value in (
        ("firewall", get("xqsystem/get_firewall_enable", "firewall_enable")),
        ("dos_protection", get("xqsystem/get_dos_firewall", "dos_firewall")),
        ("https_only", get("xqsystem/get_access_force_https", "on")),
        ("upnp", get("xqsystem/upnp", "status")),
        ("qos", get("misystem/qos2_list", "enable")),
        ("band_steering", get("misystem/newstatus", "bsd")),
        ("remote_web_access", get("misystem/web_access_info", "open")),
        ("elink", get("misystem/get_elink", "enable")),
        ("mac_filter", get("xqsystem/get_macfilter_info", "mode", "wanenable")),
        ("ip_filter", get("xqsystem/get_ipfilter_info", "mode", "wanenable")),
        ("telnet", get("xqsystem/fac_info", "telnet")),
        ("ssh", get("xqsystem/fac_info", "ssh")),
        ("uart", get("xqsystem/fac_info", "uart")),
        ("secure_boot", get("xqsystem/fac_info", "secboot")),
    ):
        if value is not None:
            fam.add("modem_feature_enabled", help_, {"feature": feature}, _flag(value))

    upnp = d.get("xqsystem/upnp")
    if upnp is not None:
        fam.add("modem_upnp_mappings", "Active UPnP port mappings", {}, len(upnp.get("list") or []))
    for kind, ep in (("port_forward", "xqsystem/get_pt_rules"), ("virtual_server", "xqsystem/get_vs_rules")):
        if d.get(ep) is not None:
            fam.add("modem_nat_rules", "Configured NAT rules", {"type": kind}, len(d[ep].get("list") or []))
    vpn = d.get("xqsystem/vpn_status")
    if vpn is not None:
        fam.add("modem_vpn_status", "VPN client status code from the router (4 = not configured/disconnected)",
                {}, _num(vpn.get("status")))
        fam.add("modem_vpn_uptime_seconds", "VPN connection uptime", {}, _num(vpn.get("uptime")))


# ------------------------------------------------------------------ driver
class MiWifiDriver(Driver):
    name = "miwifi"
    detect = staticmethod(detect)

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self.session = new_session(cfg)
        self.token: str | None = None
        self.init_info: dict = {}
        self.unsupported: set[str] = set()
        self.slow_cache: dict[str, dict] = {}
        self.slow_tried: dict[str, float] = {}  # endpoint -> last fetch attempt
        self.clients: dict[str, dict] = {}  # MAC -> last devicelist entry

    # ------------------------------------------------------------- session
    def _init_info(self) -> dict:
        r = self.session.get(self.cfg.url + API + "xqsystem/init_info", timeout=self.cfg.timeout)
        try:
            j = r.json()
        except ValueError:
            return {}
        return j if isinstance(j, dict) else {}

    def login(self) -> None:
        self.session = new_session(self.cfg)
        self.token = None
        self.init_info = self._init_info()
        key, device_id = DEFAULT_KEY, ""
        try:
            page = self.session.get(self.cfg.url + WEB, timeout=self.cfg.timeout).text
            m = re.search(r"key\s*:\s*['\"]([0-9a-fA-F]{16,64})['\"]", page)
            key = m.group(1) if m else key
            m = re.search(r"deviceId\s*=\s*['\"]([^'\"]*)['\"]", page)
            device_id = m.group(1) if m else ""
        except requests.RequestException as exc:
            log.debug("Could not read login page, using default key: %s", exc)
        h = hashlib.sha256 if str(self.init_info.get("newEncryptMode")) == "1" else hashlib.sha1

        def digest(s: str) -> str:
            return h(s.encode()).hexdigest()

        nonce = f"0_{device_id}_{int(time.time())}_{random.randint(0, 9999)}"
        body = {"username": self.cfg.username, "password": digest(nonce + digest(self.cfg.password + key)),
                "logtype": 2, "nonce": nonce}
        r = self.session.post(self.cfg.url + API + "xqsystem/login", data=body, timeout=self.cfg.timeout)
        try:
            resp = r.json()
        except ValueError as exc:
            raise LoginError(f"Unexpected login response (HTTP {r.status_code})") from exc
        if resp.get("code") != 0 or not resp.get("token"):
            raise LoginError(f"Login rejected (code {resp.get('code')}): {resp.get('msg') or 'wrong password?'}")
        self.token = resp["token"]
        log.info("Logged in to %s (MiWiFi %s, ROM %s)", self.cfg.url,
                 self.init_info.get("hardware", "?"), self.init_info.get("romversion", "?"))

    def logout(self) -> None:
        if not self.token:
            return
        try:
            self.session.get(f"{self.cfg.url}/cgi-bin/luci/;stok={self.token}/web/logout", timeout=self.cfg.timeout)
        except requests.RequestException as exc:
            log.debug("Logout failed: %s", exc)
        self.token = None

    def reset(self) -> None:
        self.token = None

    def get(self, endpoint: str) -> dict:
        for _ in range(2):
            if not self.token:
                self.login()
            r = self.session.get(f"{self.cfg.url}/cgi-bin/luci/;stok={self.token}/api/{endpoint}",
                                 timeout=self.cfg.timeout)
            if r.status_code == 404:
                self.unsupported.add(endpoint)
                raise _Unavailable("HTTP 404")
            try:
                j = r.json()
            except ValueError as exc:
                raise _Unavailable(f"HTTP {r.status_code}, not JSON") from exc
            if not isinstance(j, dict):
                raise _Unavailable("not a JSON object")
            if j.get("code") == 401:  # "Invalid token": session expired
                self.token = None
                continue
            if j.get("code") not in (0, None):
                raise _Unavailable(f"code {j.get('code')}: {j.get('msg', '')}")
            return j
        raise LoginError(f"Session rejected while fetching {endpoint}")

    # ------------------------------------------------------------- scraping
    def _fetch(self, endpoints: list[str]) -> tuple[dict, dict]:
        """Return ({endpoint: json}, {endpoint: unix time the response was produced})."""
        data, times = {}, {}
        for ep in endpoints:
            if ep in self.unsupported:
                continue
            start = time.time()
            try:
                data[ep] = self.get(ep)
                times[ep] = (start + time.time()) / 2
            except _Unavailable as exc:
                log.debug("Endpoint %s unavailable: %s", ep, exc)
            except requests.RequestException as exc:
                if ep == STATUS:
                    raise
                log.warning("Fetch %s failed: %s", ep, exc)
        return data, times

    def _slow(self) -> dict:
        """Cached settings endpoints; refresh the stalest few that are older than SLOW_REFRESH_SECONDS."""
        now = time.time()
        due = sorted((self.slow_tried.get(ep, 0.0), ep) for ep in SLOW_ENDPOINTS
                     if ep not in self.unsupported and now - self.slow_tried.get(ep, 0.0) >= self.cfg.slow_refresh_seconds)
        due = [ep for _, ep in due]  # oldest first
        if self.slow_tried:  # everything on the first scrape, then a few per scrape
            due = due[:SLOW_PER_SCRAPE]
        data, _ = self._fetch(due)
        for ep in due:  # a failed refresh keeps the old value and is retried after another interval
            self.slow_tried[ep] = now
        self.slow_cache.update(data)
        return dict(self.slow_cache)

    def _remember_clients(self, data: dict) -> list[dict]:
        """Last-seen devicelist entries of clients that are offline now, so their name/IP/connection survive."""
        current = {str(dev.get("mac", "")).upper() for dev in (data.get("misystem/devicelist") or {}).get("list") or []}
        for dev in (data.get("misystem/devicelist") or {}).get("list") or []:
            if dev.get("mac"):
                self.clients[str(dev["mac"]).upper()] = {**dev, "online": 0}
        seen = {str(x.get("mac", "")).upper() for x in (data.get(STATUS) or {}).get("dev") or []}
        return [dev for mac, dev in self.clients.items() if mac not in current and mac in seen]

    def scrape(self, fam: Families) -> int:
        live, times = self._fetch(LIVE_ENDPOINTS)
        if STATUS not in live:
            return 0
        data = {**self._slow(), **live}
        if self.init_info:
            data["xqsystem/init_info"] = self.init_info
        offline = self._remember_clients(data)
        build_metrics(fam, data, self.cfg.client_metrics, now=times.get("misystem/sys_time"), offline_clients=offline)
        return len(live) + len(self.slow_cache)

    def dump(self) -> dict:
        data, _ = self._fetch(LIVE_ENDPOINTS + SLOW_ENDPOINTS + DISCOVER_ONLY)
        data["xqsystem/init_info"] = self.init_info or self._init_info()
        return mask(data)
