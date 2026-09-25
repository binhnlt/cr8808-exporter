"""Tiny fake Xiaomi MiWiFi router used to test the exporter without real hardware.

Run: python fake_router.py 8080   (password: secret)

Responses have the same shape as those captured from a real CR8808 (CR8818 platform,
ROM 6.20.92, wired-AP mode) with identifying values (serials, MACs, IPs, names)
replaced by dummy data.
"""
import hashlib
import json
import re
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PASSWORD = "secret"
KEY = "a2ffa5c9be07488bbb04a3a47d3c5f6a"
TOKENS: set[str] = set()
CALLS: list[str] = []
STATE = {"new_encrypt_mode": 0, "fail_logins": False}

LOGIN_PAGE = f"""<html><head><title>小米路由器</title></head><body><script>
    var deviceId = '';
    var Encrypt = {{ key: '{KEY}', iv: '64175472480004614961023454661220', nonce: null }};
</script></body></html>"""

HW = {"mac": "00:11:22:33:44:55", "platform": "CR8818", "version": "6.20.92", "channel": "release",
      "ispName": "CR8808", "DisplayRomVer": "1.4.01", "sn": "12345/A1B234567"}
WIFI = [
    {"ifname": "wl1", "channelInfo": {"bandwidth": "0", "bandList": ["20", "40"], "channel": 1},
     "encryption": "psk2", "wifimode": "11axg", "bandwidth": "0", "status": "1", "mode": "Master",
     "ssid": "HomeWiFi", "device": "wifi0.network1", "ax": "1", "hidden": "0", "password": "hunter22",
     "channel": "1", "txpwr": "max", "txbf": "3", "signal": -100},
    {"ifname": "wl0", "channelInfo": {"bandwidth": "0", "bandList": ["20", "40", "80", "160"], "channel": 40},
     "encryption": "psk2", "wifimode": "11axa", "bandwidth": "0", "status": "1", "mode": "Master",
     "ssid": "HomeWiFi", "device": "wifi1.network1", "ax": "1", "hidden": "0", "password": "hunter22",
     "channel": "40", "txpwr": "max", "txbf": "3", "signal": -94},
]
LAN = {"status": 1, "mac": "00:11:22:33:44:55", "uptime": 174465, "ipv4": [{"mask": "255.255.255.0", "ip": "192.168.31.3"}]}


def _dev(mac, name, type_, ip, online=1):
    return {"mac": mac, "oname": "", "isap": 0, "parent": "", "authority": {"wan": 1}, "online": online,
            "name": name, "ip": [{"downspeed": "0", "online": "160170", "active": 1, "upspeed": "0", "ip": ip}],
            "statistics": {"downspeed": "0", "online": "160170", "upspeed": "0"}, "icon": "", "type": type_}


PHONE = _dev("AA:BB:CC:00:00:01", "phone", 1, "192.168.31.99")
LAPTOP = _dev("AA:BB:CC:00:00:02", "AA:BB:CC:00:00:02", 2, "192.168.31.102")
TV = _dev("AA:BB:CC:00:00:03", "living-room-tv", 2, "192.168.31.131", online=0)

