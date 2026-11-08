"""Bounded movement for a screen-wall demo, not an AI player."""

import os
import random
import subprocess
import time
from pathlib import Path

env = dict(os.environ, DISPLAY=":1")


def xdo(*args):
    subprocess.run(["xdotool", *args], env=env, check=True, timeout=5)


ready_deadline = time.monotonic() + 120
log = Path("/work/minecraft-prototype/client.log")
while not log.exists() or "joined the game" not in log.read_text(errors="replace"):
    if time.monotonic() > ready_deadline:
        raise SystemExit("No loaded world within 120 seconds")
    time.sleep(1)
xdo("search", "--name", "^Minecraft 1.21.1", "windowactivate", "--sync")
# This pinned version shows its demo welcome after every launch, including saved worlds.
time.sleep(3)
xdo("mousemove", "380", "235", "click", "1")
time.sleep(1)
deadline = time.monotonic() + 900
try:
    while time.monotonic() < deadline:
        direction = random.choice(["w", "a", "d"])
        xdo("keydown", direction, "keydown", "space")
        time.sleep(random.uniform(1, 3))
        xdo("keyup", direction, "keyup", "space")
        xdo("mousemove_relative", "--", str(random.randint(-150, 150)), "0")
        time.sleep(1)
finally:
    xdo("keyup", "w", "keyup", "a", "keyup", "d", "keyup", "space")
