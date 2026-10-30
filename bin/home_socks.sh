#!/usr/bin/env bash
# home-socks: persistent SSH dynamic SOCKS tunnel VPS -> Pi (residential exit).
# Uses a dedicated key (id_home_socks). No password stored anywhere.
exec /usr/bin/ssh \
  -i /root/.ssh/id_home_socks \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile=/root/.ssh/known_hosts \
  -o StrictHostKeyChecking=accept-new \
  -o ConnectTimeout=10 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -N -D 127.0.0.1:1080 \
  ${HOME_SOCKS_HOST:?set HOME_SOCKS_HOST}
