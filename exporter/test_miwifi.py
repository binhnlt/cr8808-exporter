"""Tests for the MiWiFi driver and the collector.

Run with:  pytest exporter/
No router or network access required: `fake_router` serves responses with the
same shape as a real CR8808, with identifying values replaced by dummy data.
"""
import calendar

import fake_router
import pytest
from modem_exporter import miwifi
from modem_exporter.base import Families
from modem_exporter.collector import ModemCollector
from modem_exporter.config import Config


def samples(fam, name):
    """{frozenset(label items): value} for one metric, without the constant `modem` label."""
    return {frozenset((k, v) for k, v in key if k != "modem"): value
            for key, value in fam.samples.get(name, {}).items()}


def one(fam, name, **labels):
    for key, value in samples(fam, name).items():
        if all((k, v) in key for k, v in labels.items()):
            return value
    raise AssertionError(f"{name}{labels} not found in {samples(fam, name)}")


def labels_of(fam, name, **match):
    for key in samples(fam, name):
        if all((k, v) in key for k, v in match.items()):
            return dict(key)
    raise AssertionError(f"{name}{match} not found")


@pytest.fixture
def fam():
    f = Families("test")
    data = {**fake_router.API, "xqsystem/init_info": fake_router.init_info()}
    # 16:51:32 CST-8 on 2026-09-25 == 08:51:32 UTC
    now = calendar.timegm((2026, 9, 25, 8, 51, 30, 0, 0, 0))
    miwifi.build_metrics(f, data, now=now)
    return f


# --- helpers -----------------------------------------------------------------
@pytest.mark.parametrize("tz,offset", [("CST-8", 28800), ("UTC0", 0), ("EST5EDT", -18000),
                                       ("<+07>-7", 25200), ("IST-5:30", 19800), ("", None)])
def test_tz_offset(tz, offset):
    assert miwifi._tz_offset(tz) == offset


def test_unit_parsers():
    assert miwifi._bytes("256MB") == 256 * 1024**2
    assert miwifi._bytes("1GB") == 1024**3
    assert miwifi._hertz("1000MHz") == 1e9
    assert miwifi._hertz("1.4GHz") == pytest.approx(1.4e9)
    assert miwifi._mbps("1G") == 1000
    assert miwifi._mbps("2.5G") == 2500
    assert miwifi._mbps("100M") == 100
    assert miwifi._bytes("lots") is None


def test_band_detection():
    assert miwifi._band({"wifimode": "11axg"}) == "2.4ghz"
    assert miwifi._band({"wifimode": "11axa"}) == "5ghz"
    assert miwifi._band({"channel": "6"}) == "2.4ghz"
    assert miwifi._band({"channel": "149"}) == "5ghz"
    assert miwifi._band({"channel": "0", "ifname": "wl2"}) == "wl2"


# --- system ------------------------------------------------------------------
def test_info_labels(fam):
    info = labels_of(fam, "modem_info")
    assert info["model"] == "xiaomi.router.cr8818"
    assert info["hardware"] == "CR8818"
    assert info["isp_model"] == "CR8808"
    assert info["software_version"] == "6.20.92"
    assert info["serial_number"] == "12345/A1B234567"
    assert info["router_name"] == "Living room AP"
    assert info["timezone"] == "CST-8"


def test_system_gauges(fam):
    assert one(fam, "modem_uptime_seconds") == pytest.approx(174511.07)
    assert one(fam, "modem_cpu_usage_percent") == pytest.approx(12.5)
    assert one(fam, "modem_cpu_cores") == 2
    assert one(fam, "modem_cpu_frequency_hertz") == 1e9
    assert one(fam, "modem_memory_usage_percent") == pytest.approx(61)
    assert one(fam, "modem_memory_total_bytes") == 256 * 1024**2
    assert one(fam, "modem_clock_skew_seconds") == pytest.approx(2)
    assert one(fam, "modem_network_mode") == 3
    # temperature 0 = no sensor: not exported
    assert not samples(fam, "modem_temperature_celsius")


# --- clients / traffic -------------------------------------------------------
def test_uplink_counters(fam):
    assert one(fam, "modem_interface_rx_bytes_total", interface="wan") == 2937580288
    assert one(fam, "modem_interface_tx_bytes_total", interface="wan") == 197579169
    assert one(fam, "modem_interface_rx_rate_bytes_per_second", interface="wan") == 1000
    assert fam.defs["modem_interface_rx_bytes_total"][0] == "counter"


