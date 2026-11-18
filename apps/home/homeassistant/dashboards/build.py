#!/usr/bin/env python3
"""Build the Home Assistant dashboards and push them over the websocket API.

Dashboards live in HA's storage (.storage/lovelace.*), not in git; this script
is their source. Edit it, then:

    kubectl -n homeassistant port-forward svc/homeassistant 18123:8123 &
    HA_TOKEN=<long-lived token> python3 build.py            # push all
    python3 build.py --dump                                 # print JSON only

Requires `pip install websockets`. UI edits are overwritten by the next run.

Cards (all installed through the Marketplace): apexcharts-card, mini-graph-card,
flex-horseshoe-card, ha-sankey-chart, button-card, Bubble Card, Mushroom,
mini-media-player, clock-weather-card, lovelace-horizon-card, hourly-weather,
calendar-card-pro, battery-state-card, auto-entities, card-mod. Each section
deliberately uses a different visual so the pages do not read as one grid of
identical tiles.

Dashboards:
  - dashboard-home      - Home (system default panel)
  - dashboard-homelab   - Homelab: overview + per-node view, node pop-ups
  - dashboard-media     - Library: downloads, collection, calendar, Jellyfin
"""

import argparse
import asyncio
import json
import os

# ── Shared constants ────────────────────────────────────────────────────────
NODES = [  # must match generate-cluster-package.py
    ("elitedesk_g6_i7", "EliteDesk G6 i7", "mdi:desktop-tower"),
    ("elitedesk_g6_i5", "EliteDesk G6 i5", "mdi:desktop-tower"),
    ("elitedesk_g5_i7", "EliteDesk G5 i7", "mdi:desktop-tower"),
    ("pi_5_16gb_1", "Pi 5 16GB #1", "mdi:raspberry-pi"),
    ("pi_5_8gb_1", "Pi 5 8GB #1", "mdi:raspberry-pi"),
    ("pi_5_8gb_2", "Pi 5 8GB #2", "mdi:raspberry-pi"),
    ("pi_5_8gb_3", "Pi 5 8GB #3", "mdi:raspberry-pi"),
]
BLUE, GREEN, AMBER, RED, PURPLE, TEAL, PINK = (
    "#4f8cff", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#14b8a6", "#ec4899")
PALETTE = [BLUE, GREEN, AMBER, RED, PURPLE, TEAL, PINK]
FULL = {"columns": "full"}
HOMELAB_OK = "is_state('binary_sensor.homelab_problem', 'off')"


def sec(*cards, span=1):
    s = {"type": "grid", "cards": list(cards)}
    if span > 1:
        s["column_span"] = span
    return s


def view(title, path, icon, sections, max_cols=4):
    return {"type": "sections", "title": title, "path": path, "icon": icon,
            "max_columns": max_cols, "dense_section_placement": True, "sections": sections}


def heading(text, icon, badges=None):
    c = {"type": "heading", "heading": text, "icon": icon, "heading_style": "title"}
    if badges:
        c["badges"] = badges
    return c


def hb(entity, **kw):
    return {"type": "entity", "entity": entity, "show_state": True, "show_icon": True, **kw}


def separator(name, icon):
    """Bubble Card separator: a different section header style from `heading`."""
    return {"type": "custom:bubble-card", "card_type": "separator", "name": name,
            "icon": icon, "grid_options": FULL}


def bubble(entity, name, icon, kind="switch", cols="full", **kw):
    return {"type": "custom:bubble-card", "card_type": "button", "button_type": kind,
            "entity": entity, "name": name, "icon": icon, "show_state": True,
            "grid_options": {"columns": cols}, **kw}


def tile(entity, name=None, cols=6, **kw):
    c = {"type": "tile", "entity": entity, "grid_options": {"columns": cols}, **kw}
    if name:
        c["name"] = name
    return c


# ── ApexCharts helpers ──────────────────────────────────────────────────────
def apex(series, title=None, span="24h", height=220, chart_type=None, states=False,
         yaxis=None, apex_config=None, cols="full"):
    c = {"type": "custom:apexcharts-card", "graph_span": span, "series": series,
         "update_interval": "1min",
         "header": {"show": bool(title), "title": title or "", "show_states": states,
                    "colorize_states": True},
         "apex_config": {"chart": {"height": height}, "legend": {"show": len(series) > 1},
                         **(apex_config or {})},
         "grid_options": {"columns": cols}}
    if chart_type:
        c["chart_type"] = chart_type
    if yaxis:
        c["yaxis"] = yaxis
    return c


def line(entity, name, color, kind="area", avg="10min", **kw):
    return {"entity": entity, "name": name, "color": color, "type": kind,
            "stroke_width": 2, "group_by": {"func": "avg", "duration": avg}, **kw}


GRADIENT = {"type": "gradient", "gradient": {"opacityFrom": 0.45, "opacityTo": 0.03}}


def radial(items, title=None, height=240, cols="full"):
    """Concentric radial bars; items = (entity, name, color)."""
    return apex(
        [{"entity": e, "name": n, "color": col, "min": 0, "max": 100} for e, n, col in items],
        title=title, chart_type="radialBar", height=height, cols=cols,
        apex_config={"legend": {"show": True, "position": "bottom"},
                     "plotOptions": {"radialBar": {"hollow": {"size": "35%"},
                                                   "track": {"background": "rgba(127,127,127,.15)"}}}})


def donut(series, title=None, height=240):
    return apex(series, title=title, chart_type="donut", height=height,
                apex_config={"legend": {"show": True, "position": "bottom"},
                             "plotOptions": {"pie": {"donut": {"size": "68%"}}},
                             "stroke": {"width": 0}})


def now_columns(items, title=None, height=230, unit="", ymax=None):
    """One column per entity with its latest value — a 'compare right now' bar chart."""
    y = {"min": 0, "decimals": 0}
    if ymax:
        y["max"] = ymax
    # data_generator emits one point (now, current state) per series, and the
    # +30min offset puts "now" mid-window so the column group is not clipped.
    return {"span": {"offset": "+30s"}} | apex(
        [{"entity": e, "name": n, "color": col, "type": "column", "show": {"datalabels": True},
          "data_generator": "return [[new Date().getTime(), parseFloat(entity.state) || 0]];"}
         for e, n, col in items],
        title=title, span="1min", height=height, yaxis=[y],
        apex_config={"xaxis": {"labels": {"show": False}, "axisTicks": {"show": False}},
                     "legend": {"show": True, "position": "bottom"},
                     "plotOptions": {"bar": {"columnWidth": "90%", "borderRadius": 6}},
                     "dataLabels": {"formatter": f"EVAL:function(v){{return v.toFixed(0)+'{unit}'}}",
                                    "style": {"fontSize": "11px"}},
                     "tooltip": {"x": {"show": False}}})


def node_chart(nid, span="24h"):
    """CPU/RAM (left axis) and temperature (right axis) for one node."""
    return apex(
        [line(f"sensor.{nid}_cpu", "CPU", BLUE, "area", "5min", yaxis_id="pct"),
         line(f"sensor.{nid}_memory", "RAM", PURPLE, "line", "5min", yaxis_id="pct"),
         line(f"sensor.{nid}_temperature", "Temp", AMBER, "line", "5min", yaxis_id="temp")],
        span=span, height=200,
        yaxis=[{"id": "pct", "min": 0, "max": 100, "decimals": 0, "apex_config": {"tickAmount": 4}},
               {"id": "temp", "opposite": True, "decimals": 0, "apex_config": {"tickAmount": 4}}],
        apex_config={"legend": {"show": True}, "stroke": {"curve": "smooth"}, "fill": GRADIENT})


# ── Other custom-card helpers ───────────────────────────────────────────────
def horseshoe(entity, name, unit="%", stops=None, cols=4):
    stops = stops or {0: GREEN, 60: AMBER, 85: RED}
    return {
        "type": "custom:flex-horseshoe-card",
        "entities": [{"entity": entity, "name": name, "unit": unit, "decimals": 0}],
        "show": {"horseshoe_style": "lineargradient", "scale_tickmarks": True},
        "layout": {
            "states": [{"id": 0, "entity_index": 0, "xpos": 50, "ypos": 52,
                        "styles": ["font-size: 2.4em;", "font-weight: 600;"]}],
            "names": [{"id": 0, "entity_index": 0, "xpos": 50, "ypos": 95,
                       "styles": ["font-size: 1.1em;", "opacity: 0.8;"]}],
        },
        "horseshoe_scale": {"min": 0, "max": 100, "width": 6},
        "horseshoe_state": {"width": 12},
        "color_stops": {str(k): v for k, v in stops.items()},
        "grid_options": {"columns": cols},
    }


def big_stat(entity, name, icon, color, cols=8):
    """button-card stat tile: gradient wash, large number."""
    return {
        "type": "custom:button-card", "entity": entity, "name": name, "icon": icon,
        "show_state": True, "show_units": False,
        "styles": {
            "card": [{"padding": "16px"}, {"height": "118px"},
                     {"background": f"linear-gradient(135deg, {color}40 0%, {color}08 70%)"},
                     {"border": f"1px solid {color}55"}],
            "grid": [{"grid-template-areas": '"i s" "n n"'}, {"grid-template-columns": "40px 1fr"},
                     {"grid-template-rows": "1fr auto"}],
            "icon": [{"width": "30px"}, {"color": color}],
            "img_cell": [{"justify-content": "start"}, {"align-items": "start"}],
            "state": [{"justify-self": "end"}, {"align-self": "start"}, {"font-size": "34px"},
                      {"font-weight": "700"}, {"line-height": "1"}],
            "name": [{"justify-self": "start"}, {"font-size": "13px"}, {"opacity": "0.75"},
                     {"text-transform": "uppercase"}, {"letter-spacing": "0.06em"}],
        },
        "grid_options": {"columns": cols},
    }


def node_tile(nid, name, icon):
    """button-card with a status dot and four inline usage bars; tap opens #node-<id>."""
    bars = (
        "[[[\n"
        f"const g = (m) => {{ const s = states['sensor.{nid}_' + m]; return s ? (parseFloat(s.state) || 0) : 0; }};\n"
        "const bar = (label, v, unit, max, warn, crit) => {\n"
        "  const pct = Math.max(2, Math.min(100, v / max * 100));\n"
        "  const col = v >= crit ? 'var(--error-color)' : v >= warn ? 'var(--warning-color)' : 'var(--success-color)';\n"
        "  return `<div style=\"display:grid;grid-template-columns:38px 1fr 40px;align-items:center;gap:8px;font-size:11.5px;margin-top:5px\">`\n"
        "    + `<span style=\"opacity:.65;text-align:left\">${label}</span>`\n"
        "    + `<div style=\"height:6px;border-radius:3px;background:rgba(127,127,127,.22);overflow:hidden\">`\n"
        "    + `<div style=\"width:${pct}%;height:100%;background:${col};border-radius:3px\"></div></div>`\n"
        "    + `<span style=\"text-align:right;font-variant-numeric:tabular-nums\">${v.toFixed(0)}${unit}</span></div>`;\n"
        "};\n"
        "return bar('CPU', g('cpu'), '%', 100, 60, 85) + bar('RAM', g('memory'), '%', 100, 75, 90)\n"
        "  + bar('Temp', g('temperature'), '°', 90, 65, 75) + bar('Disk', g('root_disk'), '%', 100, 75, 90);\n"
        "]]]"
    )
    dot = (
        "[[[\n"
        f"const ok = states['sensor.{nid}_ready'] && states['sensor.{nid}_ready'].state === '1';\n"
        "const c = ok ? 'var(--success-color)' : 'var(--error-color)';\n"
        "return `<span style=\"display:inline-block;width:9px;height:9px;border-radius:50%;background:${c};box-shadow:0 0 8px ${c}\"></span>`;\n"
        "]]]"
    )
    return {
        "type": "custom:button-card", "entity": f"sensor.{nid}_cpu", "name": name, "icon": icon,
        "show_state": False,
        "custom_fields": {"dot": dot, "bars": bars},
        "tap_action": {"action": "navigate", "navigation_path": f"#node-{nid}"},
        "styles": {
            "card": [{"padding": "14px 16px"}],
            "grid": [{"grid-template-areas": '"i n dot" "bars bars bars"'},
                     {"grid-template-columns": "28px 1fr 14px"}, {"grid-template-rows": "auto auto"},
                     {"row-gap": "6px"}],
            "img_cell": [{"justify-content": "start"}],
            "icon": [{"width": "22px"}, {"color": "var(--primary-color)"}],
            "name": [{"justify-self": "start"}, {"font-weight": "600"}, {"font-size": "14px"},
                     {"padding-left": "6px"}],
            "custom_fields": {"bars": [{"justify-self": "stretch"}], "dot": [{"justify-self": "end"}]},
        },
        "grid_options": {"columns": 8},
    }


def node_popup(nid, name, icon):
    """Bubble Card standalone pop-up opened by #node-<id>: trend chart + live numbers."""
    return {"type": "custom:bubble-card", "card_type": "pop-up", "hash": f"#node-{nid}",
            "name": name, "icon": icon, "entity": f"sensor.{nid}_temperature", "show_state": True,
            "cards": [
        node_chart(nid, "12h"),
        {"type": "glance", "columns": 4, "entities": [
            {"entity": f"sensor.{nid}_cpu", "name": "CPU"},
            {"entity": f"sensor.{nid}_memory", "name": "RAM"},
            {"entity": f"sensor.{nid}_root_disk", "name": "Disk"},
            {"entity": f"sensor.{nid}_network_in", "name": "Net in"}]},
    ]}


def jellyfin_players(else_card=None):
    c = {"type": "custom:auto-entities", "show_empty": else_card is not None, "grid_options": FULL,
         "card": {"type": "vertical-stack"}, "card_param": "cards",
         "filter": {"include": [{"integration": "jellyfin", "domain": "media_player",
                                 "state": "/playing|paused/",
                                 "options": {"type": "custom:mini-media-player",
                                             "artwork": "full-cover", "hide": {"power": True}}}]}}
    if else_card:
        c["else"] = else_card
    return c


HOMELAB_HERO = {
    "type": "custom:mushroom-template-card",
    "entity": "binary_sensor.homelab_problem",
    "primary": "{{ 'All systems normal' if " + HOMELAB_OK + " else 'Homelab needs attention' }}",
    "secondary": (
        "{{ states('sensor.homelab_nodes_ready') }}/{{ states('sensor.homelab_nodes_total') }} nodes"
        " · {{ states('sensor.homelab_pods_running') }} pods"
        " · {{ states('sensor.homelab_argo_cd_apps') }} apps"
        "{% if not " + HOMELAB_OK + " %}\n{{ state_attr('binary_sensor.homelab_problem', 'summary') }}{% endif %}"
    ),
    "icon": "{{ 'mdi:check-decagram' if " + HOMELAB_OK + " else 'mdi:alert-decagram' }}",
    "icon_color": "{{ 'green' if " + HOMELAB_OK + " else 'red' }}",
    "multiline_secondary": True, "fill_container": True,
    "grid_options": FULL,
    "tap_action": {"action": "navigate", "navigation_path": "/dashboard-homelab"},
    "card_mod": {"style": "ha-card { background: linear-gradient(120deg, "
                          "{{ 'rgba(34,197,94,.16)' if " + HOMELAB_OK + " else 'rgba(239,68,68,.2)' }}, "
                          "transparent 70%) !important; }"},
}


# ── Home ────────────────────────────────────────────────────────────────────
def home_dashboard():
    clock = sec(
        {"type": "custom:clock-weather-card", "entity": "weather.forecast_home",
         "sun_entity": "sun.sun", "title": "Home", "forecast_rows": 5, "time_format": 24,
         "date_pattern": "cccc d LLLL", "locale": "en-GB", "show_humidity": True,
         "grid_options": FULL},
        span=2,
    )

    status = sec(
        {"type": "custom:mushroom-chips-card", "alignment": "center", "grid_options": FULL, "chips": [
            {"type": "entity", "entity": "person.silke_pilon", "use_entity_picture": True},
            {"type": "template", "entity": "switch.pi_hole", "icon": "mdi:shield-check",
             "icon_color": "{{ 'green' if is_state('switch.pi_hole','on') else 'red' }}",
             "content": "{{ states('sensor.pi_hole_ads_blocked') }} blocked"},
            {"type": "template", "icon": "mdi:download",
             "content": "{{ states('sensor.qbittorrent_download_speed') }} MB/s",
             "icon_color": "{{ 'blue' if states('sensor.qbittorrent_download_speed') | float(0) > 0 else 'disabled' }}",
             "tap_action": {"action": "navigate", "navigation_path": "/dashboard-media"}},
        ]},
        HOMELAB_HERO,
        radial([("sensor.homelab_cluster_cpu", "CPU", BLUE),
                ("sensor.homelab_cluster_memory", "RAM", PURPLE),
                ("sensor.homelab_longhorn_storage_used", "Storage", TEAL)], height=210),
    )

    sky = sec(
        heading("Sky", "mdi:weather-sunset"),
        {"type": "custom:horizon-card", "grid_options": FULL,
         "fields": {"sunrise": True, "sunset": True, "dawn": True, "noon": True, "dusk": True,
                    "azimuth": False, "elevation": True}},
        {"type": "custom:hourly-weather", "entity": "weather.forecast_home", "num_segments": 12,
         "label_spacing": 2, "show_precipitation_amounts": True, "grid_options": FULL},
    )

    bedroom = sec(
        separator("Bedroom Silke", "mdi:bed"),
        bubble("light.wiz_tunable_white_2fd14e", "Ceiling", "mdi:ceiling-light", "slider"),
        bubble("light.wiz_tunable_white_335d6c", "Bedside", "mdi:lamp", "slider"),
        bubble("automation.bedroom_wake_light_at_dawn", "Wake light at dawn", "mdi:weather-sunset-up"),
    )

    media = sec(
        separator("Speakers", "mdi:speaker-multiple"),
        *[{"type": "custom:mini-media-player", "entity": e, "name": n, "icon": i,
           "artwork": "cover", "hide": {"source": True}, "grid_options": FULL}
          for e, n, i in [("media_player.streamer_woonkamer", "Living room", "mdi:speaker"),
                          ("media_player.grote_slaapkamer", "Master bedroom", "mdi:cast-audio")]],
        jellyfin_players(),
    )

    energy = sec(
        heading("Power & grid", "mdi:lightning-bolt",
                badges=[hb("sensor.electricity_maps_grid_fossil_fuel_percentage", name="Fossil")]),
        apex([line("sensor.electricity_maps_co2_intensity", "CO₂", GREEN, "area", "30min",
                   color_threshold=[{"value": 0, "color": GREEN}, {"value": 300, "color": AMBER},
                                    {"value": 450, "color": RED}])],
             title="Grid CO₂ (48h)", span="48h", height=170, states=True,
             yaxis=[{"min": 0, "decimals": 0}], apex_config={"fill": GRADIENT}),
        horseshoe("sensor.apc_battery_charge", "UPS battery", cols=6, stops={0: RED, 25: AMBER, 50: GREEN}),
        horseshoe("sensor.apc_load", "UPS load", cols=6, stops={0: GREEN, 60: AMBER, 80: RED}),
        {"type": "statistic", "entity": "sensor.electricity_maps_co2_intensity", "name": "Avg CO₂ today",
         "period": {"calendar": {"period": "day"}}, "stat_type": "mean", "grid_options": FULL},
    )

    people = sec(
        heading("People", "mdi:account-group"),
        {"type": "map", "entities": [{"entity": "person.silke_pilon"}, {"entity": "zone.home"}],
         "hours_to_show": 24, "theme_mode": "auto", "grid_options": FULL},
        {"type": "custom:battery-state-card", "sort": {"by": "state"}, "grid_options": FULL,
         "colors": {"steps": [{"value": 20, "color": RED}, {"value": 50, "color": AMBER},
                              {"value": 101, "color": GREEN}]},
         "entities": [{"entity": "sensor.sm_g980f_battery_level", "name": "Phone"},
                      {"entity": "sensor.pixel_watch_4_battery_level", "name": "Pixel Watch"},
                      {"entity": "sensor.apc_battery_charge", "name": "UPS"}]},
    )

    lists = sec(
        heading("Shopping list", "mdi:cart-outline"),
        {"type": "todo-list", "entity": "todo.shopping_list", "grid_options": FULL},
    )

    activity = sec(
        heading("Recent activity", "mdi:history"),
        {"type": "logbook", "hours_to_show": 24, "grid_options": FULL, "target": {"entity_id": [
            "person.silke_pilon", "binary_sensor.homelab_problem", "sensor.apc_status",
            "light.wiz_tunable_white_2fd14e", "light.wiz_tunable_white_335d6c",
            "automation.bedroom_wake_light_at_dawn", "switch.pi_hole"]}},
    )

    return {"title": "Home", "views": [view("Home", "home", "mdi:home", [
        clock, status, sky, bedroom, media, energy, people, lists, activity])]}


# ── Homelab ─────────────────────────────────────────────────────────────────
def homelab_dashboard():
    status = sec(
        HOMELAB_HERO,
        {"type": "history-graph", "title": "Last 24 hours", "hours_to_show": 24, "grid_options": FULL,
         "entities": [{"entity": "binary_sensor.homelab_problem", "name": "Problems"},
                      {"entity": "sensor.apc_status", "name": "UPS"},
                      {"entity": "binary_sensor.pi_hole_status", "name": "Pi-hole"}]},
        span=2,
    )

    gauges = sec(
        heading("Cluster load", "mdi:gauge"),
        horseshoe("sensor.homelab_cluster_cpu", "CPU", cols=8),
        horseshoe("sensor.homelab_cluster_memory", "Memory", cols=8, stops={0: GREEN, 75: AMBER, 90: RED}),
        horseshoe("sensor.homelab_longhorn_storage_used", "Longhorn", cols=8, stops={0: GREEN, 70: AMBER, 85: RED}),
        span=2,
    )

    trend = sec(
        apex([line("sensor.homelab_cluster_cpu", "CPU", BLUE),
              line("sensor.homelab_cluster_memory", "Memory", PURPLE)],
             title="Cluster utilisation", span="24h", height=230, states=True,
             yaxis=[{"min": 0, "max": 100, "decimals": 0}],
             apex_config={"stroke": {"curve": "smooth"}, "fill": GRADIENT}),
        span=2,
    )

    nodes = sec(
        heading("Nodes", "mdi:server", badges=[hb("sensor.homelab_hottest_node", name="Hottest")]),
        *[node_tile(*n) for n in NODES],
        span=2,
    )

    compare = sec(
        heading("Nodes right now", "mdi:chart-bar"),
        now_columns([(f"sensor.{nid}_memory", name, PALETTE[i]) for i, (nid, name, _) in enumerate(NODES)],
                    "Memory used", unit="%", ymax=100),
        now_columns([(f"sensor.{nid}_cpu", name, PALETTE[i]) for i, (nid, name, _) in enumerate(NODES)],
                    "CPU used", unit="%"),
        span=2,
    )

    temps = sec(
        apex([line(f"sensor.{nid}_temperature", name, PALETTE[i], "line", "10min")
              for i, (nid, name, _) in enumerate(NODES)],
             title="Temperatures (24h)", span="24h", height=260,
             yaxis=[{"decimals": 0, "apex_config": {"tickAmount": 4}}],
             apex_config={"stroke": {"curve": "smooth", "width": 2},
                          "legend": {"show": True, "position": "bottom"}}),
        span=2,
    )

    workloads = sec(
        heading("Workloads", "mdi:kubernetes"),
        {"type": "glance", "columns": 3, "state_color": True, "grid_options": FULL, "entities": [
            {"entity": "sensor.homelab_argo_cd_apps", "name": "Apps"},
            {"entity": "sensor.homelab_argo_cd_apps_out_of_sync", "name": "Out of sync"},
            {"entity": "sensor.homelab_argo_cd_apps_unhealthy", "name": "Unhealthy"},
            {"entity": "sensor.homelab_pods_running", "name": "Pods"},
            {"entity": "sensor.homelab_pods_unhealthy", "name": "Failing"},
            {"entity": "sensor.homelab_containers_crash_looping", "name": "Crash loops"}]},
        {"type": "statistics-graph", "title": "Container restarts per hour",
         "entities": ["sensor.homelab_container_restarts_1h"], "chart_type": "bar",
         "period": "hour", "stat_types": ["max"], "days_to_show": 2, "hide_legend": True,
         "grid_options": FULL},
    )

    storage = sec(
        heading("Storage", "mdi:harddisk", badges=[hb("sensor.homelab_longhorn_volumes", name="Volumes")]),
        donut([{"entity": "sensor.homelab_longhorn_storage_used", "name": "Used", "color": TEAL},
               {"entity": "sensor.homelab_longhorn_storage_used", "name": "Free",
                "color": "#64748b", "transform": "return 100 - x;"}],
              title="Longhorn capacity", height=220),
        {"type": "entities", "state_color": True, "grid_options": FULL, "entities": [
            {"entity": "sensor.homelab_longhorn_volumes_degraded", "name": "Degraded volumes"},
            {"entity": "sensor.homelab_longhorn_volumes_faulted", "name": "Faulted volumes"},
            {"type": "divider"},
            {"entity": "sensor.radarr_disk_space_movies", "name": "NAS free (movies)", "icon": "mdi:nas"},
            {"entity": "sensor.sonarr_disk_space", "name": "Disk free (TV)", "icon": "mdi:nas"}]},
    )

    network = sec(
        heading("Network", "mdi:lan", badges=[hb("sensor.usw_lite_16_poe_clients", name="Clients")]),
        apex([line("sensor.cloud_gateway_ultra_cloudflare_wan_latency", "Cloudflare", AMBER, "line", "5min"),
              line("sensor.cloud_gateway_ultra_google_wan_latency", "Google", BLUE, "line", "5min")],
             title="WAN latency", span="12h", height=170, states=True,
             yaxis=[{"min": 0, "decimals": 0}], apex_config={"stroke": {"curve": "smooth"}}),
        tile("sensor.cloud_gateway_ultra_cpu_utilization", "Gateway CPU",
             features=[{"type": "bar-gauge"}], features_position="bottom"),
        tile("sensor.cloud_gateway_ultra_memory_utilization", "Gateway RAM",
             features=[{"type": "bar-gauge"}], features_position="bottom"),
    )

    dns = sec(
        heading("DNS · Pi-hole", "mdi:pi-hole", badges=[hb("binary_sensor.pi_hole_status")]),
        {"type": "custom:sankey-chart", "show_names": True, "show_states": True, "height": 210,
         "min_box_size": 6, "grid_options": FULL, "sections": [
             {"entities": [{"entity_id": "sensor.pi_hole_dns_queries", "name": "Queries", "color": BLUE,
                            "children": ["sensor.pi_hole_dns_queries_cached",
                                         "sensor.pi_hole_dns_queries_forwarded",
                                         "sensor.pi_hole_ads_blocked"]}]},
             {"entities": [{"entity_id": "sensor.pi_hole_dns_queries_cached", "name": "Cached", "color": TEAL},
                           {"entity_id": "sensor.pi_hole_dns_queries_forwarded", "name": "Forwarded", "color": PURPLE},
                           {"entity_id": "sensor.pi_hole_ads_blocked", "name": "Blocked", "color": RED}]}]},
        bubble("switch.pi_hole", "Ad blocking", "mdi:shield-check"),
    )

    power = sec(
        heading("UPS", "mdi:battery-charging", badges=[hb("sensor.apc_status")]),
        radial([("sensor.apc_battery_charge", "Battery", GREEN), ("sensor.apc_load", "Load", AMBER)],
               height=200),
        {"type": "custom:mini-graph-card", "name": "Mains",
         "entities": [{"entity": "sensor.apc_input_voltage", "color": PINK}],
         "hours_to_show": 24, "points_per_hour": 4, "line_width": 2,
         "show": {"labels": True, "fill": "fade"}, "grid_options": {"columns": 6}},
        {"type": "sensor", "entity": "sensor.apc_battery_runtime", "name": "Runtime",
         "graph": "line", "hours_to_show": 24, "grid_options": {"columns": 6}},
    )

    popups = sec(*[node_popup(*n) for n in NODES])

    overview = view("Overview", "overview", "mdi:view-dashboard",
                    [status, gauges, trend, nodes, compare, temps, workloads, storage,
                     network, dns, power, popups])

    detail = view("Nodes", "nodes", "mdi:server", [
        sec(heading(name, icon, badges=[hb(f"sensor.{nid}_root_disk", name="Disk", icon="mdi:harddisk"),
                                         hb(f"sensor.{nid}_temperature", name="Temp")]),
            node_chart(nid, "24h"),
            {"type": "custom:mini-graph-card", "name": "Network in",
             "entities": [{"entity": f"sensor.{nid}_network_in", "color": TEAL}],
             "hours_to_show": 24, "points_per_hour": 6, "height": 60, "line_width": 2,
             "show": {"labels": False, "fill": "fade", "icon": False}, "grid_options": FULL})
        for nid, name, icon in NODES])

    return {"title": "Homelab", "views": [overview, detail]}


# ── Library ─────────────────────────────────────────────────────────────────
def media_dashboard():
    stats = sec(
        big_stat("sensor.sonarr_shows", "Series", "mdi:television-classic", BLUE),
        big_stat("sensor.radarr_movies", "Movies", "mdi:movie-open", PINK),
        big_stat("sensor.lidarr_albums", "Albums", "mdi:album", TEAL),
        big_stat("sensor.qbittorrent_active_torrents", "Active", "mdi:download", GREEN),
        big_stat("sensor.sonarr_wanted", "Missing eps", "mdi:magnify", AMBER),
        big_stat("sensor.qbittorrent_global_ratio", "Ratio", "mdi:scale-balance", PURPLE),
        span=2,
    )

    absfmt = "EVAL:function(v){return Math.abs(v).toFixed(1)+' MB/s'}"
    speed = sec(
        heading("Bandwidth", "mdi:swap-vertical",
                badges=[hb("sensor.qbittorrent_connection_status", name="Port")]),
        apex([line("sensor.qbittorrent_download_speed", "Down", BLUE, "area", "2min"),
              line("sensor.qbittorrent_upload_speed", "Up", GREEN, "area", "2min",
                   transform="return -x;")],
             span="6h", height=220,
             yaxis=[{"decimals": 1, "apex_config": {"labels": {"formatter": absfmt}}}],
             apex_config={"stroke": {"curve": "smooth"}, "fill": GRADIENT,
                          "tooltip": {"y": {"formatter": absfmt}}}),
        bubble("switch.qbittorrent_alternative_speed", "Slow mode", "mdi:speedometer-slow", cols=6),
        bubble("sensor.qbittorrent_status", "qBittorrent", "mdi:download-circle", "state", cols=6),
        span=2,
    )

    torrents = sec(
        heading("Torrents", "mdi:magnet"),
        *[{"type": "entity", "entity": e, "name": n, "icon": i, "state_color": True,
           "grid_options": {"columns": 6}}
          for e, n, i in [("sensor.qbittorrent_all_torrents", "Total", "mdi:magnet"),
                          ("sensor.qbittorrent_inactive_torrents", "Seeding idle", "mdi:sleep"),
                          ("sensor.qbittorrent_paused_torrents", "Paused", "mdi:pause-circle"),
                          ("sensor.qbittorrent_errored_torrents", "Errored", "mdi:alert-circle")]],
        {"type": "entities", "grid_options": FULL, "entities": [
            {"entity": "sensor.qbittorrent_all_time_download", "name": "All-time down", "icon": "mdi:arrow-down-bold"},
            {"entity": "sensor.qbittorrent_all_time_upload", "name": "All-time up", "icon": "mdi:arrow-up-bold"}]},
    )

    backlog = sec(
        heading("Queues & backlog", "mdi:tray-full"),
        now_columns([("sensor.sonarr_queue", "Sonarr queue", BLUE),
                     ("sensor.radarr_queue", "Radarr queue", PINK),
                     ("sensor.lidarr_queue", "Lidarr queue", TEAL),
                     ("sensor.lidarr_wanted", "Albums missing", AMBER)], height=210),
        {"type": "custom:mini-graph-card", "name": "Missing episodes (7d)",
         "entities": [{"entity": "sensor.sonarr_wanted", "color": AMBER}],
         "hours_to_show": 168, "points_per_hour": 0.5, "line_width": 2, "height": 80,
         "show": {"fill": "fade", "labels": False}, "grid_options": FULL},
    )

    calendar = sec(
        heading("Coming up", "mdi:calendar-star"),
        {"type": "custom:calendar-card-pro", "days_to_show": 14, "show_location": False,
         "entities": [{"entity": "calendar.sonarr", "color": BLUE, "label": "📺"},
                      {"entity": "calendar.radarr", "color": PINK, "label": "🎬"}],
         "grid_options": FULL},
        span=2,
    )

    jellyfin = sec(
        heading("Now playing", "mdi:play-network", badges=[hb("sensor.music_active_clients", name="Clients")]),
        jellyfin_players({"type": "markdown", "content": "### Nothing playing\nJellyfin sessions show up here."}),
        span=2,
    )

    return {"title": "Library", "views": [view("Library", "media", "mdi:filmstrip-box-multiple", [
        stats, speed, torrents, backlog, calendar, jellyfin])]}


# ── Push ────────────────────────────────────────────────────────────────────
DASHBOARDS = [
    ("dashboard-home", "Home", "mdi:home", home_dashboard),
    ("dashboard-homelab", "Homelab", "mdi:server", homelab_dashboard),
    ("dashboard-media", "Library", "mdi:filmstrip-box-multiple", media_dashboard),
]


async def push(url, token):
    import websockets

    async with websockets.connect(url, max_size=2**26) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        assert json.loads(await ws.recv())["type"] == "auth_ok", "auth failed"
        n = 0

        async def call(msg):
            nonlocal n
            n += 1
            await ws.send(json.dumps({**msg, "id": n}))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == n and r.get("type") == "result":
                    if not r["success"]:
                        raise RuntimeError(f"{msg['type']}: {r['error']}")
                    return r["result"]

        existing = {d["url_path"] for d in await call({"type": "lovelace/dashboards/list"})}
        for url_path, title, icon, build in DASHBOARDS:
            if url_path not in existing:
                await call({"type": "lovelace/dashboards/create", "url_path": url_path,
                            "title": title, "icon": icon, "show_in_sidebar": True,
                            "require_admin": False, "mode": "storage"})
            await call({"type": "lovelace/config/save", "url_path": url_path, "config": build()})
            print(f"saved {url_path}")
        # Open on our Home dashboard instead of HA's built-in "home" panel.
        system = (await call({"type": "frontend/get_system_data", "key": "core"}))["value"] or {}
        await call({"type": "frontend/set_system_data", "key": "core",
                    "value": {**system, "default_panel": "dashboard-home"}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="print JSON, do not push")
    ap.add_argument("--url", default=os.environ.get("HA_WS", "ws://127.0.0.1:18123/api/websocket"))
    a = ap.parse_args()
    if a.dump:
        print(json.dumps({u: b() for u, _, _, b in DASHBOARDS}, indent=1))
        return
    asyncio.run(push(a.url, os.environ["HA_TOKEN"]))


if __name__ == "__main__":
    main()
