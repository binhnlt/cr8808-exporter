"""Generates grafana-dashboard.json.  Run: python examples/gen_dashboard.py examples/grafana-dashboard.json"""
import json
import sys

DS = {"type": "prometheus", "uid": "${datasource}"}
F = 'job=~"$job", modem=~"$modem"'
RI = "$__rate_interval"
panels = []
y = 0


def target(expr, legend="", ref="A", instant=False, fmt=None):
    t = {"expr": expr, "refId": ref, "datasource": DS, "legendFormat": legend}
    if instant:
        t["instant"] = True
        t["range"] = False
    if fmt:
        t["format"] = fmt
    return t


def row(title):
    global y
    panels.append({"type": "row", "title": title, "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
                   "panels": []})
    y += 1


def add(p, x, w, h):
    p.setdefault("datasource", DS)
    p["gridPos"] = {"h": h, "w": w, "x": x, "y": y}
    panels.append(p)


def stat(title, expr, unit=None, mappings=None, thresholds=None, decimals=None, legend="", color_mode="value",
         desc=None):
    d = {"color": {"mode": "thresholds"},
         "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "text", "value": None}]}}
    if unit:
        d["unit"] = unit
    if mappings:
        d["mappings"] = mappings
    if decimals is not None:
        d["decimals"] = decimals
    p = {"type": "stat", "title": title, "fieldConfig": {"defaults": d, "overrides": []},
         "options": {"colorMode": color_mode, "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"]},
                     "textMode": "auto"},
         "targets": [target(expr, legend)]}
    if desc:
        p["description"] = desc
    return p


def ts(title, targets, unit=None, desc=None, stack=False, min_=None, overrides=None):
    d = {"custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "never",
                    "stacking": {"mode": "normal" if stack else "none"}}}
    if unit:
        d["unit"] = unit
    if min_ is not None:
        d["min"] = min_
    p = {"type": "timeseries", "title": title, "fieldConfig": {"defaults": d, "overrides": overrides or []},
         "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["mean", "max", "lastNotNull"]},
                     "tooltip": {"mode": "multi", "sort": "desc"}},
         "targets": targets}
    if desc:
        p["description"] = desc
    return p


def table(title, targets, exclude=(), rename=None, overrides=None, desc=None, sort=None, order=None):
    organize = {"excludeByName": {k: True for k in ("Time", "__name__", "job", "instance", "modem", *exclude)},
                "renameByName": rename or {}, "indexByName": {k: i for i, k in enumerate(order or [])}}
    p = {"type": "table", "title": title,
         "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": overrides or []},
         "options": {"showHeader": True, "cellHeight": "sm", "sortBy": sort or []},
         "targets": targets,
         "transformations": [{"id": "merge", "options": {}}, {"id": "organize", "options": organize}]}
    if desc:
        p["description"] = desc
    return p


def col(name, **props):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": k, "value": v} for k, v in props.items()]}


ONOFF = [{"type": "value", "options": {"0": {"text": "OFF", "color": "text"}, "1": {"text": "ON", "color": "green"}}}]
UPDOWN = [{"type": "value", "options": {"0": {"text": "DOWN", "color": "red"}, "1": {"text": "UP", "color": "green"}}}]
LINK = [{"type": "value", "options": {"0": {"text": "no link", "color": "text"}, "1": {"text": "linked", "color": "green"}}}]
YESNO = [{"type": "value", "options": {"0": {"text": "offline", "color": "text"}, "1": {"text": "online", "color": "green"}}}]
CLIENT_JOIN = f"* on(modem, mac) group_left(name, ip, connection) modem_client_info{{{F}}}"

# ---------------------------------------------------------------- Router
row("Router")
add(stat("Reachable", f"modem_up{{{F}}}", mappings=UPDOWN, color_mode="background",
         thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}]), 0, 3, 4)
