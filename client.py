"""Install and launch an actual Minecraft demo client, without account credentials."""

import json
import os
import subprocess
import sys
from pathlib import Path

import minecraft_launcher_lib as launcher

ROOT = Path("/work/minecraft-prototype")
GAME = ROOT / "game"
VERSION = "1.21.1"
HEAP_MB = int(os.environ.get("MC_HEAP_MB", "1024"))
FPS = int(os.environ.get("MC_FPS", "10"))
WIDTH = int(os.environ.get("MC_WIDTH", "640"))
HEIGHT = int(os.environ.get("MC_HEIGHT", "360"))
THREADS = int(os.environ.get("MC_THREADS", "2"))

if sys.argv[1] == "install":
    launcher.install.install_minecraft_version(VERSION, str(GAME))
    print(json.dumps({"installed": VERSION}), flush=True)
elif sys.argv[1] == "run":
    GAME.mkdir(parents=True, exist_ok=True)
    (GAME / "options.txt").write_text(
        f"version:3955\nrenderDistance:2\nsimulationDistance:5\nmaxFps:{FPS}\ngraphicsMode:0\nrenderClouds:false\nparticles:2\nmipmapLevels:0\nenableVsync:false\npauseOnLostFocus:false\nonboardAccessibility:false\ntutorialStep:none\nguiScale:1\nsoundCategory_master:0.0\noverrideWidth:{WIDTH}\noverrideHeight:{HEIGHT}\n"
    )
    # The library's development identity is used only with Mojang's demo mode.
    options = launcher.utils.generate_test_options()
    options.update(
        {
            "demo": True,
            "executablePath": "/usr/bin/java",
            "jvmArguments": ["-Xms128M", f"-Xmx{HEAP_MB}M", f"-XX:ActiveProcessorCount={THREADS}"]
            + (["-XX:+UseSerialGC"] if HEAP_MB < 512 else []),
            "customResolution": True,
            "resolutionWidth": str(WIDTH),
            "resolutionHeight": str(HEIGHT),
        }
    )
    if (GAME / "saves" / "Demo_World" / "level.dat").exists():
        options["quickPlaySingleplayer"] = "Demo_World"
    argv = launcher.command.get_minecraft_command(VERSION, str(GAME), options)
    env = dict(os.environ, DISPLAY=":1", LIBGL_ALWAYS_SOFTWARE="1", LP_NUM_THREADS=str(THREADS))
    with (ROOT / "client.log").open("w") as log:
        proc = subprocess.Popen(
            argv, cwd=GAME, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        (ROOT / "client.pid").write_text(str(proc.pid))
        raise SystemExit(proc.wait())
else:
    raise SystemExit("usage: client.py install|run")
