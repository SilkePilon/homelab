#!/usr/bin/env python3
"""Build the Home Assistant dashboards and push them over the websocket API.

Dashboards live in HA's storage (.storage/lovelace.*), not in git; this script
is their source. Edit it, then:

    kubectl -n homeassistant port-forward svc/homeassistant 18123:8123 &
    HA_TOKEN=<long-lived token> python3 build.py            # push all
    python3 build.py --dump                                 # print JSON only

Requires `pip install websockets`. Cards used: core + Mushroom, mini-graph-card,
auto-entities, card-mod (all installed through the Marketplace).

Dashboards:
  - dashboard-home      - home: rooms, media, power, people, homelab summary
                          (set as the system default panel)
  - dashboard-homelab   - cluster, nodes, network, storage, power
  - dashboard-media     - Library: downloads, library, calendar, Jellyfin
"""

import argparse
import asyncio
import json
import os

# ── Nodes (must match generate-cluster-package.py) ──────────────────────────
NODES = [
    ("elitedesk_g6_i7", "EliteDesk G6 i7", "mdi:desktop-tower"),
    ("elitedesk_g6_i5", "EliteDesk G6 i5", "mdi:desktop-tower"),
    ("elitedesk_g5_i7", "EliteDesk G5 i7", "mdi:desktop-tower"),
    ("pi_5_16gb_1", "Pi 5 16GB #1", "mdi:raspberry-pi"),
    ("pi_5_8gb_1", "Pi 5 8GB #1", "mdi:raspberry-pi"),
    ("pi_5_8gb_2", "Pi 5 8GB #2", "mdi:raspberry-pi"),
    ("pi_5_8gb_3", "Pi 5 8GB #3", "mdi:raspberry-pi"),
]
PALETTE = ["#4f8cff", "#22c55e", "#f59e0b", "#ef4444", "#a855f7", "#14b8a6", "#ec4899"]


# ── Small card helpers ──────────────────────────────────────────────────────
def heading(text, icon=None, badges=None, nav=None):
    c = {"type": "heading", "heading": text, "heading_style": "title"}
    if icon:
        c["icon"] = icon
    if badges:
        c["badges"] = badges
    if nav:
        c["tap_action"] = {"action": "navigate", "navigation_path": nav}
    return c


def hbadge(entity, **kw):
    return {"type": "entity", "entity": entity, "show_state": True, "show_icon": True, **kw}


def tile(entity, name=None, cols=6, features=None, **kw):
    c = {"type": "tile", "entity": entity, "grid_options": {"columns": cols}, **kw}
    if name:
        c["name"] = name
    if features:
        c["features"] = features
        c["features_position"] = "bottom"
    return c


def gauge(entity, name, cols=4, green=0, yellow=70, red=85, **kw):
    return {
        "type": "gauge", "entity": entity, "name": name, "needle": True,
        "min": 0, "max": 100,
        "severity": {"green": green, "yellow": yellow, "red": red},
        "grid_options": {"columns": cols, "rows": 2}, **kw,
    }


def graph(entities, name=None, hours=24, cols="full", **kw):
    c = {
        "type": "custom:mini-graph-card",
        "entities": entities,
        "hours_to_show": hours,
        "points_per_hour": 4,
        "line_width": 2,
        "smoothing": True,
        "animate": True,
        "show": {"labels": False, "points": False, "legend": len(entities) > 1,
                 "fill": "fade" if len(entities) <= 2 else False},
        "grid_options": {"columns": cols},
        **kw,
    }
    if name:
        c["name"] = name
    return c


def chip_tmpl(icon, content, color, nav=None, entity=None):
    c = {"type": "template", "icon": icon, "content": content, "icon_color": color}
    if entity:
        c["entity"] = entity
    if nav:
        c["tap_action"] = {"action": "navigate", "navigation_path": nav}
    return c


def section(*cards, span=1):
    s = {"type": "grid", "cards": list(cards)}
    if span > 1:
        s["column_span"] = span
    return s


