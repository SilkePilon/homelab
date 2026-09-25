#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Runs `t3 serve` from the newest build on $T3_CHANNEL and keeps it current.
#
# The t3 baked into the image is only the bootstrapper: `t3 update` downloads
# releases to $T3_HOME/runtime/versions/<version>/t3, which is on the PVC.
# Every $T3_UPDATE_INTERVAL seconds this asks npm for the channel's newest
# version; if it differs from the running one, it installs it and restarts the
# server in place. The pod never rolls, but agent turns running at that moment
# are killed, same as a pod restart.
#
# If the registry is unreachable at boot, the newest version already on the
# PVC runs, and failing that the baked-in one.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

T3_HOME=${T3_HOME:-$HOME/.t3}
T3_CHANNEL=${T3_CHANNEL:-nightly}
T3_UPDATE_INTERVAL=${T3_UPDATE_INTERVAL:-7200}
T3_KEEP_VERSIONS=${T3_KEEP_VERSIONS:-3}
WORKDIR=$HOME/code
VERSIONS=$T3_HOME/runtime/versions

log() { echo "[t3-supervisor] $*" >&2; }

# npm dist-tags are named after the channel, except stable, which is `latest`.
dist_tag() { [ "$T3_CHANNEL" = stable ] && echo latest || echo "$T3_CHANNEL"; }

latest_version() {
  npm view "t3@$(dist_tag)" version 2>/dev/null
}

# Echoes the binary for $1, downloading it first if it is not on the PVC.
install_version() {
  local v=$1
  # `t3 update` refuses the version it is itself; the image already has it.
  if [ "$v" = "$(t3 --version 2>/dev/null | awk '{print $2}' | sed 's/^v//')" ]; then
    command -v t3
    return
  fi
  if [ ! -x "$VERSIONS/$v/t3" ]; then
    log "installing t3 $v ($T3_CHANNEL)"
    t3 update "$v" --channel "$T3_CHANNEL" --base-dir "$T3_HOME" \
      --allow-downgrade --yes >&2 || return 1
  fi
  [ -x "$VERSIONS/$v/t3" ] && echo "$VERSIONS/$v/t3"
}

newest_installed() {
  ls -1 "$VERSIONS" 2>/dev/null | sort -V | tail -n 1
}

# Keep the running version plus the newest few; each is ~100MB.
prune() {
  ls -1 "$VERSIONS" 2>/dev/null | sort -V | head -n "-$T3_KEEP_VERSIONS" |
    while read -r old; do
      [ "$old" = "$running" ] || rm -rf "${VERSIONS:?}/$old"
    done
}

server_pid=
running=

start_server() {
  local bin=$1
  log "starting $("$bin" --version 2>/dev/null)"
  # Own process group: the npm-installed t3 is a launcher that spawns the real
  # server and does not forward SIGTERM, so stop_server signals the group.
  setsid "$bin" serve --host 0.0.0.0 --port 3773 --base-dir "$T3_HOME" "$WORKDIR" &
  server_pid=$!
}

# SIGTERM the whole group, give it 20s to flush SQLite, then SIGKILL, so the
# next server never races the old one for the port.
stop_server() {
  [ -n "$server_pid" ] || return 0
  kill -TERM -- "-$server_pid" 2>/dev/null
  local i
  for i in $(seq 1 40); do
    kill -0 -- "-$server_pid" 2>/dev/null || break
    sleep 0.5
  done
  kill -KILL -- "-$server_pid" 2>/dev/null
  wait "$server_pid" 2>/dev/null
  server_pid=
}

trap 'log "shutting down"; stop_server; exit 0' TERM INT

mkdir -p "$WORKDIR" "$VERSIONS"

target=$(latest_version)
bin=
if [ -n "$target" ]; then
  bin=$(install_version "$target") && running=$target
fi
if [ -z "$bin" ]; then
  running=$(newest_installed)
  if [ -n "$running" ]; then
    log "registry or install failed; falling back to installed $running"
    bin=$VERSIONS/$running/t3
  else
    log "registry or install failed; falling back to the image's t3"
    running=image
    bin=$(command -v t3)
  fi
fi
start_server "$bin"
prune

next_check=$((SECONDS + T3_UPDATE_INTERVAL))
while :; do
  # Short sleeps in the background so SIGTERM is handled within a second.
  sleep 5 &
  wait $! 2>/dev/null

  if ! kill -0 "$server_pid" 2>/dev/null; then
    wait "$server_pid"
    code=$?
    log "t3 serve exited with $code"
    exit "$code"
  fi

  [ "$SECONDS" -ge "$next_check" ] || continue
  next_check=$((SECONDS + T3_UPDATE_INTERVAL))

  target=$(latest_version)
  if [ -z "$target" ]; then
    log "update check failed; staying on $running"
    continue
  fi
  [ "$target" = "$running" ] && continue

  if new_bin=$(install_version "$target"); then
    log "updating $running -> $target"
    stop_server
    running=$target
    start_server "$new_bin"
    prune
  else
    log "install of $target failed; staying on $running"
  fi
done
