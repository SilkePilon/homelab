#!/usr/bin/env python3
"""Generate packages/homelab_cluster.yaml: HA sensors backed by Prometheus.

Each Prometheus query is ONE `rest` resource (one HTTP call per scan); per-node
queries fan out into one sensor per node via a selectattr() template, so adding
a node is a one-line change to NODES below and a re-run:

    python3 apps/homeassistant/generate-cluster-package.py

The output is committed, so Argo CD never runs this.
"""

from pathlib import Path

PROM = "http://prometheus.monitoring.svc.cluster.local:9090/api/v1/query"

# node name (Kubernetes / node-exporter `node` label) -> short id, display name
NODES = {
    "hp-elitedesk-800-g6-i7": ("g6_i7", "EliteDesk G6 i7"),
    "hp-elitedesk-800-g6-i5": ("g6_i5", "EliteDesk G6 i5"),
    "hp-elitedesk-800-g5-i7": ("g5_i7", "EliteDesk G5 i7"),
    "raspberrypi-5-16gb-1": ("pi16_1", "Pi 5 16GB #1"),
    "raspberrypi-5-8gb-1": ("pi8_1", "Pi 5 8GB #1"),
    "raspberrypi-5-8gb-2": ("pi8_2", "Pi 5 8GB #2"),
    "raspberrypi-5-8gb-3": ("pi8_3", "Pi 5 8GB #3"),
}

# Cluster-wide single-value queries:
# (id, name, query, unit, icon, device_class, state_class, precision)
CLUSTER = [
    ("nodes_ready", "Nodes ready",
     'sum(kube_node_status_condition{condition="Ready",status="true"})',
     None, "mdi:server-network", None, "measurement", 0),
    ("nodes_total", "Nodes total", "count(kube_node_info)",
     None, "mdi:server", None, "measurement", 0),
    ("cpu", "Cluster CPU",
     '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))',
     "%", "mdi:cpu-64-bit", None, "measurement", 1),
    ("memory", "Cluster memory",
     "100 * (1 - sum(node_memory_MemAvailable_bytes) / sum(node_memory_MemTotal_bytes))",
     "%", "mdi:memory", None, "measurement", 1),
    ("pods_running", "Pods running", 'sum(kube_pod_status_phase{phase="Running"})',
     None, "mdi:cube-outline", None, "measurement", 0),
    ("pods_unhealthy", "Pods unhealthy",
     'sum(kube_pod_status_phase{phase=~"Pending|Failed|Unknown"}) or vector(0)',
     None, "mdi:cube-off-outline", None, "measurement", 0),
    ("crashloops", "Containers crash-looping",
     'count(kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} == 1) or vector(0)',
     None, "mdi:restart-alert", None, "measurement", 0),
    ("restarts_1h", "Container restarts (1h)",
     "round(sum(increase(kube_pod_container_status_restarts_total[1h]))) or vector(0)",
     None, "mdi:restart", None, "measurement", 0),
    ("longhorn_used", "Longhorn storage used",
     "100 * sum(longhorn_node_storage_usage_bytes) / sum(longhorn_node_storage_capacity_bytes)",
     "%", "mdi:harddisk", None, "measurement", 1),
    # longhorn_volume_robustness: 0 unknown, 1 healthy, 2 degraded, 3 faulted.
    # Every manager reports every volume, hence max by (volume).
    ("longhorn_volumes", "Longhorn volumes",
     "count(max by (volume) (longhorn_volume_robustness))",
     None, "mdi:database", None, "measurement", 0),
    ("longhorn_degraded", "Longhorn volumes degraded",
     "count(max by (volume) (longhorn_volume_robustness) == 2) or vector(0)",
     None, "mdi:database-alert", None, "measurement", 0),
    ("longhorn_faulted", "Longhorn volumes faulted",
     "count(max by (volume) (longhorn_volume_robustness) == 3) or vector(0)",
     None, "mdi:database-remove", None, "measurement", 0),
    # Needs the argocd scrape job in apps/monitoring/prometheus.yaml.
    ("argocd_apps", "Argo CD apps", "count(argocd_app_info) or vector(0)",
     None, "mdi:source-branch", None, "measurement", 0),
    ("argocd_out_of_sync", "Argo CD apps out of sync",
     'count(argocd_app_info{sync_status!="Synced"}) or vector(0)',
     None, "mdi:source-branch-sync", None, "measurement", 0),
    ("argocd_unhealthy", "Argo CD apps unhealthy",
     'count(argocd_app_info{health_status!="Healthy"}) or vector(0)',
     None, "mdi:heart-broken", None, "measurement", 0),
]