def sections_view(title, path, icon, sections, badges=None, header=None, max_cols=4):
    v = {"type": "sections", "title": title, "path": path, "icon": icon,
         "max_columns": max_cols, "sections": sections, "dense_section_placement": True}
    if badges:
        v["badges"] = badges
    if header:
        v["header"] = {"card": header, "layout": "responsive", "badges_position": "bottom"}
    return v


# Status colour of the homelab roll-up, reused on several dashboards.
HOMELAB_OK = "is_state('binary_sensor.homelab_problem', 'off')"

HOMELAB_HERO = {
    "type": "custom:mushroom-template-card",
    "entity": "binary_sensor.homelab_problem",
    "primary": "{{ 'All systems normal' if " + HOMELAB_OK + " else 'Homelab needs attention' }}",
    "secondary": (
        "{{ states('sensor.homelab_nodes_ready') }}/{{ states('sensor.homelab_nodes_total') }} nodes"
        " · {{ states('sensor.homelab_pods_running') }} pods"
        " · CPU {{ states('sensor.homelab_cluster_cpu') | round(0) }}%"
        " · RAM {{ states('sensor.homelab_cluster_memory') | round(0) }}%"
        "{% if not " + HOMELAB_OK + " %}\n{{ state_attr('binary_sensor.homelab_problem', 'summary') }}{% endif %}"
    ),
    "icon": "{{ 'mdi:check-decagram' if " + HOMELAB_OK + " else 'mdi:alert-decagram' }}",
    "icon_color": "{{ 'green' if " + HOMELAB_OK + " else 'red' }}",
    "multiline_secondary": True,
    "fill_container": True,
    "grid_options": {"columns": "full"},
    "tap_action": {"action": "navigate", "navigation_path": "/dashboard-homelab"},
}


