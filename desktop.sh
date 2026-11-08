#!/usr/bin/env bash
# Minimal display: Minecraft does not need the desktop recipe's Chromium or MCP servers.
set -euo pipefail
export DISPLAY=:1
Xvfb :1 -screen 0 640x360x24 -nolisten tcp -ac -dpi 96 &
xvfb_pid=$!
trap 'kill "$xvfb_pid" 2>/dev/null || true' EXIT
for attempt in $(seq 1 50); do
  xdpyinfo >/dev/null 2>&1 && break
  sleep 0.1
done
openbox >/tmp/openbox.log 2>&1 &
x11vnc -display :1 -localhost -rfbport 5900 -shared -forever -nopw -quiet -noxdamage >/tmp/vnc.log 2>&1 &
websockify --web /opt/novnc 8080 localhost:5900 >/tmp/websockify.log 2>&1 &
# Setup and launch remain explicit bounded execs, so a failed install leaves a machine to inspect.
wait "$xvfb_pid"