def test_client_counts(fam):
    assert one(fam, "modem_clients", state="online") == 2
    assert one(fam, "modem_clients", state="known") == 3
    assert one(fam, "modem_clients_by_connection", connection="wifi_2.4ghz") == 1
    assert one(fam, "modem_clients_by_connection", connection="wifi_5ghz") == 1
    assert one(fam, "modem_clients_by_connection", connection="wired") == 0
    assert one(fam, "modem_wifi_clients", band="2.4ghz") == 1


def test_per_client(fam):
    assert one(fam, "modem_client_download_bytes_total", mac="AA:BB:CC:00:00:01") == 8164283
    assert one(fam, "modem_client_upload_rate_bytes_per_second", mac="AA:BB:CC:00:00:01") == 5
    assert one(fam, "modem_client_online", mac="AA:BB:CC:00:00:01") == 1
    assert one(fam, "modem_client_wifi_signal", mac="AA:BB:CC:00:00:02", band="5ghz") == 92
    phone = labels_of(fam, "modem_client_info", mac="AA:BB:CC:00:00:01")
    assert phone == {"mac": "AA:BB:CC:00:00:01", "name": "phone", "ip": "192.168.31.99",
                     "connection": "wifi_2.4ghz", "is_ap": "0"}
    # counters carry only `mac`, so a renamed / re-addressed client keeps one series
    assert all({k for k, _ in key} == {"mac"} for key in samples(fam, "modem_client_download_bytes_total"))


def test_offline_client_keeps_details_from_known_list(fam):
    tv = labels_of(fam, "modem_client_info", mac="AA:BB:CC:00:00:03")
    assert tv["name"] == "living-room-tv"
    assert tv["connection"] == "wifi_5ghz"
    assert one(fam, "modem_client_online", mac="AA:BB:CC:00:00:03") == 0


def test_client_metrics_can_be_disabled():
    f = Families("test")
    miwifi.build_metrics(f, dict(fake_router.API), client_metrics=False)
    assert not samples(f, "modem_client_info")
    assert not samples(f, "modem_client_download_bytes_total")
    assert one(f, "modem_clients", state="online") == 2


# --- LAN / WAN / Wi-Fi -------------------------------------------------------
def test_lan(fam):
    assert one(fam, "modem_lan_port_up", port="4", label="WAN") == 1
    assert one(fam, "modem_lan_port_up", port="1") == 0
    assert one(fam, "modem_lan_port_max_speed_mbps", port="1") == 1000
    assert labels_of(fam, "modem_lan_info")["ip"] == "192.168.31.3"
    assert one(fam, "modem_dhcp_lease_time_seconds") == 12 * 3600
    assert one(fam, "modem_dhcp_server_enabled") == 1


def test_wan(fam):
    assert one(fam, "modem_wan_link_up") == 0
    assert one(fam, "modem_wan_status") == 0
    assert one(fam, "modem_wan_ipv6_up") == 0


def test_wifi(fam):
    assert one(fam, "modem_wifi_radio_enabled", band="2.4ghz") == 1
    assert one(fam, "modem_wifi_channel", band="5ghz", ifname="wl0") == 40
    assert one(fam, "modem_wifi_channel_width_mhz", band="5ghz") == 80
    ssid = labels_of(fam, "modem_wifi_ssid_info", band="2.4ghz")
    assert ssid["ssid"] == "HomeWiFi"
    assert ssid["standard"] == "11axg"
    assert ssid["bandwidth_setting"] == "auto"
    assert one(fam, "modem_wifi_roam_rssi_threshold_dbm", band="2.4ghz") == -65


def test_features(fam):
    assert one(fam, "modem_feature_enabled", feature="firewall") == 1
    assert one(fam, "modem_feature_enabled", feature="upnp") == 1
    assert one(fam, "modem_feature_enabled", feature="telnet") == 0
    assert one(fam, "modem_feature_enabled", feature="band_steering") == 1
    assert one(fam, "modem_upnp_mappings") == 2
    assert one(fam, "modem_nat_rules", type="port_forward") == 1


def test_no_secrets_exported(fam):
    assert "hunter22" not in str(fam.samples)


def test_mask():
    masked = miwifi.mask({"ssid": "x", "password": "hunter22", "list": [{"passwd": "a", "key": ""}]})
    assert masked == {"ssid": "x", "password": "***", "list": [{"passwd": "***", "key": ""}]}