# ── Overview (home) ─────────────────────────────────────────────────────────
def home_dashboard():
    greeting = {
        "type": "custom:mushroom-title-card",
        "title": (
            "{% set h = now().hour %}Good "
            "{{ 'night' if h < 6 else 'morning' if h < 12 else 'afternoon' if h < 18 else 'evening' }}, "
            "{{ user.split(' ')[0] }}"
        ),
        "subtitle": "{{ now().strftime('%A %-d %B') }} · {{ states('sensor.homelab_cluster_cpu') | round(0) }}% cluster load",
        "alignment": "start",
        "card_mod": {"style": ".title { font-size: 2rem !important; font-weight: 600 !important; }"},
        "grid_options": {"columns": "full"},
    }
    chips = {
        "type": "custom:mushroom-chips-card",
        "alignment": "start",
        "chips": [
            {"type": "weather", "entity": "weather.forecast_home",
             "show_conditions": True, "show_temperature": True},
            {"type": "entity", "entity": "person.silke_pilon", "use_entity_picture": True},
            chip_tmpl("{{ 'mdi:server-network' if " + HOMELAB_OK + " else 'mdi:server-network-off' }}",
                      "{{ 'Homelab OK' if " + HOMELAB_OK + " else 'Homelab issue' }}",
                      "{{ 'green' if " + HOMELAB_OK + " else 'red' }}", nav="/dashboard-homelab"),
            chip_tmpl("mdi:battery-charging-high",
                      "UPS {{ states('sensor.apc_battery_charge') }}%",
                      "{{ 'green' if 'OL' in states('sensor.apc_status_data') else 'orange' }}",
                      entity="sensor.apc_status"),
            chip_tmpl("mdi:download", "{{ states('sensor.qbittorrent_download_speed') }} MB/s",
                      "{{ 'blue' if states('sensor.qbittorrent_download_speed') | float(0) > 0 else 'disabled' }}",
                      nav="/dashboard-media"),
        ],
        "grid_options": {"columns": "full"},
    }

    welcome = section(greeting, chips,
                      {"type": "weather-forecast", "entity": "weather.forecast_home",
                       "forecast_type": "daily", "show_current": True, "show_forecast": True,
                       "grid_options": {"columns": "full"}},
                      span=2)

    bedroom = section(
        heading("Bedroom Silke", "mdi:bed", nav=None),
        *[{
            "type": "custom:mushroom-light-card", "entity": e, "name": n,
            "show_brightness_control": True, "show_color_temp_control": True,
            "use_light_color": True, "collapsible_controls": True,
            "grid_options": {"columns": "full"},
        } for e, n in [("light.wiz_tunable_white_2fd14e", "Ceiling"),
                       ("light.wiz_tunable_white_335d6c", "Bedside")]],
        tile("automation.bedroom_wake_light_at_dawn", "Wake light at dawn", cols="full",
             icon="mdi:weather-sunset-up"),
    )

    media = section(
        heading("Media", "mdi:speaker-multiple"),
        {"type": "custom:mushroom-media-player-card", "entity": "media_player.streamer_woonkamer",
         "name": "Living room", "use_media_info": True, "show_volume_level": True,
         "media_controls": ["play_pause_toggle", "previous", "next"],
         "volume_controls": ["volume_mute", "volume_set"], "collapsible_controls": True,
         "grid_options": {"columns": "full"}},
        {"type": "custom:mushroom-media-player-card", "entity": "media_player.grote_slaapkamer",
         "name": "Master bedroom", "use_media_info": True,
         "media_controls": ["play_pause_toggle"], "volume_controls": ["volume_set"],
         "collapsible_controls": True, "grid_options": {"columns": "full"}},
        # Anything currently playing on Jellyfin, any client.
        {"type": "custom:auto-entities", "show_empty": False,
         "card": {"type": "entities", "title": "Playing on Jellyfin", "state_color": True},
         "filter": {"include": [{"integration": "jellyfin", "domain": "media_player",
                                 "state": "/playing|paused/"}]},
         "grid_options": {"columns": "full"}},
    )

    power = section(
        heading("Power", "mdi:flash"),
        gauge("sensor.apc_battery_charge", "UPS battery", cols=6, green=50, yellow=25, red=0),
        gauge("sensor.apc_load", "UPS load", cols=6, yellow=60, red=80),
        tile("sensor.electricity_maps_co2_intensity", "Grid CO₂", cols=6),
        tile("sensor.electricity_maps_grid_fossil_fuel_percentage", "Fossil share", cols=6),
        graph([{"entity": "sensor.electricity_maps_co2_intensity", "name": "CO₂ intensity",
                "color": "#22c55e"}], hours=48, cols="full",
              color_thresholds=[{"value": 0, "color": "#22c55e"}, {"value": 300, "color": "#f59e0b"},
                                {"value": 450, "color": "#ef4444"}],
              show={"labels": False, "points": False, "legend": False, "fill": "fade", "name": False,
                    "icon": False}),
    )

    people = section(
        heading("People", "mdi:account-group"),
        tile("person.silke_pilon", cols="full"),
        tile("sensor.sm_g980f_battery_level", "Phone", cols=6),
        tile("sensor.pixel_watch_4_battery_level", "Watch", cols=6),
    )

    homelab = section(
        heading("Homelab", "mdi:server", nav="/dashboard-homelab"),
        HOMELAB_HERO,
        tile("sensor.homelab_cluster_cpu", "Cluster CPU", cols=6, features=[{"type": "bar-gauge"}]),
        tile("sensor.homelab_cluster_memory", "Cluster RAM", cols=6, features=[{"type": "bar-gauge"}]),
        tile("switch.pi_hole", "Pi-hole blocking", cols=6),
        tile("sensor.pi_hole_ads_blocked", "Ads blocked", cols=6),
    )

    lists = section(
        heading("Shopping list", "mdi:cart"),
        {"type": "todo-list", "entity": "todo.shopping_list", "hide_create": False,
         "grid_options": {"columns": "full"}},
    )

    return {"title": "Home", "views": [
        sections_view("Home", "home", "mdi:home",
                      [welcome, homelab, bedroom, media, power, people, lists], max_cols=4),
    ]}


