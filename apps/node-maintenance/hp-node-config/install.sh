#!/bin/sh
# Runs in the pod. /host is the node's root filesystem; host commands go
# through nsenter into PID 1's namespaces, like image-gc does.
set -eu
H=/host
host() { nsenter -t 1 -m -u -i -n -p -- "$@"; }
changed=""

# put SRC DEST MODE: copy only if different; record what changed
put() {
  mkdir -p "$(dirname "$H$2")"
  if ! cmp -s "/config/$1" "$H$2"; then
    cp "/config/$1" "$H$2.tmp" && chmod "$3" "$H$2.tmp" && mv "$H$2.tmp" "$H$2"
    echo "updated $2"
    changed="$changed $1"
  fi
}

put 90-watchdog.conf       /etc/systemd/system.conf.d/90-watchdog.conf  644
put 90-size.conf           /etc/systemd/journald.conf.d/90-size.conf    644
put nic-watchdog           /usr/local/sbin/nic-watchdog                 755
put nic-watchdog.service   /etc/systemd/system/nic-watchdog.service     644
put nic-watchdog.timer     /etc/systemd/system/nic-watchdog.timer       644

case "$changed" in *90-watchdog.conf*)
  # system.conf is only read at (re-)exec. daemon-reexec keeps every
  # running service; it just restarts PID 1 in place.
  host systemctl daemon-reexec ;;
esac
case "$changed" in *nic-watchdog*)
  host systemctl daemon-reload ;;
esac
host systemctl enable --now nic-watchdog.timer >/dev/null 2>&1
case "$changed" in *90-size.conf*)
  host systemctl restart systemd-journald ;;
esac

# Offload off on every e1000e NIC: in the NM profile for future
# activations, and live via ethtool so it applies now without bouncing
# the link.
for dev in $(host ls /sys/class/net); do
  host ethtool -i "$dev" 2>/dev/null | grep -qx 'driver: e1000e' || continue
  conn=$(host nmcli -g GENERAL.CONNECTION device show "$dev" 2>/dev/null || true)
  if [ -n "$conn" ]; then
    cur=$(host nmcli -g ethtool.feature-tso,ethtool.feature-gso connection show "$conn")
    if [ "$cur" != "$(printf 'off\noff')" ]; then
      host nmcli connection modify "$conn" ethtool.feature-tso off ethtool.feature-gso off
      echo "set tso/gso off in NM profile '$conn'"
    fi
  fi
  host ethtool -K "$dev" tso off gso off
done

echo "host config ok on $NODE_NAME:$(echo "${changed:- nothing changed}")"