add(stat("Uptime", f"modem_uptime_seconds{{{F}}}", unit="s", decimals=1), 3, 4, 4)
add(stat("CPU", f"modem_cpu_usage_percent{{{F}}}", unit="percent", decimals=0,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 70}, {"color": "red", "value": 90}]),
    7, 3, 4)
add(stat("Memory", f"modem_memory_usage_percent{{{F}}}", unit="percent", decimals=0,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 80}, {"color": "red", "value": 92}]),
    10, 3, 4)
add(stat("Clients online", f'modem_clients{{{F}, state="online"}}', decimals=0), 13, 3, 4)
add(stat("Ethernet links", f"sum by (modem) (modem_lan_port_up{{{F}}})", decimals=0,
         desc="Ethernet ports with a cable linked (the port labelled WAN is the uplink in AP mode)"), 16, 3, 4)
add(stat("Clock skew", f"modem_clock_skew_seconds{{{F}}}", unit="s", decimals=1,
         thresholds=[{"color": "red", "value": None}, {"color": "orange", "value": -60}, {"color": "green", "value": -10},
                     {"color": "orange", "value": 10}, {"color": "red", "value": 60}],
         desc="Router clock minus exporter clock. Large values mean NTP isn't working on the router."), 19, 5, 4)
y += 4
add(table("Device information", [target(f"modem_info{{{F}}}", instant=True, fmt="table")], exclude=("Value", "vendor"),
          rename={"model": "Model", "hardware": "Platform", "isp_model": "ISP model", "software_version": "ROM",
                  "display_version": "ISP ROM", "rom_channel": "Channel", "serial_number": "Serial",
                  "mac_address": "MAC", "router_name": "Name", "country": "Country", "timezone": "TZ"}), 0, 24, 4)
y += 4

# ---------------------------------------------------------------- Traffic
row("Traffic")
add(ts("Uplink throughput", [
    target(f'rate(modem_interface_rx_bytes_total{{{F}, interface="wan"}}[{RI}]) * 8', "{{modem}} download"),
    target(f'rate(modem_interface_tx_bytes_total{{{F}, interface="wan"}}[{RI}]) * 8', "{{modem}} upload", "B"),
], unit="bps", min_=0, desc="Traffic through the router's uplink (the WAN counters; in AP mode the uplink port)."),
    0, 12, 8)
add(ts("Traffic by connection type", [
    target(f"sum by (modem, connection) (rate(modem_client_download_bytes_total{{{F}}}[{RI}]) * 8 "
           f"* on(modem, mac) group_left(connection) modem_client_info{{{F}}})", "{{connection}} down"),
    target(f"sum by (modem, connection) (rate(modem_client_upload_bytes_total{{{F}}}[{RI}]) * 8 "
           f"* on(modem, mac) group_left(connection) modem_client_info{{{F}}})", "{{connection}} up", "B"),
], unit="bps", min_=0, desc="Sum of client traffic per connection type (wired, 2.4 GHz, 5 GHz, guest)."), 12, 12, 8)
y += 8
add(ts("Top clients: download", [
    target(f"topk(10, rate(modem_client_download_bytes_total{{{F}}}[{RI}]) * 8 {CLIENT_JOIN})", "{{name}}"),
], unit="bps", min_=0), 0, 12, 8)
add(ts("Top clients: upload", [
    target(f"topk(10, rate(modem_client_upload_bytes_total{{{F}}}[{RI}]) * 8 {CLIENT_JOIN})", "{{name}}"),
], unit="bps", min_=0), 12, 12, 8)
y += 8

