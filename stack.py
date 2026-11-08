"""Print a reproducible stack declaration; creates no resources by itself."""

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--count", type=int, default=10, choices=range(1, 101))
parser.add_argument("--size", choices=["small", "medium"], default="small")
args = parser.parse_args()
root = Path(__file__).parent
files = {
    f"/work/minecraft-prototype/{name}": (root / name).read_text()
    for name in [
        "desktop.sh",
        "install.sh",
        "run.sh",
        "client.py",
        "wall.py",
        "autoplay.py",
    ]
}
print(
    json.dumps(
        {
            "stack": "minecraft-wall",
            "sleep": "30m",
            "services": {
                f"mc-client-{index:02d}": {
                    "image": "ghcr.io/briq-cloud/sandbox:desktop-2026-11-04",
                    "size": args.size,
                    "volume": {"gb": 8, "mount": "/work"},
                    "ports": [8080, 8090],
                    "cmd": ["bash", "/work/minecraft-prototype/desktop.sh"],
                    "files": files,
                    "healthcheck": {
                        "cmd": [
                            "sh",
                            "-c",
                            "curl -fs http://127.0.0.1:8080/vnc.html >/dev/null",
                        ]
                    },
                }
                for index in range(1, args.count + 1)
            },
        },
        indent=2,
    )
)