API = {
    "misystem/status": {
        "dev": [
            {"mac": "AA:BB:CC:00:00:01", "maxdownloadspeed": "110", "isap": 0, "upload": "37936699", "upspeed": "5",
             "downspeed": "7", "online": "160167", "devname": "AA:BB:CC:00:00:01", "maxuploadspeed": "82",
             "download": "8164283"},
            {"mac": "AA:BB:CC:00:00:02", "maxdownloadspeed": "0", "isap": 0, "upload": "57809", "upspeed": "0",
             "downspeed": "0", "online": "817", "devname": "AA:BB:CC:00:00:02", "maxuploadspeed": "0",
             "download": "302120"},
            {"mac": "AA:BB:CC:00:00:03", "maxdownloadspeed": "0", "isap": 0, "upload": "159457", "upspeed": "0",
             "downspeed": "0", "online": "31236", "devname": "AA:BB:CC:00:00:03", "maxuploadspeed": "0",
             "download": "170430"},
        ],
        "code": 0, "mem": {"usage": 0.61, "total": "256MB", "hz": "1333MHz", "type": "DDR3"}, "temperature": 0,
        "count": {"all_without_mash": 3, "online": 2, "all": 3, "online_without_mash": 2}, "hardware": HW,
        "upTime": "174511.07", "cpu": {"core": 2, "hz": "1000MHz", "load": 0.125},
        "wan": {"downspeed": "1000", "maxdownloadspeed": "10161", "devname": "nil", "upload": "197579169",
                "upspeed": "200", "maxuploadspeed": "67174", "download": "2937580288"},
    },
    "misystem/newstatus": {"cap_count": 0, "re_count": 0, "count": 2, "code": 0, "hardware": HW, "bsd": 1,
                           "2g": {"online_sta_count": 1, "ssid": "HomeWiFi", "passwd": "hunter22"},
                           "5g": {"online_sta_count": 1, "ssid": "HomeWiFi", "passwd": "hunter22"}},
    "misystem/devicelist": {"mac": "", "list": [PHONE, LAPTOP], "code": 0},
    "misystem/router_name": {"locale": "Home", "name": "Living room AP", "code": 0},
    "misystem/sys_time": {"role": "agent", "code": 0, "time": {"min": 51, "day": 25, "index": "2.84", "month": 9,
                          "year": 2026, "sec": 32, "hour": 16, "timezone": "CST-8"}},
    "misystem/topo_graph": {"show": 0, "code": 0, "graph": {"ip": "192.168.31.3", "hardware": "CR8818", "mode": 3}},
    "misystem/messages": {"messages": [], "count": 0, "code": 0},
    "misystem/get_ps_map": {"code": 0, "ports": [
        {"port": "1", "index": "1", "label": "1", "speed": "1G", "service": "LAN"},
        {"port": "2", "index": "2", "label": "2", "speed": "1G", "service": "LAN"},
        {"port": "3", "index": "3", "label": "3", "speed": "1G", "service": "LAN"},
        {"port": "4", "index": "4", "label": "WAN", "speed": "1G", "service": "LAN"}]},
    "misystem/qos2_list": {"enable": "0", "devs": {}, "code": 0},
    "misystem/web_access_info": {"open": False, "code": 0},
    "misystem/get_elink": {"enable": 0, "code": 0},
    "xqnetwork/lan_info": {"linkList": [0, 0, 0, 1], "info": LAN, "code": 0},
    "xqnetwork/lan_dhcp": {"code": 0, "info": {"leasetimeUnit": "h", "leasetime": "12h", "limit": "250", "start": "5",
                                               "leasetimeNum": "12", "ignore": "0"}},
    "xqnetwork/wan_info": {"info": {"ipv6_info": {"wanType": "none"}, "ipv6_show": 1, "link": 0}, "code": 0},
    "xqnetwork/pppoe_status": {"gw": "", "dns": [], "code": 0, "status": 0, "ip": {"mask": "", "address": ""}},
    "xqnetwork/get_wan6_info_v2": {"code": 0, "wan6_info": {"ip6addr": [], "ipv6_mode": "native", "up": False}},
    "xqnetwork/get_netmode": {"netmode": 3, "code": 0},
    "xqnetwork/wifi_detail_all": {"bsd": 0, "info": WIFI, "code": 0},
    "xqnetwork/wifi_connect_devices": {"code": 0, "list": [{"mac": "AA:BB:CC:00:00:01", "wifiIndex": 1, "signal": 112},
                                                           {"mac": "AA:BB:CC:00:00:02", "wifiIndex": 2, "signal": 92}]},
    "xqnetwork/get_roam_rssi_threshold": {"RoamRssiThreshold5G": "-60", "RoamRssiThreshold2G": "-65", "code": 0},
    "xqnetwork/macbind_info": {"lanmask": "255.255.255.0", "devicelist": [], "list": [], "code": 0},
    "xqnetwork/wifi_macfilter_info": {"weblist": [], "flist": [TV, PHONE, LAPTOP], "code": 0},
    "xqsystem/information": {"lan": LAN, "code": 0,
                             "wifi": [{**WIFI[0], "channel": "1(20M)"}, {**WIFI[1], "channel": "40(80M)"}]},
    "xqsystem/fac_info": {"telnet": False, "init": True, "ssh": False, "version": "6.20.92", "uart": False,
                          "secboot": False},
    "xqsystem/get_firewall_enable": {"firewall_enable": "1", "code": 0},
    "xqsystem/get_dos_firewall": {"dos_firewall": "0", "code": 0},
    "xqsystem/get_access_force_https": {"on": 0, "code": 0},
    "xqsystem/get_macfilter_info": {"code": 0, "mode": {"wan": "black", "wanenable": "1"}},
    "xqsystem/get_ipfilter_info": {"code": 0, "mode": {"wan": "black", "wanenable": "0"}},
    "xqsystem/get_pt_rules": {"status": "1", "list": [{"name": "ssh"}], "code": 0},
    "xqsystem/get_vs_rules": {"status": "1", "list": [], "code": 0},
    "xqsystem/upnp": {"status": 1, "list": [{"proto": "UDP"}, {"proto": "TCP"}], "code": 0},
    "xqsystem/vpn_status": {"status": 4, "uptime": 0, "code": 0},
}


