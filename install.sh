#!/usr/bin/env bash
set -euo pipefail
cd /work/minecraft-prototype
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends openjdk-21-jre-headless python3-venv mesa-utils libgl1-mesa-dri xdotool ffmpeg
python3 -m venv .venv
.venv/bin/pip install --disable-pip-version-check minecraft-launcher-lib==8.0
.venv/bin/python client.py install
