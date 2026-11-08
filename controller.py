"""Bounded, receipt-scoped Minecraft fleet controller. Python standard library only."""
import copy
import json
import math
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

TERMINAL_VM = {"stopped", "sleeping", "error"}
GAME_CMD = ["bash", "/work/minecraft-prototype/run.sh"]


class APIRejected(RuntimeError):
    """A definite HTTP refusal, safe to reconcile without an unknown-outcome intent."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


class API:
    def __init__(self):
        self.base = os.environ.get("BRIQ_API_URL", "https://api.briq.cloud").rstrip("/")
        self.key = os.environ["BRIQ_API_KEY"]
        if urlsplit(self.base).scheme not in {"http", "https"}:
            raise ValueError("Invalid API URL")

    def call(self, method, path, body=None, *, idempotency_key=None):
        headers = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers, method=method)
        try:
            with build_opener(NoRedirect).open(request, timeout=10 if path == "/v1/quota" else 40) as response:
                return json.loads(response.read(2_000_000))
        except HTTPError as exc:
            error = APIRejected if 400 <= exc.code < 500 and exc.code != 408 else RuntimeError
            raise error("API HTTP " + str(exc.code)) from None
        except (OSError, ValueError):
            raise RuntimeError("API outcome unknown; mutation was not retried") from None


class Fleet:
    def __init__(self, api, config, state_path, clock=time.time):
        self.api, self.config, self.path, self.clock = api, config, Path(state_path), clock
        self.lock = threading.RLock()
        self.launching = False
        self.start_lock = threading.Lock()
        self.preflighting = False
        self.stop_generation = 0
        self.quota_lock = threading.Lock()
        self.last_quota_attempt = None
        self.verified_team = False
        self.raw_quota = None
        self.state = {"workers": {}, "run": None, "errors": []}
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        for worker in config["workers"]:
            identity = worker["id"]
            self.state["workers"].setdefault(identity, {**worker, "phase": "ready"})
        ids = list(self.state["workers"]) + [config["templateId"]]
        if any(not re.fullmatch(r"bq_[A-Za-z0-9]+", identity) for identity in ids):
            raise ValueError("Invalid briq ID")
        if config["templateId"] in self.state["workers"]:
            raise ValueError("Template must be separate from workers")
        # An interrupted launch is never replayed: it may have created an unreceipted VM.
        run = self.state["run"]
        if run and run.get("launching"):
            run["launching"] = False
            run["cancelled"] = True
            self.state["errors"].append("Controller restarted during launch: reconcile mutation intents before another run")
        self.state["quota"] = {"capacity": 0, "error": "Quota not yet verified", "fetchedAt": None}
        self.save()

    def capacities(self, quota):
        run = self.state["run"]
        active = run if run and not run.get("finishedAt") else None
        selected = set(active["workers"]) if active else set()
        reserved = active["requested"] if active else 0
        workers = list(self.state["workers"].values())
        available = [w for w in workers if w["id"] not in selected and not w.get("stopRequested")
                     and w.get("vmState") in {"running", "stopped", "sleeping"}]
        live = sum(w.get("vmState") == "running" for w in available)
        pending = max(0, reserved - len(selected)) + sum(
            self.state["workers"][identity].get("phase") in {"queued", "starting"} and
            self.state["workers"][identity].get("vmState") != "running" for identity in selected)
        headroom = max(0, min(quota["remainingConcurrency"], int(
            (quota["maxMemoryGb"] - quota["memoryGb"]) / self.config["workerMemoryGb"])) - pending)
        forks = max(0, min(100 - len(workers) - max(0, reserved - len(selected)), int(
            (quota["maxDiskGb"] - quota["diskGb"]) / self.config["workerDiskGb"]) - max(0, reserved - len(selected))))
        additional = max(0, min(100 - reserved, live + headroom, len(available) + forks))
        fresh = max(0, min(100 - reserved, headroom, forks))
        if active and (active["cancelled"] or self.clock() >= active["deadline"]):
            additional = fresh = 0
        return additional, fresh, available

    def refresh_quota(self, force=False):
        """Refresh real quota at most every ten seconds; never trust persisted identity."""
        with self.quota_lock:
            now = self.clock()
            if not force and self.last_quota_attempt is not None and now - self.last_quota_attempt < 10:
                return self.raw_quota
            self.last_quota_attempt = now
            try:
                quota = self.api.call("GET", "/v1/quota")
                if quota.get("team", {}).get("id") != self.config["expectedTeamId"]:
                    self.verified_team = False
                    raise ValueError("API credential team mismatch")
                self.verified_team = True
                money = {name: quota[name] for name in ("creditBalanceCents", "remainingTodayCents", "spentTodayCents")}
                if any(type(value) not in (int, float) or not math.isfinite(value) for value in money.values()):
                    raise ValueError("Invalid credit quota")
                with self.lock:
                    additional, fresh, _ = self.capacities(quota)
                    run = self.state["run"]
                    reserved = run["requested"] if run and not run.get("finishedAt") else 0
                    capacity = reserved + additional
                budget = max(0, min(money["creditBalanceCents"], money["remainingTodayCents"]))
                with self.lock:
                    self.raw_quota = quota
                    self.state["quota"] = {**money, "budgetCents": budget, "capacity": capacity,
                        "allowedConcurrency": capacity, "additionalCapacity": additional, "forkCapacity": fresh, "remainingConcurrency": quota["remainingConcurrency"], "fetchedAt": self.clock()}
                    run = self.state["run"]
                    if budget <= 0 and run and not run.get("finishedAt") and not run["cancelled"]:
                        run["stopReason"] = "Daily credit budget exhausted"
                        self.stop()
                    self.save()
                return quota
            except Exception as exc:
                with self.lock:
                    self.raw_quota = None
                    previous = self.state.get("quota", {})
                    self.state["quota"] = {**previous, "capacity": 0, "allowedConcurrency": 0, "additionalCapacity": 0, "forkCapacity": 0,
                        "error": "API credential team mismatch" if not self.verified_team and isinstance(exc, ValueError) else "Quota unavailable"}
                    self.save()
                return None

    def budget_ready(self):
        quota = self.state.get("quota", {})
        fetched = quota.get("fetchedAt")
        return (self.verified_team and not quota.get("error") and fetched is not None
                and 0 <= self.clock() - fetched <= 30 and quota.get("budgetCents", 0) > 0)

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.state))
            temporary.chmod(0o600)
            temporary.replace(self.path)

    def call_worker(self, worker, action, body=None):
        # Persist intent before every mutation. Unknown outcomes are never replayed.
        with self.lock:
            worker["intent"] = action
            self.save()
        try:
            result = self.api.call("POST", "/v1/briqs/" + worker["id"] + "/" + action, body)
        except APIRejected:
            with self.lock:
                worker.pop("intent", None)
                self.save()
            raise
        with self.lock:
            worker.pop("intent", None)
            self.save()
        return result

    def start(self, count, duration=900, mode="cold", add=False):
        if not self.start_lock.acquire(blocking=False):
            raise ValueError("Launch preflight already in progress")
        try:
            return self._start(count, duration, mode, add)
        finally:
            self.start_lock.release()

    def _start(self, count, duration=900, mode="cold", add=False):
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("Count must be 1–100")
        if type(duration) is not int or not 60 <= duration <= 1800:
            raise ValueError("Duration must be 60–1800 seconds")
        if not isinstance(mode, str) or mode not in {"cold", "fork"}:
            raise ValueError("Mode must be cold or fork")
        if type(add) is not bool:
            raise ValueError("Add must be a boolean")
        with self.lock:
            old = self.state["run"]
            active = old if old and not old.get("finishedAt") else None
            live_launch = active and self.launching and not active["cancelled"]
            if (self.state.get("forkIntent") and not live_launch) or any(
                w.get("gameStopError") or (w.get("intent") and not (live_launch and w["intent"] == "start"))
                for w in self.state["workers"].values()):
                raise ValueError("Reconcile unresolved mutation intents in the receipt first")
            if active and (active["cancelled"] or self.clock() >= active["deadline"]):
                raise ValueError("Stop and finish the current run first")
            if add and not active:
                raise ValueError("No active run to add to")
            if active and active["requested"] + count > 100:
                raise ValueError("Fleet cannot exceed 100 workers")
            self.preflighting = True
            generation = self.stop_generation
        try:
            quota = self.refresh_quota(force=True)
            if not quota or not self.verified_team:
                raise ValueError(self.state["quota"].get("error", "Quota unavailable"))
            if not self.budget_ready():
                raise ValueError("Daily credit budget exhausted or quota stale")
            for w in list(self.state["workers"].values()):
                b = self.api.call("GET", "/v1/briqs/" + w["id"])["briq"]
                with self.lock:
                    w.update(vmState=b["state"], host=b["internalHost"], cost=b["costCentsSoFar"])
            with self.lock:
                if active and (self.state["run"] is not active or active.get("finishedAt")):
                    raise ValueError("Current run finished during launch preflight")
                additional, fresh, rows = self.capacities(quota)
                capacity = fresh if mode == "fork" else additional
                if count > capacity:
                    raise ValueError(f"Quota permits {capacity} additional workers now (memory, disk and fleet limit); requested {count}")
                if generation != self.stop_generation or (active and self.clock() >= active["deadline"]):
                    raise ValueError("Launch cancelled by Stop all or deadline")
                rows.sort(key=lambda w: w["vmState"] != "running")
                selected = rows[:count] if mode == "cold" else []
                now = self.clock()
                run = active or {"id": uuid.uuid4().hex, "requested": 0, "startedAt": now,
                       "deadline": now + duration, "cancelled": False, "launching": False,
                       "finishedAt": None, "workers": [], "batches": []}
                source = next((w["id"] for w in self.state["workers"].values()
                               if mode == "fork" and w.get("phase") == "running" and w.get("vmState") == "running"
                               and not w.get("stopRequested")), self.config["templateId"])
                run.setdefault("pendingForkSources", []).extend([source] * (count - len(selected)))
                run.setdefault("batches", []).append({"count": count, "mode": mode, "addedAt": now, "sourceId": source})
                run["requested"] += count
                run["workers"].extend(w["id"] for w in selected)
                for w in selected:
                    w.update(phase="queued", baseline=w["cost"], error=None, stopSent=False, stopRequested=False)
                    for key in ("execId", "execState", "gameStopSent", "exitCode", "warning"):
                        w.pop(key, None)
                self.state["run"] = run
                spawn = not self.launching
                self.launching = True
                run["launching"] = True
                self.save()
        finally:
            with self.lock:
                self.preflighting = False
        if spawn:
            self.launch_thread = threading.Thread(target=self.launch, args=(run,), daemon=True)
            self.launch_thread.start()

    def launch(self, run):
        completed = False
        try:
            while True:
                with self.lock:
                    if run["cancelled"] or self.clock() >= run["deadline"]:
                        break
                    if not self.budget_ready():
                        raise RuntimeError("Credit quota unavailable or exhausted during launch")
                    queued = next((self.state["workers"][identity] for identity in run["workers"]
                                   if self.state["workers"][identity]["phase"] == "queued"), None)
                    if not queued and len(run["workers"]) >= run["requested"]:
                        # Publish completion under the same lock used by additive starts.
                        completed = True
                        self.launching = False
                        run["launching"] = False
                        self.save()
                        return
                if queued:
                    if queued["vmState"] in {"stopped", "sleeping"}:
                        self.call_worker(queued, "start")
                    with self.lock:
                        queued["phase"] = "starting"
                    continue
                name = "mc-" + run["id"][:8] + "-" + str(len(run["workers"]) + 1)
                with self.lock:
                    if run["cancelled"]:
                        break
                    source = run.get("pendingForkSources", [self.config["templateId"]])[0]
                    self.state["forkIntent"] = {"name": name, "runId": run["id"], "sourceId": source, "copies": "disk"}
                    self.save()
                result = self.api.call("POST", "/v1/briqs/" + source + "/fork",
                                       {"name": name, "stack": self.config["stack"], "sleep": "30m", "wait": False})
                with self.lock:
                    for b in result["briqs"]:
                        w = {"id": b["id"], "name": b["name"], "phase": "starting", "baseline": 0,
                             "cost": b["costCentsSoFar"], "host": b["internalHost"], "vmState": b["state"],
                             "stopSent": False, "stopRequested": run["cancelled"]}
                        self.state["workers"][w["id"]] = w
                        run["workers"].append(w["id"])
                    self.state.pop("forkIntent", None)
                    if run.get("pendingForkSources"):
                        run["pendingForkSources"].pop(0)
                    self.save()
        except Exception as exc:
            with self.lock:
                if isinstance(exc, APIRejected):
                    self.state.pop("forkIntent", None)
                self.state["errors"].append(str(exc) if isinstance(exc, RuntimeError) else "Launch failed")
                run["cancelled"] = True
        finally:
            if not completed:
                with self.lock:
                    self.launching = False
                    run["launching"] = False
                    self.save()

    def stop(self):
        with self.lock:
            self.stop_generation += 1
            run = self.state["run"]
            if run:
                run["cancelled"] = True
            # Stop all explicitly owned workers, including unused prepared workers.
            for w in self.state["workers"].values():
                if not w.get("intent") and w.get("vmState") == "running":
                    w["stopSent"] = False
                w["stopRequested"] = True
            self.save()

    def tick(self):
        with self.lock:
            run = self.state["run"]
            if run and self.clock() >= run["deadline"] and not run["finishedAt"] and not run.get("deadlineStopRequested"):
                run["deadlineStopRequested"] = True
                self.stop()
            workers = list(self.state["workers"].values())
        self.refresh_quota()
        if not self.verified_team:
            return
        # Rotate at most two exec-status checks per three-second monitor tick.
        candidates = sorted((w for w in workers if w.get("execId") and w.get("execState") == "running"),
                            key=lambda w: w.get("lastExecCheckedAt", 0))[:2]
        check_ids = {w["id"] for w in candidates}
        for w in workers:
            if run and not run.get("finishedAt") and not run.get("deadlineStopRequested") and self.clock() >= run["deadline"]:
                run["deadlineStopRequested"] = True
                self.stop()
            try:
                b = self.api.call("GET", "/v1/briqs/" + w["id"])["briq"]
                w.update(vmState=b["state"], host=b["internalHost"], cost=b["costCentsSoFar"])
                selected = run and w["id"] in run["workers"]
                stopping = w.get("stopRequested") or (selected and run["cancelled"])
                if w["id"] in check_ids and b["state"] == "running" and not stopping:
                    w["lastExecCheckedAt"] = self.clock()
                    e = self.api.call("GET", "/v1/briqs/" + w["id"] + "/execs/" + w["execId"])["exec"]
                    w.update(execState=e["state"], exitCode=e["exitCode"])
                    w.pop("warning", None)
                    if e["state"] != "running":
                        w["phase"] = "finished" if e["state"] == "exited" and e["exitCode"] == 0 else "error" if e["state"] in {"lost", "timed_out"} or e["exitCode"] not in {None, 0} else "stopped"
                        stopping = True
                if stopping:
                    if b["state"] == "running" and not w.get("stopSent"):
                        # Stop the game group before the VM snapshot can retain it.
                        # An exec observation failure must never prevent the VM stop.
                        if w.get("execId") and not w.get("gameStopSent"):
                            w["gameStopSent"] = True
                            self.save()
                            try:
                                self.api.call("DELETE", "/v1/briqs/" + w["id"] + "/execs/" + w["execId"])
                            except Exception:
                                w["gameStopError"] = "Game stop outcome requires reconciliation before reuse"
                                w["error"] = w["gameStopError"]
                        w["stopSent"] = True
                        self.save()
                        try:
                            self.call_worker(w, "stop")
                        except APIRejected:
                            w["stopSent"] = False
                            raise
                    if b["state"] in TERMINAL_VM:
                        w["stopRequested"] = False
                        if w["phase"] not in {"finished", "error"}:
                            w["phase"] = "stopped"
                elif selected and w["phase"] == "starting" and b["state"] == "running" and self.budget_ready():
                    # Adopt a recorded running game; never launch a duplicate.
                    active = [e for e in self.api.call("GET", "/v1/briqs/" + w["id"] + "/execs")["execs"]
                              if e["state"] == "running" and e["cmd"] == GAME_CMD]
                    if active:
                        e = active[0]
                    elif not w.get("intent"):
                        remaining = max(1000, int((run["deadline"] - self.clock()) * 1000))
                        e = self.call_worker(w, "exec", {"cmd": GAME_CMD, "async": True, "timeoutMs": remaining})["exec"]
                    else:
                        continue
                    w.update(execId=e["id"], execState=e["state"], phase="preparing")
                if w.get("execState") == "running" and b["state"] == "running" and not stopping:
                    try:
                        with build_opener(NoRedirect).open("http://" + w["host"] + ":8090/health", timeout=2) as response:
                            health = json.loads(response.read(4096))
                        age = health.get("frameAgeSeconds")
                        fresh = isinstance(age, (int, float)) and 0 <= age < 10
                        if health.get("error"):
                            w.update(phase="error", error="Worker reported a game error", stopRequested=True)
                        else:
                            w["phase"] = "running" if health.get("gameLoaded") and health.get("javaAlive") and fresh else "preparing"
                    except (OSError, ValueError):
                        w["phase"] = "preparing"
                if b["state"] == "error":
                    w["phase"] = "error"
            except Exception as exc:
                with self.lock:
                    if isinstance(exc, APIRejected) and str(exc) == "API HTTP 429":
                        w["warning"] = "API rate limited; status delayed"
                    else:
                        w["error"] = str(exc) if isinstance(exc, RuntimeError) else "Worker observation failed"
                    if w.get("intent"):
                        w["phase"] = "error"
                        if run:
                            run["cancelled"] = True
        with self.lock:
            if run and not self.preflighting and not run["launching"] and not self.state.get("forkIntent") and not any(w.get("intent") for w in self.state["workers"].values()) and all(self.state["workers"][i].get("vmState") in TERMINAL_VM for i in run["workers"]) and not any(w.get("stopRequested") for w in self.state["workers"].values()):
                run["finishedAt"] = run["finishedAt"] or self.clock()
            self.save()

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.state)
        quota = result["quota"]
        fetched = quota.get("fetchedAt")
        quota["ageSeconds"] = max(0, self.clock() - fetched) if fetched is not None else None
        quota["fresh"] = (not quota.get("error") and fetched is not None and 0 <= self.clock() - fetched <= 30)
        quota["canStart"] = self.budget_ready()
        with self.lock:
            if self.raw_quota and quota["canStart"]:
                additional, fresh, _ = self.capacities(self.raw_quota)
                current = self.state["run"]
                reserved = current["requested"] if current and not current.get("finishedAt") else 0
                quota.update(additionalCapacity=additional, forkCapacity=fresh,
                             capacity=reserved + additional, allowedConcurrency=reserved + additional)
        run = result["run"]
        selected = [result["workers"][i] for i in run["workers"]] if run else []
        result["summary"] = {"running": sum(w["phase"] == "running" for w in selected),
                             "preparing": (max(0, run["requested"] - len(selected)) if run and not run["cancelled"] else 0) + sum(
                                 w["phase"] in {"queued", "starting", "preparing"} and not w.get("stopRequested")
                                 and not (run and run["cancelled"]) for w in selected),
                             "vmRunning": sum(w.get("vmState") == "running" for w in selected),
                             "started": sum(bool(w.get("execId")) for w in selected),
                             "finished": sum(w["phase"] == "finished" for w in selected),
                             "errors": sum(w["phase"] == "error" or bool(w.get("error")) for w in selected),
                             "costCents": sum(max(0, w.get("cost", 0) - w.get("baseline", 0)) for w in selected),
                             "elapsed": max(0, int((run["finishedAt"] or self.clock()) - run["startedAt"])) if run else 0}
        # Neither API secrets nor arbitrary hosts go to the browser.
        for w in result["workers"].values():
            w.pop("host", None)
        return result


def make_handler(fleet):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, data, kind="application/json"):
            if kind == "application/json":
                data = json.dumps(data).encode()
            self.send_response(code)
            for key, value in {"Content-Type": kind, "Content-Length": str(len(data)), "Cache-Control": "no-store",
                               "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                return self.reply(200, Path(__file__).with_name("controller.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self.reply(200, fleet.snapshot())
            if path.startswith("/frame/"):
                w = fleet.state["workers"].get(path[7:])
                if not w or not re.fullmatch(r"[A-Za-z0-9_.:-]+", w.get("host", "")):
                    return self.reply(404, {"error": "Unknown worker"})
                try:
                    with build_opener(NoRedirect).open("http://" + w["host"] + ":8090/frame.jpg", timeout=3) as response:
                        data = response.read(2_000_001)
                    if len(data) > 2_000_000:
                        raise ValueError()
                    return self.reply(200, data, "image/jpeg")
                except (OSError, ValueError):
                    return self.reply(503, {"error": "Frame unavailable"})
            self.reply(404, {})

        def do_POST(self):
            origin = urlsplit(self.headers.get("Origin", ""))
            if (self.headers.get("X-Minecraft-Control") != "1" or
                self.headers.get("Content-Type") != "application/json" or
                origin.scheme != self.headers.get("X-Forwarded-Proto", "http") or
                origin.netloc != self.headers.get("X-Forwarded-Host", self.headers.get("Host")) or
                self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"):
                return self.reply(403, {"error": "Same-origin JSON control required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                if self.path == "/api/start":
                    fleet.start(body.get("count"), body.get("duration", 900), body.get("mode", "cold"), body.get("add", False))
                elif self.path == "/api/stop":
                    fleet.stop()
                else:
                    return self.reply(404, {})
                self.reply(202, {"ok": True})
            except (ValueError, RuntimeError) as exc:
                self.reply(409, {"error": str(exc)})

        def log_message(self, *_):
            pass
    return Handler


def main():
    config = json.loads(Path(os.environ["MINECRAFT_CONFIG"]).read_text())
    fleet = Fleet(API(), config, os.environ.get("MINECRAFT_STATE", "/work/minecraft-controller/state.json"))
    def monitor():
        while True:
            try:
                fleet.tick()
            except Exception:
                pass  # Receipt remains available; a future observation may recover.
            threading.Event().wait(3)
    def quota_monitor():
        while True:
            fleet.refresh_quota()
            threading.Event().wait(10)
    threading.Thread(target=quota_monitor, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8090"))), make_handler(fleet)).serve_forever()


if __name__ == "__main__":
    main()