# ── Homelab ─────────────────────────────────────────────────────────────────
def node_card(nid, name, icon):
    s = lambda m: f"states('sensor.{nid}_{m}')"
    ready = f"is_state('sensor.{nid}_ready', '1')"
    return {
        "type": "custom:mushroom-template-card",
        "entity": f"sensor.{nid}_cpu",
        "primary": name,
        "secondary": (
            "{% if not " + ready + " %}NOT READY · {% endif %}"
            "CPU {{ " + s("cpu") + " | float(0) | round(0) }}% · RAM {{ " + s("memory") + " | float(0) | round(0) }}%\n"
            "{{ " + s("temperature") + " | float(0) | round(0) }}°C · disk {{ " + s("root_disk") + " | float(0) | round(0) }}%"
        ),
        "multiline_secondary": True,
        "icon": icon,
        "icon_color": (
            "{% set t = " + s("temperature") + " | float(0) %}"
            "{{ 'red' if not " + ready + " or t >= 75 else 'orange' if t >= 65 "
            "or " + s("memory") + " | float(0) >= 90 else 'green' }}"
        ),
        "badge_icon": "{{ 'mdi:close' if not " + ready + " else '' }}",
        "badge_color": "red",
        "fill_container": True,
        "grid_options": {"columns": 8},
        "tap_action": {"action": "navigate", "navigation_path": "/dashboard-homelab/nodes"},
        "hold_action": {"action": "more-info"},
    }


