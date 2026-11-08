"""Internal worker capture and game health; the controller serves the screen wall."""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path("/work/minecraft-prototype")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/health":
                with (ROOT / "client.log").open("rb") as handle:
                    handle.seek(max(0, (ROOT / "client.log").stat().st_size - 200_000))
                    log = handle.read().decode(errors="replace")
                pid = int((ROOT / "client.pid").read_text())
                frame = ROOT / "frame.jpg"
                data = json.dumps({
                    "gameLoaded": "joined the game" in log,
                    "javaAlive": Path(f"/proc/{pid}").exists(),
                    "frameAgeSeconds": round(time.time() - frame.stat().st_mtime, 2) if frame.exists() else None,
                    "error": "Java heap exhausted" if "OutOfMemoryError" in log else None,
                }).encode()
                kind = "application/json"
            elif path == "/frame.jpg":
                data, kind = (ROOT / "frame.jpg").read_bytes(), "image/jpeg"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (OSError, ValueError, KeyError):
            self.send_error(503, "Frame unavailable")

    def log_message(self, *_):
        pass


ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
