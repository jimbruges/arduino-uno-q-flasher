# Arduino UNO Q Flasher (Web UI)

A web app that prepares multiple Arduino UNO Q boards in parallel over `adb`,
with a separate guarded recovery workflow for flashing the latest Linux image.
It provides live per-device logs and replaces the original shell workflow.

<img width="800" alt="Arduino UNO Q flasher UI" src="https://github.com/user-attachments/assets/f795a71a-c1b4-4a7b-a440-304114787171" />


## Prerequisites

### Hardware

* [Amazon Basics 10 Port USB A Hub](https://www.amazon.es/-/en/Amazon-Basics-10-Port-Power-Adapter/dp/B076YRSWGW)
* 10 USB-A to USB-C cables

### Software

- **Python 3.11+**
- **adb** (Android platform-tools) on your `PATH`
  - macOS: `brew install android-platform-tools`
  - Windows: download from <https://developer.android.com/studio/releases/platform-tools>
  - Linux: `sudo apt install adb`
- **`unoq-setup.sh`** present at the project root (pushed to each device).
- Optional **`.env`** at the project root with:
  ```
  UNOQ_DEFAULT_PASSWORD=your-new-password
  ```
- **`properties.msgpack`** at the project root or top level of the chosen app
   folder. The recommended fresh-board setup requires it and pushes it to:
   - `/var/lib/arduino-app-cli/properties.msgpack` (App Lab's active state)
  - `/home/arduino/.local/share/arduino-app-cli/properties.msgpack`
  - `/tmp/properties.msgpack`

## Install & run

```bash
# Ensure Python 3.11+ is used for the venv (required by this project).
# macOS (Homebrew) if missing: brew install python@3.11
python3.11 -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# Optional but recommended: modern editable-install support
python -m pip install --upgrade pip
pip install -e .
python -m app
```

Open <http://localhost:8000>.

## Using the UI

1. Enter the WiFi credentials and click **Save**. They are stored in the local
   project `.env` file for later sessions.
2. Connect the UNO Q boards over USB-C. They appear automatically within five
   seconds; **Identify** blinks one board's red LED when cables need tracing.
3. Leave the recommended defaults enabled and click **Run selected steps on all
   boards**. This configures WiFi, marks onboarding complete, updates App Lab,
   and restores locally cached downloads.
4. Choose an app folder only when the boards need a custom app. The staged copy
   remains available for 24 hours and is restored after browser or app reloads.
   App-folder deployment stays off until explicitly enabled in the same step;
   there is no second deployment toggle under Run.
5. Optional post-update commands have their own step after the app folder. They
   remain off unless **Run commands after setup** is enabled.
5. Each device card shows:
   - status badge (idle / running / success / failed)
   - progress bar across the workflow stages (selected steps show as run,
     skipped ones are marked skipped)
   - live-tailing log panel
6. If a device fails, correct the reported problem and click **Retry**.

Run choices, post-update commands, selected examples, and the current staged
app folder are restored after reload. Passwords are not stored
in browser storage. Staged uploads expire after 24 hours.

The recommended cache-board preset prepares **Blink LED**, **Real-time
Accelerometer**, and **Edge AI Assistant**. Resetting to recommended setup
restores those selections without enabling app-folder deployment.

## Restore the latest board image

Image recovery is separate from normal ADB setup and is intentionally limited
to one board at a time because the official CLI has no target-selection flag.
The repository includes Arduino's official Apple Silicon
`arduino-flasher-cli` v0.5.3 executable under `tools/`; the app uses it directly
without a system-wide installation. Other platforms can provide a compatible
`arduino-flasher-cli` on `PATH`.

The board must enter Emergency Download Mode (EDL): disconnect it, bridge the
two EDL pins, and connect USB-C while they are bridged. The app will not start
unless exactly one Qualcomm `05c6:9008` EDL device is detected and the operator
confirms the bridge procedure. A clean flash can erase user data, needs roughly
8 GB of temporary host space, and may download about 1 GB. The first flash
stores the checksum-verified latest image under `.cache/flasher-images/`;
subsequent boards reuse it. Each board still requires the EDL bridge procedure
and confirmation. Operations have a 45-minute limit and can be canceled. After
success, disconnect USB-C, remove the bridge, and reconnect the board normally.

## Offline Edge LLM model deployment

The App Lab **Chat with a Local LLM** example uses
`llamacpp:Qwen3.5-0.8B-Q4_0` (507 MB). Export it once from a board where App Lab
has installed the model:

```bash
chmod +x export-edge-llm-model.sh
./export-edge-llm-model.sh SOURCE_BOARD_SERIAL
```

This creates the ignored local bundle `model-bundles/llamacpp/`, containing the
GGUF and `models.ini`. Both the web flasher and `uno-q-update.sh` automatically
push this bundle to `/var/lib/arduino-app-cli/models/` after the system update.
App Lab then recognizes the model as installed without downloading it on each
board. The deployment does not start the LLM example.

## Workflow (per device)

By default, package downloads are routed over USB through the Mac-hosted cache
on port 3142. The app creates a separate `adb reverse` tunnel for every board,
so no LAN routing or proxy configuration is required. Debian and Arduino APT
artifacts are stored under `.cache/packages/`; concurrent requests for the same
file are coalesced, and cached files remain available when the upstream is
temporarily unavailable. Immediately before setup, each board's clock is
verified against the Mac and corrected when needed so fresh images can validate
repository signatures before NTP is available. Original board APT sources are
restored after every run, including failures.

Use **Prepare & save shared cache** on one prepared board to start and stop selected
examples or an uploaded app, then capture the resulting downloads. The app
saves tagged Docker images, Arduino toolchain files, and prepared app runtime
caches under `.cache/workshop/`. This is the app's reusable checkpoint: it is
not a raw snapshot of the whole board. Future runs automatically restore it
before App Lab initialization.

Every checkpoint is checked against its SHA-256 manifest and archive structure
before a run. A multi-part capture is transactional: if the board disconnects,
the previous verified checkpoint is restored instead of leaving mixed files.
Marker files on each board avoid retransferring artifacts already present.

Post-update commands are disabled by default. Captured examples are ready to
launch in App Lab without starting and stopping them on every target board.
Use the example multi-select and **Prepare & save shared cache** on one device to update that
board, start and stop the selected examples, and then capture any new package
downloads, Docker images, toolchain data, and per-example runtime caches. Later
fleet runs restore those artifacts without starting and stopping the examples
again.

The app-folder and `properties.msgpack` pushes are separate run steps. Enable
**Prepare uploaded app on cache board** to start and stop the uploaded app during
a one-board warm-cache run; its generated `.cache` data is captured with the
example caches and restored to subsequent boards.

Advanced options expose the complete per-board workflow:

1. `push_setup_script` — push `unoq-setup.sh` to `/home/arduino/.unoq-setup.sh`
2. `push_env` — push `.env` (if present locally)
3. `chmod_script` — make the setup script executable
4. `change_password` — set the arduino user's password from `UNOQ_DEFAULT_PASSWORD`
   (skippable; handles the "already-changed" case gracefully)
5. `push_properties` — install and verify `properties.msgpack` in the system,
   user, and temporary App Lab paths
6. `run_setup` — execute the remote setup script (WiFi, DNS, system update),
   then restore `model-bundles/` when present
7. `prune_docker_images` — optional cleanup of unused Docker/Podman images and
   stopped containers before post-update (disabled by default)
8. `restore_workshop_cache` — restore verified Docker and app-runtime caches
9. `push_app` — push the chosen folder to `/home/arduino/ArduinoApps/`
10. `prepare_uploaded_app` / `prepare_examples` — cache-board-only start/stop
11. `post_update` — run configured workshop preparation commands in order;
   any failure marks the board failed
12. `verify_ready` — require a responsive App Lab CLI/daemon, populated assets,
   a working Docker daemon, sufficient free disk, and the bundled model
13. `capture_cache` — save and verify warmed artifacts after a cache-board run

## ADB parallelism — how many boards can I flash at once?

There are three different limits to keep in mind:

| Limit | Where | Practical impact |
| --- | --- | --- |
| **adb server transports** | `adb` itself | Historical 16-device cap; raised to 128 in platform-tools r30+. Not the bottleneck for typical fleets. |
| **USB host controller** | Your motherboard | Spec allows 127 devices per controller; each shares 480 Mbps (USB 2.0) or 5 Gbps (USB 3.x). A typical PC has 2–4 controllers. |
| **Bus bandwidth + power** | Your hubs | UNO Q draws real current. Bus-powered hubs sag past ~4 boards. Use **powered** hubs, and prefer splitting boards across multiple host ports / controllers. |

All connected boards begin setup together and package updates run concurrently.
Large artifact streams are limited to two at a time because parallel `adb push`
shares the USB controller's bandwidth; this does not exclude or defer a board's
overall setup task.

**Rule of thumb**: 8–16 boards per host machine is comfortable. For 32+
either split across multiple machines or add a PCIe USB controller and
distribute boards across separate controllers.