def homelab_dashboard():
    status = section(
        heading("Status", "mdi:heart-pulse"),
        HOMELAB_HERO,
        {"type": "custom:mushroom-chips-card", "chips": [
            chip_tmpl("mdi:server", "{{ states('sensor.homelab_nodes_ready') }}/{{ states('sensor.homelab_nodes_total') }} nodes",
                      "{{ 'green' if states('sensor.homelab_nodes_ready') == states('sensor.homelab_nodes_total') else 'red' }}"),
            chip_tmpl("mdi:cube-outline", "{{ states('sensor.homelab_pods_running') }} pods",
                      "{{ 'green' if states('sensor.homelab_pods_unhealthy') | int(0) == 0 else 'orange' }}"),
            chip_tmpl("mdi:source-branch",
                      "{{ states('sensor.homelab_argo_cd_apps') | int(0) - states('sensor.homelab_argo_cd_apps_out_of_sync') | int(0) }}/{{ states('sensor.homelab_argo_cd_apps') }} synced",
                      "{{ 'green' if states('sensor.homelab_argo_cd_apps_out_of_sync') | int(0) == 0 else 'orange' }}"),
            chip_tmpl("mdi:database", "{{ states('sensor.homelab_longhorn_volumes') }} volumes",
                      "{{ 'red' if states('sensor.homelab_longhorn_volumes_faulted') | int(0) > 0 else 'orange' if states('sensor.homelab_longhorn_volumes_degraded') | int(0) > 0 else 'green' }}"),
            chip_tmpl("mdi:restart", "{{ states('sensor.homelab_container_restarts_1h') }} restarts/h",
                      "{{ 'orange' if states('sensor.homelab_container_restarts_1h') | int(0) > 5 else 'disabled' }}"),
            chip_tmpl("mdi:thermometer", "{{ states('sensor.homelab_hottest_node') | float(0) | round(0) }}°C max",
                      "{{ 'red' if states('sensor.homelab_hottest_node') | float(0) >= 75 else 'orange' if states('sensor.homelab_hottest_node') | float(0) >= 65 else 'green' }}"),
        ], "grid_options": {"columns": "full"}},
        span=2,
    )

    cluster = section(
        heading("Cluster", "mdi:kubernetes"),
        gauge("sensor.homelab_cluster_cpu", "CPU", yellow=60, red=85),
        gauge("sensor.homelab_cluster_memory", "Memory", yellow=75, red=90),
        gauge("sensor.homelab_longhorn_storage_used", "Longhorn", yellow=70, red=85),
        graph([{"entity": "sensor.homelab_cluster_cpu", "name": "CPU", "color": PALETTE[0]},
               {"entity": "sensor.homelab_cluster_memory", "name": "Memory", "color": PALETTE[4]}],
              hours=24, cols=12, height=110),
        span=2,
    )

    nodes = section(
        heading("Nodes", "mdi:server", nav="/dashboard-homelab/nodes"),
        *[node_card(*n) for n in NODES],
        span=2,
    )

    temps = section(
        heading("Temperatures", "mdi:thermometer"),
        graph([{"entity": f"sensor.{nid}_temperature", "name": name, "color": PALETTE[i]}
               for i, (nid, name, _) in enumerate(NODES)], hours=24,
              show={"labels": True, "points": False, "legend": True, "fill": False}),
        span=2,
    )

    gitops = section(
        heading("Workloads", "mdi:source-branch"),
        tile("sensor.homelab_argo_cd_apps", "Argo CD apps", cols=6, icon="mdi:source-branch"),
        tile("sensor.homelab_argo_cd_apps_out_of_sync", "Out of sync", cols=6),
        tile("sensor.homelab_argo_cd_apps_unhealthy", "Unhealthy", cols=6),
        tile("sensor.homelab_pods_unhealthy", "Pods failing", cols=6),
        tile("sensor.homelab_containers_crash_looping", "Crash-looping", cols=6),
        tile("sensor.homelab_container_restarts_1h", "Restarts (1h)", cols=6),
    )

    storage = section(
        heading("Storage", "mdi:harddisk"),
        tile("sensor.homelab_longhorn_volumes", "Longhorn volumes", cols="full"),
        tile("sensor.homelab_longhorn_volumes_degraded", "Degraded", cols=6),
        tile("sensor.homelab_longhorn_volumes_faulted", "Faulted", cols=6),
        tile("sensor.radarr_disk_space_movies", "Media disk", cols=6, icon="mdi:nas"),
        tile("sensor.sonarr_disk_space", "TV disk", cols=6, icon="mdi:nas"),
    )

    network = section(
        heading("Network", "mdi:lan"),
        tile("sensor.cloud_gateway_ultra_cloudflare_wan_latency", "Cloudflare", cols=6, icon="mdi:web-clock"),
        tile("sensor.cloud_gateway_ultra_google_wan_latency", "Google", cols=6, icon="mdi:web-clock"),
        tile("sensor.cloud_gateway_ultra_cpu_utilization", "Gateway CPU", cols=6),
        tile("sensor.cloud_gateway_ultra_cpu_temperature", "Gateway temp", cols=6),
        tile("sensor.usw_lite_16_poe_clients", "Switch clients", cols=6, icon="mdi:switch"),
        tile("update.cloud_gateway_ultra_firmware", "Firmware", cols=6),
        graph([{"entity": "sensor.cloud_gateway_ultra_cloudflare_wan_latency", "name": "Cloudflare", "color": PALETTE[2]},
               {"entity": "sensor.cloud_gateway_ultra_google_wan_latency", "name": "Google", "color": PALETTE[0]}],
              name="WAN latency", hours=24),
    )

    pihole = section(
        heading("Pi-hole", "mdi:pi-hole", badges=[hbadge("binary_sensor.pi_hole_status")]),
        tile("switch.pi_hole", "Blocking", cols="full"),
        tile("sensor.pi_hole_dns_queries", "Queries today", cols=6),
        tile("sensor.pi_hole_ads_blocked", "Blocked today", cols=6),
        tile("sensor.pi_hole_ads_percentage_blocked", "Blocked %", cols=6),
        tile("sensor.pi_hole_dns_unique_clients", "Clients", cols=6),
    )

    power = section(
        heading("Power", "mdi:flash", badges=[hbadge("sensor.apc_status")]),
        gauge("sensor.apc_battery_charge", "Battery", cols=6, green=50, yellow=25, red=0),
        gauge("sensor.apc_load", "Load", cols=6, yellow=60, red=80),
        tile("sensor.apc_battery_runtime", "Runtime", cols=6),
        tile("sensor.apc_input_voltage", "Input", cols=6),
        graph([{"entity": "sensor.apc_load", "name": "Load", "color": PALETTE[2]}], hours=24,
              show={"labels": False, "points": False, "legend": False, "fill": "fade"}),
    )

    overview = sections_view("Overview", "overview", "mdi:view-dashboard",
                             [status, cluster, nodes, temps, gitops, storage, network, pihole, power],
                             max_cols=4)

    node_sections = []
    for i, (nid, name, icon) in enumerate(NODES):
        node_sections.append(section(
            heading(name, icon, badges=[hbadge(f"sensor.{nid}_temperature"),
                                        hbadge(f"sensor.{nid}_root_disk", icon="mdi:harddisk")]),
            graph([{"entity": f"sensor.{nid}_cpu", "name": "CPU", "color": PALETTE[0]},
                   {"entity": f"sensor.{nid}_memory", "name": "Memory", "color": PALETTE[4]}],
                  hours=12),
            graph([{"entity": f"sensor.{nid}_network_in", "name": "Network in", "color": PALETTE[5]}],
                  hours=12, show={"labels": False, "points": False, "legend": False, "fill": "fade",
                                  "name": True, "icon": False}),
        ))
    nodes_view = sections_view("Nodes", "nodes", "mdi:server", node_sections, max_cols=4)

    return {"title": "Homelab", "views": [overview, nodes_view]}