# ---------------------------------------------------------------- Clients & Wi-Fi
row("Clients & Wi-Fi")
add(ts("Clients by connection", [
    target(f"modem_clients_by_connection{{{F}}}", "{{connection}}"),
], stack=True, min_=0), 0, 8, 8)
add(ts("Wi-Fi signal per client", [
    target(f"modem_client_wifi_signal{{{F}}} * on(modem, mac) group_left(name) modem_client_info{{{F}}}",
           "{{name}} ({{band}})"),
], desc="Raw signal value reported by the router (not dBm); higher is better."), 8, 8, 8)
add(ts("Wi-Fi channel", [
    target(f"modem_wifi_channel{{{F}}}", "{{band}} ({{ifname}})"),
], desc="Changes here show the router's auto channel selection moving."), 16, 8, 8)
y += 8
add(table("Clients", [
    target(f"modem_client_online{{{F}}} {CLIENT_JOIN}", ref="A", instant=True, fmt="table"),
    target(f"modem_client_download_bytes_total{{{F}}} {CLIENT_JOIN}", ref="B", instant=True, fmt="table"),
    target(f"modem_client_upload_bytes_total{{{F}}} {CLIENT_JOIN}", ref="C", instant=True, fmt="table"),
    target(f"modem_client_connected_seconds{{{F}}} {CLIENT_JOIN}", ref="D", instant=True, fmt="table"),
], rename={"name": "Name", "mac": "MAC", "ip": "IP", "connection": "Connection", "Value #A": "Status",
           "Value #B": "Downloaded", "Value #C": "Uploaded", "Value #D": "Connected for"},
    overrides=[col("Status", mappings=YESNO, **{"custom.cellOptions": {"type": "color-text"}}),
               col("Downloaded", unit="bytes"), col("Uploaded", unit="bytes"), col("Connected for", unit="s")],
    sort=[{"displayName": "Downloaded", "desc": True}],
    order=["name", "Value #A", "connection", "ip", "mac", "Value #B", "Value #C", "Value #D"],
    desc="Byte counters are since the router booted. Offline clients show their last session."), 0, 14, 9)
add(table("Wi-Fi radios", [
    target(f"modem_wifi_radio_enabled{{{F}}} * on(modem, band, ifname) group_left(ssid, standard, encryption, "
           f"tx_power, hidden) modem_wifi_ssid_info{{{F}}}", ref="A", instant=True, fmt="table"),
    target(f"modem_wifi_channel{{{F}}} * on(modem, band, ifname) group_left(ssid, standard, encryption, tx_power, "
           f"hidden) modem_wifi_ssid_info{{{F}}}", ref="B", instant=True, fmt="table"),
    target(f"modem_wifi_channel_width_mhz{{{F}}} * on(modem, band, ifname) group_left(ssid, standard, encryption, "
           f"tx_power, hidden) modem_wifi_ssid_info{{{F}}}", ref="C", instant=True, fmt="table"),
], rename={"band": "Band", "ifname": "Interface", "ssid": "SSID", "standard": "Standard", "encryption": "Security",
           "tx_power": "TX power", "hidden": "Hidden", "Value #A": "Radio", "Value #B": "Channel",
           "Value #C": "Width (MHz)"},
    exclude=("ifname", "hidden"),
    order=["band", "ssid", "Value #A", "Value #B", "Value #C", "standard", "encryption", "tx_power"],
    overrides=[col("Radio", mappings=ONOFF, **{"custom.cellOptions": {"type": "color-text"}})]), 14, 10, 5)
add(table("Ethernet ports", [
    target(f"modem_lan_port_up{{{F}}}", ref="A", instant=True, fmt="table"),
    target(f"modem_lan_port_max_speed_mbps{{{F}}}", ref="B", instant=True, fmt="table"),
], rename={"port": "Port", "label": "Label", "service": "Role", "Value #A": "Link", "Value #B": "Max Mbit/s"},
    overrides=[col("Link", mappings=LINK, **{"custom.cellOptions": {"type": "color-text"}})]), 14, 10, 4)
y += 9

