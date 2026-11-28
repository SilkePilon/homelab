#!/bin/sh
# Builds the `server:` section of the runner config and starts the daemon.
# The UUID is the hex encoding of the secret's first 16 characters, the same
# one `forgejo forgejo-cli actions register` derives on the Forgejo side.
set -eu
id=$(cut -c1-16 /secrets/RUNNER_SECRET)
hex=$(printf %s "$id" | od -An -tx1 | tr -d ' \n')
uuid=$(echo "$hex" | sed -E 's/^(.{8})(.{4})(.{4})(.{4})(.{12})$/\1-\2-\3-\4-\5/')
cp /etc/runner/config.yml /data/config.yml
cat >> /data/config.yml <<CONF
server:
  connections:
    forgejo:
      url: ${FORGEJO_URL}
      uuid: ${uuid}
      token_url: file:/secrets/RUNNER_SECRET
CONF
until [ -S /run/dind/docker.sock ]; do echo "waiting for docker..."; sleep 2; done
exec forgejo-runner daemon --config /data/config.yml