# ── Media ───────────────────────────────────────────────────────────────────
def media_dashboard():
    downloads = section(
        heading("Downloads", "mdi:download", badges=[hbadge("sensor.qbittorrent_connection_status")]),
        graph([{"entity": "sensor.qbittorrent_download_speed", "name": "Down", "color": PALETTE[0]},
               {"entity": "sensor.qbittorrent_upload_speed", "name": "Up", "color": PALETTE[1]}],
              hours=6, lower_bound=0, height=100),
        tile("sensor.qbittorrent_status", "Status", cols=6, icon="mdi:download-circle"),
        tile("sensor.qbittorrent_active_torrents", "Active", cols=6, icon="mdi:download"),
        tile("sensor.qbittorrent_all_torrents", "Torrents", cols=6, icon="mdi:magnet"),
        tile("sensor.qbittorrent_errored_torrents", "Errored", cols=6, icon="mdi:alert-circle-outline"),
        tile("switch.qbittorrent_alternative_speed", "Slow mode", cols=6, icon="mdi:speedometer-slow"),
        tile("sensor.qbittorrent_global_ratio", "Ratio", cols=6, icon="mdi:scale-balance"),
        span=2,
    )

    library = section(
        heading("Library", "mdi:filmstrip-box-multiple"),
        tile("sensor.sonarr_shows", "Series", cols=4, icon="mdi:television-classic"),
        tile("sensor.radarr_movies", "Movies", cols=4, icon="mdi:movie-open"),
        tile("sensor.lidarr_albums", "Albums", cols=4, icon="mdi:album"),
        tile("sensor.sonarr_wanted", "Missing eps", cols=6),
        tile("sensor.lidarr_wanted", "Missing albums", cols=6),
        tile("sensor.sonarr_queue", "Sonarr queue", cols=4),
        tile("sensor.radarr_queue", "Radarr queue", cols=4),
        tile("sensor.lidarr_queue", "Lidarr queue", cols=4),
        tile("sensor.radarr_disk_space_movies", "Media disk", cols="full", icon="mdi:nas"),
        span=2,
    )

    upcoming = section(
        heading("Coming up", "mdi:calendar-star"),
        {"type": "calendar", "entities": ["calendar.sonarr", "calendar.radarr"],
         "initial_view": "listWeek", "grid_options": {"columns": "full"}},
        span=2,
    )

    jellyfin = section(
        heading("Jellyfin", "mdi:play-network", badges=[hbadge("sensor.music_active_clients")]),
        {"type": "custom:auto-entities", "show_empty": True,
         "card": {"type": "entities", "state_color": True},
         "filter": {"include": [{"integration": "jellyfin", "domain": "media_player",
                                 "state": "/playing|paused/"}]},
         "else": {"type": "markdown", "content": "Nothing playing right now."},
         "grid_options": {"columns": "full"}},
        span=2,
    )

    return {"title": "Library", "views": [
        sections_view("Library", "media", "mdi:play-box-multiple",
                      [downloads, library, upcoming, jellyfin], max_cols=4),
    ]}


# ── Push ────────────────────────────────────────────────────────────────────
DASHBOARDS = [
    # (url_path, title, icon, builder)
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
            if url_path and url_path not in existing:
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
