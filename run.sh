#!/usr/bin/env bash
set -euo pipefail
cd /work/minecraft-prototype
export DISPLAY=:1 LIBGL_ALWAYS_SOFTWARE=1 LP_NUM_THREADS=2
test -f peers.json || printf '{"mc-client-01":"localhost"}\n' > peers.json
python3 wall.py > wall.log 2>&1 &
wall_pid=$!
ffmpeg -nostdin -loglevel warning -f x11grab -video_size 640x360 -framerate 2 -i :1 \
  -an -threads 1 -q:v 6 -update 1 -atomic_writing 1 -y frame.jpg > capture.log 2>&1 &
capture_pid=$!
: > client.log
python3 autoplay.py > autoplay.log 2>&1 &
autoplay_pid=$!
trap 'kill "$wall_pid" "$capture_pid" "$autoplay_pid" 2>/dev/null || true' EXIT
.venv/bin/python client.py run