# Per-node queries, each must carry a `node` label:
# (id, name, query, unit, icon, device_class, precision)
PER_NODE = [
    ("cpu", "CPU",
     '100 * (1 - avg by (node) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
     "%", "mdi:cpu-64-bit", None, 1),
    ("memory", "Memory",
     "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
     "%", "mdi:memory", None, 1),
    ("temperature", "Temperature",
     "max by (node) (node_hwmon_temp_celsius)",
     "°C", None, "temperature", 1),
    ("disk", "Root disk",
     '100 * (1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"})',
     "%", "mdi:harddisk", None, 1),
    ("network", "Network in",
     'sum by (node) (rate(node_network_receive_bytes_total{device!~"lo|veth.*|cni.*|flannel.*|tailscale.*"}[5m])) / 1e6',
     "MB/s", "mdi:download-network", "data_rate", 2),
    ("ready", "Ready",
     'max by (node) (kube_node_status_condition{condition="Ready",status="true"})',
     None, "mdi:check-network", None, 0),
]


def q(s: str) -> str:
    """Single-quote a YAML scalar."""
    return "'" + s.replace("'", "''") + "'"


def sensor(lines, uid, name, tmpl, unit, icon, dclass, sclass):
    lines.append(f"      - name: {q(name)}")
    lines.append(f"        unique_id: {uid}")
    lines.append(f"        value_template: {q(tmpl)}")
    if unit:
        lines.append(f"        unit_of_measurement: {q(unit)}")
    if icon:
        lines.append(f"        icon: {icon}")
    if dclass:
        lines.append(f"        device_class: {dclass}")
    if sclass:
        lines.append(f"        state_class: {sclass}")


def main():
    out = [
        "# GENERATED by ../generate-cluster-package.py - edit that, not this.",
        "# Homelab cluster health from Prometheus, one REST call per query.",
        "rest:",
    ]
    for uid, name, query, unit, icon, dclass, sclass, prec in CLUSTER:
        out += [
            f"  - resource: {PROM}",
            "    scan_interval: 60",
            "    timeout: 15",
            "    params:",
            f"      query: {q(query)}",
            "    sensor:",
        ]
        tmpl = (
            "{% set r = value_json.data.result %}"
            f"{{{{ (r[0].value[1] | float) | round({prec}) if r else none }}}}"
        )
        if prec == 0:
            tmpl = tmpl.replace("| round(0)", "| int")
        sensor(out, f"homelab_{uid}", f"Homelab {name}", tmpl, unit, icon, dclass, sclass)

    for mid, mname, query, unit, icon, dclass, prec in PER_NODE:
        out += [
            f"  - resource: {PROM}",
            "    scan_interval: 60",
            "    timeout: 15",
            "    params:",
            f"      query: {q(query)}",
            "    sensor:",
        ]
        for node, (nid, nname) in NODES.items():
            tmpl = (
                "{% set r = value_json.data.result | selectattr('metric.node', 'eq', "
                f"'{node}') | list %}}"
                f"{{{{ (r[0].value[1] | float) | round({prec}) if r else none }}}}"
            )
            if prec == 0:
                tmpl = tmpl.replace("| round(0)", "| int")
            sensor(out, f"homelab_node_{nid}_{mid}", f"{nname} {mname}", tmpl, unit,
                   icon, dclass, "measurement")

    path = Path(__file__).with_name("packages") / "homelab_cluster.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(out) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