# --- end to end against the fake router -------------------------------------
@pytest.fixture
def router():
    server = fake_router.start()
    fake_router.CALLS.clear()
    fake_router.TOKENS.clear()
    fake_router.STATE.update(new_encrypt_mode=0, fail_logins=False)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def cfg(url, **kw):
    return Config(url=url, password=fake_router.PASSWORD, **{"timeout": 3, **kw})


def test_detect(router):
    from modem_exporter.base import new_session
    c = cfg(router)
    assert miwifi.detect(new_session(c), c)
    dead = cfg("http://127.0.0.1:1", timeout=1)
    assert not miwifi.detect(new_session(dead), dead)


@pytest.mark.parametrize("mode", [0, 1])
def test_login_sha1_and_sha256(router, mode):
    fake_router.STATE["new_encrypt_mode"] = mode
    drv = miwifi.MiWifiDriver(cfg(router))
    drv.login()
    assert drv.token in fake_router.TOKENS


def test_scrape_end_to_end_and_tiering(router):
    drv = miwifi.MiWifiDriver(cfg(router))
    f = Families("test")
    assert drv.scrape(f) > 30
    assert one(f, "modem_uptime_seconds") > 0
    assert labels_of(f, "modem_info")["model"] == "xiaomi.router.cr8818"
    # second scrape: only the live endpoints (settings are cached), no new login
    fake_router.CALLS.clear()
    drv.scrape(Families("test"))
    api_calls = [c for c in fake_router.CALLS if "/api/" in c]
    assert len(api_calls) == len(miwifi.LIVE_ENDPOINTS)
    assert not any("login" in c for c in fake_router.CALLS)


def test_only_allowlisted_get_requests(router):
    drv = miwifi.MiWifiDriver(cfg(router))
    drv.dump()
    drv.scrape(Families("test"))
    allowed = set(miwifi.LIVE_ENDPOINTS + miwifi.SLOW_ENDPOINTS + miwifi.DISCOVER_ONLY)
    for call in fake_router.CALLS:
        method, path = call.split(" ", 1)
        if method == "POST":
            assert path == "/cgi-bin/luci/api/xqsystem/login"
        elif "/api/" in path and ";stok=" in path:
            assert path.split("/api/", 1)[1] in allowed


def test_expired_token_triggers_relogin(router):
    drv = miwifi.MiWifiDriver(cfg(router))
    drv.login()
    fake_router.TOKENS.clear()  # router forgot the session
    assert drv.get("misystem/status")["code"] == 0
    assert sum("login" in c for c in fake_router.CALLS) == 2


def test_unsupported_endpoint_is_skipped_afterwards(router):
    drv = miwifi.MiWifiDriver(cfg(router))
    del_ = fake_router.API.pop("xqsystem/vpn_status")
    try:
        drv.scrape(Families("test"))
        assert "xqsystem/vpn_status" in drv.unsupported
    finally:
        fake_router.API["xqsystem/vpn_status"] = del_


def test_dump_masks_secrets(router):
    drv = miwifi.MiWifiDriver(cfg(router))
    dumped = drv.dump()
    assert "hunter22" not in str(dumped)
    assert "misystem/status" in dumped


def _families(collector):
    return {m.name: m for m in collector.collect()}


def test_collector_up(router):
    col = ModemCollector(cfg(router, name="ap1"))
    fams = _families(col)
    assert fams["modem_up"].samples[0].value == 1
    assert fams["modem_up"].samples[0].labels == {"modem": "ap1"}
    assert fams["modem_exporter_info"].samples[0].labels["driver"] == "miwifi"
    assert "modem_uptime_seconds" in fams


def test_collector_unreachable():
    col = ModemCollector(cfg("http://127.0.0.1:1", timeout=1))
    fams = _families(col)
    assert fams["modem_up"].samples[0].value == 0
    assert "modem_uptime_seconds" not in fams


def test_collector_login_backoff(router):
    fake_router.STATE["fail_logins"] = True
    col = ModemCollector(cfg(router, login_backoff_seconds=600, cache_seconds=0))
    fams = _families(col)
    assert fams["modem_up"].samples[0].value == 0
    assert fams["modem_login_backoff_seconds"].samples[0].value > 500
    logins = sum("login" in c for c in fake_router.CALLS)
    _families(col)  # during backoff the router is not contacted again
    assert sum("login" in c for c in fake_router.CALLS) == logins
