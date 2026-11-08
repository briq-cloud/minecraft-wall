# Minecraft wall

Run real Minecraft Java clients in separate [briq](https://briq.cloud) VMs and watch their screens together. Choose 1–100 clients, start or stop the fleet, and track running games, elapsed time, errors, cost and remaining daily credits.

Each client plays an independent built-in **demo world** with scripted keyboard movement. No Minecraft account is needed. This is a prototype, not multiplayer or an AI player.

## Run it

You need Python 3.10+, Node.js, credits and a production API key from [Agents](https://app.briq.cloud/agents). All cloud commands below use production.

```sh
git clone https://github.com/briq-cloud/minecraft-wall.git
cd minecraft-wall
npm install -g @briq-cloud/cli
export BRIQ_API_URL=https://api.briq.cloud
read -rs BRIQ_API_KEY # paste the full secret key, then press Enter
export BRIQ_API_KEY
briq quota
```

### 1. Prepare one template

The template holds Java, Minecraft assets and a saved demo world. Workers fork its disk, so installation happens once.

```sh
mkdir -p .minecraft-wall
python3 stack.py --count 1 > .minecraft-wall/stack.json
briq --json up --file .minecraft-wall/stack.json > .minecraft-wall/template.json
export TEMPLATE=$(python3 -c 'import json; print(json.load(open(".minecraft-wall/template.json"))["briqs"][0]["id"])')
briq exec --async --timeout 600000 "$TEMPLATE" bash /work/minecraft-prototype/install.sh
```

If creation is still in progress, check `briq status "$TEMPLATE"` before installing. Installation returns an exec ID; check `briq exec-result "$TEMPLATE" EXEC_ID` until it exits successfully.

```sh
briq exec --async --timeout 600000 "$TEMPLATE" bash /work/minecraft-prototype/run.sh
briq expose "$TEMPLATE" 8080
```

Open that private noVNC URL. Click **Play Demo World**, then **Continue Playing**. Once the world loads, press Escape and **Save and Quit to Title** to save it for workers.

```sh
briq stop "$TEMPLATE"
```

### 2. Start the wall

```sh
python3 deploy.py --template "$TEMPLATE"
```

Open the printed dashboard link and follow the controller's private URL. Choose **10** clients and click **Start fleet**. The first launch can take a few minutes. Tiles become running when a world and fresh capture are available.

**Stop all** stops workers and keeps results visible on the separate controller. Runs stop automatically after 15 minutes while the controller runs; game execs also have a bounded deadline.

The slider goes to 100; actual capacity depends on key, team, memory and disk quotas. The controller uses one live slot. Launch is disabled when credits or today's allowance are exhausted, or quota cannot be verified. Stop remains available.

## How it works

```text
briq API → fork template → Minecraft + Xvfb + Mesa → FFmpeg JPEG
     ↑                                                   ↓
controller: start / stop / quota / billing ← browser wall ← frames
```

| Technology | Purpose |
| --- | --- |
| briq CLI and production REST API | Create VMs, fork disks, run commands, stop workers and read quota/cost |
| Minecraft Java 1.21.1 + Java 21 | Actual graphical game clients in Mojang's demo mode |
| Xvfb + Openbox | Virtual Linux display and window management |
| Mesa | Render OpenGL on the CPU without a GPU |
| FFmpeg | Capture each display as an atomic JPEG twice per second |
| Python standard library + browser JavaScript | Proxy private frames, tile screens and manage the fleet |
| xdotool + noVNC | Script movement and prepare the template interactively |

Workers use **small: 1 vCPU / 2 GB RAM**, with a 1 GB Java heap, 640×360 display, two-chunk render distance and a 10 FPS target. Ten real clients have been exercised at this size. A 512 MB VM failed with a kernel out-of-memory kill even with a 256 MB heap, 320×240 and a 5 FPS target. Performance at 100 clients has not been validated.

Cost is metered usage for the selected workers since this run started. It excludes the controller, template and previous usage. The total can update as API metering catches up. **Credits remaining today** is the lower of the account balance and remaining daily allowance.

## Cleanup and development

Stop all keeps worker disks for reuse. To remove this installation and its disks, review the stack first, then destroy it:

```sh
briq list --stack minecraft-wall
briq destroy --stack minecraft-wall
```

Keep `.minecraft-wall/`: it contains deployment receipts that prevent duplicate creation. If a mutation times out, reconcile its receipt in the dashboard before retrying. Keep the controller running while a fleet is active. Private wall URLs grant control of this installation; share them carefully.

```sh
python3 -m unittest discover -p 'test_*.py'
node test_controller_ui.cjs
```

Code is MIT licensed. Minecraft and its assets belong to Mojang/Microsoft. Setup downloads them from official distribution endpoints; they are not included here.