def init_info():
    return {"code": 0, "inited": 1, "hardware": "CR8818", "romversion": "6.20.92", "countrycode": "CN",
            "id": "12345/A1B234567", "routername": "Living room AP", "model": "xiaomi.router.cr8818",
            "wifi_ap": 1, "newEncryptMode": STATE["new_encrypt_mode"], "language": "zh_cn"}


def expected_password(nonce: str) -> str:
    h = hashlib.sha256 if STATE["new_encrypt_mode"] == 1 else hashlib.sha1
    return h((nonce + h((PASSWORD + KEY).encode()).hexdigest()).encode()).hexdigest()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype="application/json"):
        data = (json.dumps(body) if not isinstance(body, str) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        CALLS.append("GET " + path)
        if path == "/cgi-bin/luci/web":
            return self._send(200, LOGIN_PAGE, "text/html")
        if path == "/cgi-bin/luci/api/xqsystem/init_info":
            return self._send(200, init_info())
        m = re.fullmatch(r"/cgi-bin/luci/;stok=([^/]*)/(api|web)/(.+)", path)
        if not m:
            return self._send(404, "No page is registered", "text/plain")
        token, kind, rest = m.groups()
        if token not in TOKENS:
            return self._send(200, {"code": 401, "msg": "Invalid token"})
        if kind == "web" and rest == "logout":
            TOKENS.discard(token)
            return self._send(200, "<html></html>", "text/html")
        if rest in API:
            return self._send(200, API[rest])
        return self._send(404, f"No page is registered at '/api/{rest}'.", "text/plain")

    def do_POST(self):
        CALLS.append("POST " + self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if self.path != "/cgi-bin/luci/api/xqsystem/login":
            return self._send(404, "not found", "text/plain")
        form = dict(urllib.parse.parse_qsl(body))
        if STATE["fail_logins"] or form.get("password") != expected_password(form.get("nonce", "")):
            return self._send(200, {"code": 401, "msg": "Not authorized"})
        token = secrets.token_hex(16)
        TOKENS.add(token)
        return self._send(200, {"code": 0, "token": token, "url": f"/cgi-bin/luci/;stok={token}/web/home"})

    def log_message(self, *args):
        pass


def start(port: int = 0) -> ThreadingHTTPServer:
    import threading

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"Fake MiWiFi router on http://127.0.0.1:{port} (password: {PASSWORD})")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