# ---------------------------------------------------------------- System
row("System")
add(ts("CPU & memory", [
    target(f"modem_cpu_usage_percent{{{F}}}", "CPU"),
    target(f"modem_memory_usage_percent{{{F}}}", "Memory", "B"),
], unit="percent", min_=0), 0, 12, 8)
add(ts("Scrape duration", [
    target(f"modem_scrape_duration_seconds{{{F}}}", "{{modem}}"),
], unit="s", min_=0, desc="Time the exporter needed to read the router. Settings endpoints are refreshed a few at a "
                          "time, so this stays flat."), 12, 6, 8)
add(ts("Clock skew", [target(f"modem_clock_skew_seconds{{{F}}}", "{{modem}}")], unit="s"), 18, 6, 8)
y += 8
FEATURE = [{"type": "value", "options": {"0": {"text": "OFF", "color": "rgba(128, 128, 128, 0.25)"},
                                         "1": {"text": "ON", "color": "green"}}}]
feat = stat("Features", f"modem_feature_enabled{{{F}}}", mappings=FEATURE, legend="{{feature}}",
            desc="Router settings. telnet / ssh / uart ON on a stock firmware is worth a look.")
feat["options"]["colorMode"] = "background"
feat["options"]["textMode"] = "value_and_name"
feat["fieldConfig"]["defaults"]["thresholds"]["steps"] = [{"color": "rgba(128, 128, 128, 0.25)", "value": None},
                                                          {"color": "green", "value": 1}]
add(feat, 0, 24, 5)
y += 5
add({"type": "text", "title": "Notes", "options": {"mode": "markdown", "content": (
    "* Throughput is `rate()` of the byte counters. The router's own `*_rate_bytes_per_second` gauges are also exported.\n"
    "* Counters reset when the router reboots; `rate()` handles that.\n"
    "* Per-client series are keyed by MAC; names, IPs and connection type come from `modem_client_info`.\n"
    "* The `modem_client_wifi_signal` value is the raw number the router reports, not dBm.")}}, 0, 24, 4)
y += 4

dash = {
    "annotations": {"list": []}, "editable": True, "graphTooltip": 1,
    "title": "Xiaomi CR8808 - Wi-Fi router / AP", "uid": "xiaomi-cr8808",
    "tags": ["xiaomi", "miwifi", "cr8808", "wifi", "router", "network"],
    "timezone": "", "time": {"from": "now-6h", "to": "now"}, "refresh": "1m", "schemaVersion": 39,
    "description": "Xiaomi MiWiFi router / AP exporter. Use the variables at the top to pick the data source, job, "
                   "router and extra label filters.",
    "templating": {"list": [
        {"type": "datasource", "name": "datasource", "label": "Data source", "query": "prometheus", "current": {},
         "hide": 0, "refresh": 1, "regex": "", "options": []},
        {"type": "query", "name": "job", "label": "Job", "datasource": DS,
         "query": {"query": "label_values(modem_exporter_info{driver=\"miwifi\"}, job)", "refId": "job"},
         "definition": "label_values(modem_exporter_info{driver=\"miwifi\"}, job)", "refresh": 2,
         "includeAll": True, "multi": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"},
         "sort": 1, "hide": 0, "options": []},
        {"type": "query", "name": "modem", "label": "Router", "datasource": DS,
         "query": {"query": "label_values(modem_exporter_info{job=~\"$job\", driver=\"miwifi\"}, modem)", "refId": "modem"},
         "definition": "label_values(modem_exporter_info{job=~\"$job\", driver=\"miwifi\"}, modem)", "refresh": 2,
         "includeAll": True, "multi": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"},
         "sort": 1, "hide": 0, "options": []},
        {"type": "adhoc", "name": "filters", "label": "Filters", "datasource": DS, "filters": [], "hide": 0},
    ]},
    "panels": [],
}
for i, p in enumerate(panels, 1):
    p["id"] = i
dash["panels"] = panels
json.dump(dash, open(sys.argv[1], "w"), indent=2, ensure_ascii=False)
print(len(panels), "panels")
