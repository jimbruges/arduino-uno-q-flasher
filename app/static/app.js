// Arduino UNO Q Flasher — frontend logic.
// Single page; no build step.

const STAGES = [
    "push_setup_script",
    "push_env",
    "chmod_script",
    "change_password",
    "push_properties",
    "run_setup",
    "prune_docker_images",
    "restore_workshop_cache",
    "push_app",
    "prepare_uploaded_app",
    "prepare_examples",
    "post_update",
    "verify_ready",
    "capture_cache",
];

const SKIP_STEP_WIFI_STAGES = ["push_env", "change_password"];

const DEVICE_POLL_MS = 5000;
const UI_PREFS_KEY = "unoq-flasher-ui-v1";
const UI_UPLOAD_KEY = "unoq-flasher-upload-v1";
const RECOMMENDED_EXAMPLES = [
    {
        id: "examples:blink",
        name: "Blink LED",
        description: "Blink LED from Python",
    },
    {
        id: "examples:real-time-accelerometer",
        name: "Real-time Accelerometer",
        description: "Accelerometer inspiration",
    },
    {
        id: "examples:inspirational/platform_unoq/edge-ai-assistant",
        name: "Edge AI Assistant",
        description: "Chatbot powered by a local LLM",
    },
];
const PERSISTED_CONTROL_IDS = [
    "run-use-package-cache",
    "run-step-wifi",
    "run-step-properties",
    "run-step-setup",
    "run-step-prune",
    "run-step-app",
    "run-step-warm-app",
    "run-step-post-update",
];

const state = {
    upload: null,
    folderFiles: null,
    devices: [],
    cards: new Map(),
    runId: null,
    ws: null,
    wifiOk: false,
    examples: [],
    exampleSource: null,
    savedExampleIds: new Set(),
    flashStatus: null,
    flashPoll: null,
    health: null,
    cacheIntegrity: "unchecked",
    settingsLoaded: false,

    runFinalStatusText: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// ---------- bootstrap ----------

async function init() {
    restoreUiPreferences();
    wireControls();
    renderFolderStepFromState();
    await restoreStagedUpload();
    await populateSettingsForm();
    await refreshHealth();
    await verifyWorkshopCache();
    await refreshDevices();
    refreshFlashStatus();
    setInterval(refreshDevices, DEVICE_POLL_MS);
}

async function restoreStagedUpload() {
    const uploadId = localStorage.getItem(UI_UPLOAD_KEY);
    if (!uploadId) return;
    try {
        const response = await fetch(`/api/uploads/${encodeURIComponent(uploadId)}`);
        if (!response.ok) throw new Error("expired");
        state.upload = await response.json();
        renderFolderStepFromState();
        renderEimList(state.upload.eim_files || []);
    } catch (_) {
        localStorage.removeItem(UI_UPLOAD_KEY);
        state.upload = null;
        renderFolderStepFromState();
        $("#step-folder-state").textContent = "Previous folder expired; choose it again if needed";
    }
}

function wireControls() {
    $("#folder-input").addEventListener("change", onFolderPicked);
    $("#refresh-btn").addEventListener("click", () => {
        refreshHealth();
        refreshDevices();
    });
    $("#wifi-check-all-btn")?.addEventListener("click", wifiCheckAllDevices);
    $("#run-btn").addEventListener("click", () => startRun());
    $("#save-settings-btn").addEventListener("click", saveSettings);
    $("#reset-recommended-btn")?.addEventListener("click", resetRecommendedSetup);
    $("#flash-refresh-btn")?.addEventListener("click", refreshFlashStatus);
    $("#flash-edl-confirm")?.addEventListener("change", updateFlashButton);
    $("#flash-start-btn")?.addEventListener("click", startImageFlash);
    $("#flash-cancel-btn")?.addEventListener("click", cancelImageFlash);
    for (const id of [
        "#run-step-wifi",
        "#run-step-app",
        "#run-step-properties",
        "#run-step-setup",
        "#run-step-prune",
        "#run-step-warm-app",
        "#run-step-post-update",
        "#run-use-package-cache",
    ]) {
        const el = $(id);
        if (el) el.addEventListener("change", () => {
            saveUiPreferences();
            if (id === "#run-step-wifi") renderWifiStepFromState();
            if (id === "#run-step-app") renderFolderStepFromState();
            updateStartButtons();
        });
    }
    $("#post-update-cmd")?.addEventListener("input", () => {
        saveUiPreferences();
        updateStartButtons();
    });
    $("#example-search")?.addEventListener("input", renderExampleOptions);
    $("#select-inspirational-btn")?.addEventListener("click", () => {
        for (const example of state.examples) example.selected = example.inspirational;
        saveUiPreferences();
        renderExampleOptions();
    });
    $("#clear-examples-btn")?.addEventListener("click", () => {
        for (const example of state.examples) example.selected = false;
        saveUiPreferences();
        renderExampleOptions();
    });
    for (const btn of $$(".pw-toggle")) {
        btn.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            togglePasswordVisibility(btn);
        });
    }
}

function resetRecommendedSetup() {
    if (!window.confirm("Restore recommended defaults? This clears custom commands and replaces your selected cache examples.")) return;
    const checked = {
        "run-use-package-cache": true,
        "run-step-wifi": true,
        "run-step-properties": true,
        "run-step-setup": true,
        "run-step-prune": false,
        "run-step-app": false,
        "run-step-warm-app": false,
        "run-step-post-update": false,
    };
    for (const [id, value] of Object.entries(checked)) $(`#${id}`).checked = value;
    $("#post-update-cmd").value = "";
    applyRecommendedExamples();
    saveUiPreferences();
    renderWifiStepFromState();
    renderFolderStepFromState();
    renderExampleOptions();
    updateStartButtons();
}

function applyRecommendedExamples() {
    const recommendedIds = new Set(RECOMMENDED_EXAMPLES.map((example) => example.id));
    for (const example of state.examples) example.selected = recommendedIds.has(example.id);
    state.savedExampleIds = recommendedIds;
}

async function refreshFlashStatus() {
    try {
        const response = await fetch("/api/flashing/status");
        const status = await response.json();
        state.flashStatus = status;
        $("#flash-tool-status").textContent = status.tool_available
            ? "Arduino Flasher CLI is ready from this project."
            : "Arduino Flasher CLI is not available for this computer.";
        const image = status.image || {};
        $("#flash-image-status").textContent = image.available
            ? image.cached
                ? `Latest image ${image.version} is verified and saved on this Mac.`
                : `Latest image ${image.version} will download once, then be reused for each board.`
            : `Could not check the latest image${image.error ? `: ${image.error}` : "."}`;
        const enoughDisk = status.free_bytes >= status.required_free_bytes;
        $("#flash-disk-status").textContent = enoughDisk
            ? `${formatBytes(status.free_bytes)} free; image flashing has enough workspace.`
            : `${formatBytes(status.free_bytes)} free; at least ${formatBytes(status.required_free_bytes)} is required.`;
        const deviceText = status.edl_devices === 1
            ? "One EDL board detected and ready."
            : `${status.edl_devices} EDL boards detected; connect exactly one.`;
        $("#flash-device-status").textContent = deviceText;
        const active = status.status === "starting" || status.status === "running";
        $("#flash-result").textContent = active
            ? "Flashing in progress. Do not disconnect the board."
            : status.status === "succeeded"
                ? "Flash complete. Unplug USB-C, remove the EDL jumper, then reconnect."
                : status.status === "failed"
                    ? `Flash failed (exit ${status.exit_code}). Check the log below.`
                    : status.status === "canceled"
                        ? "Image operation canceled. The board was not reported as successfully flashed."
                    : "";
        $("#flash-cancel-btn").hidden = !active;
        const log = $("#flash-log");
        log.textContent = (status.logs || []).join("\n");
        log.hidden = !status.logs?.length;
        updateFlashButton();
        if (active && !state.flashPoll) {
            state.flashPoll = setInterval(refreshFlashStatus, 1000);
        } else if (!active && state.flashPoll) {
            clearInterval(state.flashPoll);
            state.flashPoll = null;
        }
    } catch (_) {
        $("#flash-tool-status").textContent = "Unable to check image flashing support.";
    }
}

async function cancelImageFlash() {
    if (!window.confirm("Cancel the current image operation?")) return;
    $("#flash-cancel-btn").disabled = true;
    try {
        const response = await fetch("/api/flashing/cancel", {method: "POST"});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "Unable to cancel");
    } catch (error) {
        $("#flash-result").textContent = error.message;
    } finally {
        $("#flash-cancel-btn").disabled = false;
        await refreshFlashStatus();
    }
}

function updateFlashButton() {
    const status = state.flashStatus;
    const active = status?.status === "starting" || status?.status === "running";
    $("#flash-start-btn").disabled = !status?.tool_available
        || status?.edl_devices !== 1
        || status?.free_bytes < status?.required_free_bytes
        || !$("#flash-edl-confirm").checked
        || active;
}

async function startImageFlash() {
    const preserveUser = $("#flash-preserve-user").checked;
    const warning = preserveUser
        ? "Flash the detected UNO Q and try to preserve user files?"
        : "Erase and flash the detected UNO Q with the latest image?";
    if (!window.confirm(`${warning}\n\nDo not disconnect it until flashing completes.`)) return;
    $("#flash-start-btn").disabled = true;
    try {
        const response = await fetch("/api/flashing/start", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                edl_pins_confirmed: $("#flash-edl-confirm").checked,
                preserve_user: preserveUser,
            }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "Unable to start flashing");
        $("#flash-edl-confirm").checked = false;
        await refreshFlashStatus();
    } catch (error) {
        $("#flash-result").textContent = error.message;
        await refreshFlashStatus();
    }
}

function restoreUiPreferences() {
    let preferences = {};
    try {
        preferences = JSON.parse(localStorage.getItem(UI_PREFS_KEY) || "{}");
    } catch (_) {
        preferences = {};
    }
    for (const id of PERSISTED_CONTROL_IDS) {
        if (typeof preferences[id] === "boolean" && $(`#${id}`)) {
            $(`#${id}`).checked = preferences[id];
        }
    }
    const postUpdateControl = $("#post-update-cmd");
    if (postUpdateControl && typeof preferences.postUpdateCmd === "string") {
        postUpdateControl.value = preferences.postUpdateCmd;
    }
    state.savedExampleIds = new Set(
        Array.isArray(preferences.exampleApps)
            ? preferences.exampleApps
            : RECOMMENDED_EXAMPLES.map((example) => example.id),
    );
}

function saveUiPreferences() {
    const preferences = {};
    for (const id of PERSISTED_CONTROL_IDS) {
        const control = $(`#${id}`);
        if (control) preferences[id] = control.checked;
    }
    preferences.postUpdateCmd = $("#post-update-cmd")?.value || "";
    preferences.exampleApps = state.examples.length
        ? selectedExampleIds()
        : [...state.savedExampleIds];
    localStorage.setItem(UI_PREFS_KEY, JSON.stringify(preferences));
}

function togglePasswordVisibility(btn) {
    const target = document.getElementById(btn.dataset.target);
    if (!target) return;
    const nowShowing = target.type === "password";
    target.type = nowShowing ? "text" : "password";
    btn.dataset.showing = nowShowing ? "true" : "false";
    const which = target.id === "setting-wifi-pw" ? "WiFi password" : "device password";
    btn.setAttribute("aria-label", (nowShowing ? "Hide " : "Show ") + which);
}

// ---------- health & WiFi step ----------

async function refreshHealth() {
    try {
        const r = await fetch("/api/health");
        const j = await r.json();
        state.health = j;
        const el = $("#health");
        if (!j.adb_available) {
            el.className = "health bad";
            el.textContent = `ADB not found: ${j.adb_error}`;
        } else if (!j.setup_script_available || !j.properties_available) {
            el.className = "health bad";
            el.textContent = "required project file missing";
        } else {
            el.className = "health ok";
            const pw = j.password_configured ? "device pw set" : "no device pw";
            el.textContent = `ADB ready · ${pw}`;
        }
        renderPackageCacheStatus(j.package_cache);
        renderWorkshopCacheStatus(j.workshop_cache);
        state.wifiOk = j.wifi_ssid_configured && j.wifi_password_configured;
        renderWifiStep(j);
        updateStartButtons();
    } catch (e) {
        const el = $("#health");
        el.className = "health bad";
        el.textContent = "backend unreachable";
    }
}

async function verifyWorkshopCache() {
    const el = $("#workshop-cache-status");
    if (el) el.textContent = "Checking saved app and toolchain files...";
    try {
        const response = await fetch("/api/cache/verify", {method: "POST"});
        const cache = await response.json();
        if (!response.ok) throw new Error(cache.detail || `HTTP ${response.status}`);
        state.cacheIntegrity = cache.integrity;
        renderWorkshopCacheStatus(cache);
    } catch (error) {
        state.cacheIntegrity = "invalid";
        if (el) el.textContent = `Could not verify saved files: ${error}`;
    }
    updateStartButtons();
}

function renderPackageCacheStatus(cache) {
    const el = $("#package-cache-status");
    if (!el || !cache) return;
    const size = formatBytes(cache.size_bytes || 0);
    const served = formatBytes(cache.bytes_from_cache || 0);
    el.textContent = cache.running
        ? `${size} stored · ${cache.hits || 0} hits · ${served} served from Mac`
        : "Package cache is not running";
}

function renderWorkshopCacheStatus(cache) {
    const el = $("#workshop-cache-status");
    if (!el || !cache) return;
    if (!cache.images_ready) {
        state.cacheIntegrity = cache.integrity || "verified";
        el.textContent = "No prepared-app checkpoint yet; package downloads are still cached";
        return;
    }
    state.cacheIntegrity = cache.integrity || "unchecked";
    if (state.cacheIntegrity === "invalid") {
        el.textContent = "Prepared app cache is damaged. Prepare & save it again on one board, or turn restoration off.";
        return;
    }
    const toolchain = cache.arduino_ready
        ? ` + ${formatBytes(cache.arduino_archive_bytes)} toolchain`
        : "";
    const runtime = cache.app_runtime_ready
        ? ` + ${formatBytes(cache.app_runtime_archive_bytes)} app cache`
        : "";
    const integrity = state.cacheIntegrity === "verified" ? "verified · " : "";
    el.textContent = `${integrity}${cache.images.length} images · ${formatBytes(cache.image_archive_bytes)}${toolchain}${runtime} · source ${cache.source_device}`;
}

function renderWifiStep(health) {
    state.wifiHealth = health;
    renderWifiStepFromState();
}

function renderWifiStepFromState() {
    const step = $("#step-wifi");
    const stateEl = $("#step-wifi-state");
    if (!$("#run-step-wifi")?.checked) {
        step.dataset.status = "skipped";
        stateEl.textContent = "off — keeping board credentials";
        return;
    }
    const h = state.wifiHealth || {};
    const ssidSet = !!h.wifi_ssid_configured;
    const pwSet = !!h.wifi_password_configured;
    if (ssidSet && pwSet) {
        step.dataset.status = "configured";
        stateEl.textContent = "✓ configured";
    } else if (!ssidSet && !pwSet) {
        step.dataset.status = "warning";
        stateEl.textContent = "SSID + password needed";
    } else if (!ssidSet) {
        step.dataset.status = "warning";
        stateEl.textContent = "SSID needed";
    } else {
        step.dataset.status = "warning";
        stateEl.textContent = "password needed";
    }
}

function renderFolderStepFromState() {
    const step = $("#step-folder");
    const stateEl = $("#step-folder-state");
    const deploy = !!$("#run-step-app")?.checked;
    if (state.upload) {
        step.dataset.status = deploy ? "configured" : "optional";
        const mb = state.folderFiles ? approxSize(state.folderFiles) : "";
        const folder = mb
            ? `${state.upload.folder_name} · ${state.upload.file_count} files · ${mb}`
            : `${state.upload.folder_name} · ${state.upload.file_count} files`;
        stateEl.textContent = deploy ? `will deploy · ${folder}` : `selected, deployment off · ${folder}`;
    } else if (deploy) {
        step.dataset.status = "warning";
        stateEl.textContent = "choose a folder";
    } else {
        step.dataset.status = "optional";
        stateEl.textContent = "off";
    }
}

// ---------- settings (always-visible inline form in Step 1) ----------

async function populateSettingsForm() {
    try {
        const r = await fetch("/api/settings");
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        // Pre-populate all three fields with the actual saved values so the
        // user can (a) verify them with the show/hide button, and (b) edit
        // in-place. Passwords stay masked as type="password" until revealed.
        $("#setting-ssid").value = j.UNOQ_WIFI_SSID || "";
        $("#setting-wifi-pw").value = j.UNOQ_WIFI_PASSWORD || "";
        $("#setting-device-pw").value = j.UNOQ_DEFAULT_PASSWORD || "";
        $("#setting-wifi-pw").placeholder = j.UNOQ_WIFI_PASSWORD_set
            ? "(set — click show to reveal)" : "••••••••";
        $("#setting-device-pw").placeholder = j.UNOQ_DEFAULT_PASSWORD_set
            ? "(set — click show to reveal)" : "••••••••";
        renderEnvFilePath(j);
        state.settingsLoaded = true;
        $("#settings-status").textContent = "loaded";
        updateStartButtons();
    } catch (e) {
        state.settingsLoaded = false;
        $("#settings-status").textContent = `load failed: ${e}`;
        updateStartButtons();
    }
}

function renderEnvFilePath(j) {
    const el = $("#env-file-path");
    if (!el) return;
    const path = j.env_file_path;
    if (!path) {
        el.hidden = true;
        return;
    }
    el.hidden = false;
    el.innerHTML = "";
    const label = document.createElement("span");
    label.className = "env-label";
    label.textContent = j.env_file_exists ? "stored at" : "will be created at";
    const value = document.createElement("span");
    value.textContent = path;
    el.appendChild(label);
    el.appendChild(value);
}

async function saveSettings() {
    // Fields are pre-populated with the saved values, so we send whatever's
    // currently in them. An intentionally-cleared field will overwrite the
    // stored value with an empty string.
    const body = {
        UNOQ_WIFI_SSID: $("#setting-ssid").value.trim(),
        UNOQ_WIFI_PASSWORD: $("#setting-wifi-pw").value,
        UNOQ_DEFAULT_PASSWORD: $("#setting-device-pw").value,
    };
    if ($("#run-step-wifi")?.checked && (!body.UNOQ_WIFI_SSID || !body.UNOQ_WIFI_PASSWORD)) {
        $("#settings-status").textContent = "WiFi name and password are required";
        return;
    }
    try {
        const r = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!r.ok) {
            const j = await r.json().catch(() => ({}));
            $("#settings-status").textContent = `save failed: ${j.detail || r.status}`;
            return;
        }
        $("#settings-status").textContent = "saved ✓";
        await refreshHealth();
        await populateSettingsForm();
        setTimeout(() => {
            $("#settings-status").textContent = "";
        }, 1500);
    } catch (e) {
        $("#settings-status").textContent = `save error: ${e}`;
    }
}

// ---------- devices ----------

async function refreshDevices() {
    try {
        const r = await fetch("/api/devices");
        if (!r.ok) {
            const j = await r.json().catch(() => ({}));
            updateRunStepState(j.detail || `error: ${r.status}`);
            return;
        }
        const j = await r.json();
        state.devices = j.devices;
        renderDeviceGrid();
        await refreshExamples();
        updateStartButtons();
    } catch (e) {
        updateRunStepState("error fetching devices");
    }
}

function renderDeviceGrid() {
    const grid = $("#devices-grid");
    const tpl = $("#device-card-template");
    const seen = new Set();
    const runActive = state.runId !== null;

    for (const d of state.devices) {
        seen.add(d.serial);
        if (state.cards.has(d.serial)) continue;

        const node = tpl.content.firstElementChild.cloneNode(true);
        node.dataset.serial = d.serial;
        node.querySelector(".device-serial").textContent = d.serial;

        const badgeEl = node.querySelector(".status-badge");
        const progressEl = node.querySelector(".progress-fill");
        const stageEl = node.querySelector(".current-stage");
        const logEl = node.querySelector(".log-panel");
        const retryBtn = node.querySelector(".retry-btn");
        const diagnoseBtn = node.querySelector(".diagnose-btn");
        const identifyBtn = node.querySelector(".identify-btn");
        const wifiCheckBtn = node.querySelector(".wifi-check-btn");
        const captureCacheBtn = node.querySelector(".capture-cache-btn");
        const warmCacheBtn = node.querySelector(".warm-cache-btn");
        const wifiBadgeEl = node.querySelector(".wifi-badge");
        const elapsedEl = node.querySelector(".elapsed");
        const failureEl = node.querySelector(".failure-reason");
        const summaryEl = node.querySelector(".summary-panel");
        const skipInputs = Array.from(node.querySelectorAll(".skip-toggle"));

        const logFollowState = { follow: true };
        logEl.addEventListener("scroll", () => {
            const nearBottom =
                (logEl.scrollTop + logEl.clientHeight) >= (logEl.scrollHeight - 12);
            logFollowState.follow = nearBottom;
        });

        retryBtn.addEventListener("click", () => retryDevice(d.serial));
        diagnoseBtn.addEventListener("click", () => diagnoseWithCopilot(d.serial));
        identifyBtn.addEventListener("click", () => identifyDevice(d.serial));
        wifiCheckBtn.addEventListener("click", () => wifiCheckDevice(d.serial));
        captureCacheBtn.addEventListener("click", () => captureImageCache(d.serial));
        warmCacheBtn.addEventListener("click", () => startRun(d.serial, true));

        grid.appendChild(node);
        state.cards.set(d.serial, {
            card: node, badgeEl, progressEl, stageEl, logEl, retryBtn, diagnoseBtn, identifyBtn,
            wifiCheckBtn, wifiBadgeEl, elapsedEl, failureEl, summaryEl, skipInputs, logFollowState,
            captureCacheBtn,
            warmCacheBtn,
        });
    }

    if (!runActive) {
        for (const serial of Array.from(state.cards.keys())) {
            if (!seen.has(serial)) {
                const c = state.cards.get(serial);
                c.card.remove();
                state.cards.delete(serial);
            }
        }
    }
}

async function refreshExamples() {
    const serial = state.devices[0]?.serial;
    if (!serial) {
        state.examples = [];
        state.exampleSource = null;
        renderExampleOptions();
        return;
    }
    if (state.exampleSource === serial && state.examples.length > 0) return;
    const selected = new Set([...state.savedExampleIds, ...selectedExampleIds()]);
    try {
        const response = await fetch(`/api/devices/${encodeURIComponent(serial)}/examples`);
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
        state.examples = (result.examples || []).map((example) => ({
            ...example,
            selected: selected.has(example.id),
        }));
        for (const recommended of RECOMMENDED_EXAMPLES) {
            if (selected.has(recommended.id) && !state.examples.some((example) => example.id === recommended.id)) {
                state.examples.push({
                    ...recommended,
                    inspirational: true,
                    status: "not listed on this board",
                    selected: true,
                });
            }
        }
        state.exampleSource = serial;
        state.savedExampleIds.clear();
        renderExampleOptions();
    } catch (error) {
        $("#example-options").textContent = `Could not load examples: ${error}`;
    }
}

function selectedExampleIds() {
    return state.examples.filter((example) => example.selected).map((example) => example.id);
}

function renderExampleOptions() {
    const container = $("#example-options");
    if (!container) return;
    const query = ($("#example-search")?.value || "").trim().toLowerCase();
    container.innerHTML = "";
    for (const example of state.examples) {
        const haystack = `${example.name} ${example.id} ${example.description}`.toLowerCase();
        if (query && !haystack.includes(query)) continue;
        const label = document.createElement("label");
        label.className = "example-option";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = example.selected;
        input.addEventListener("change", () => {
            example.selected = input.checked;
            saveUiPreferences();
            updateExampleSummary();
            updateRunStepState();
        });
        const text = document.createElement("span");
        text.textContent = `${example.name} · ${example.id}`;
        label.append(input, text);
        container.appendChild(label);
    }
    if (!container.children.length) {
        const empty = document.createElement("span");
        empty.className = "muted";
        empty.textContent = state.examples.length ? "No matching examples." : "No examples available.";
        container.appendChild(empty);
    }
    updateExampleSummary();
    updateRunStepState();
}

function updateExampleSummary() {
    const count = selectedExampleIds().length;
    const summary = $("#example-picker-summary");
    if (summary) summary.textContent = count ? `${count} example${count === 1 ? "" : "s"} selected` : "No examples selected";
    const cacheSummary = $("#cache-preparation-summary");
    if (cacheSummary) cacheSummary.textContent = `Cache-board preparation · ${count ? `${count} example${count === 1 ? "" : "s"} selected` : "no examples selected"}`;
}

async function captureImageCache(serial) {
    const c = state.cards.get(serial);
    if (!c) return;
    const btn = c.captureCacheBtn;
    btn.disabled = true;
    btn.textContent = "Saving...";
    appendLog(serial, "Capturing all tagged Docker images to the Mac cache...", "info");
    try {
        const response = await fetch(`/api/cache/workshop/${encodeURIComponent(serial)}`, {
            method: "POST",
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
        appendLog(
            serial,
            `Captured ${result.images.length} images, ${formatBytes(result.arduino_archive_bytes)} toolchain, and ${formatBytes(result.app_runtime_archive_bytes)} app cache.`,
            "info",
        );
        await verifyWorkshopCache();
    } catch (error) {
        appendLog(serial, `image capture failed: ${error}`, "err");
    } finally {
        btn.disabled = false;
        btn.textContent = "Save current files as cache";
    }
}

// ---------- folder picker (Step 2) ----------

async function onFolderPicked(e) {
    const files = e.target.files;
    if (!files || files.length === 0) return;
    state.folderFiles = files;
    const rootName = (files[0].webkitRelativePath || files[0].name).split("/")[0];
    state.upload = null;
    $("#step-folder-state").textContent = `${rootName} — uploading ${files.length} files…`;
    $("#step-folder").dataset.status = "optional";
    renderEimList(null);

    const fd = new FormData();
    fd.append("folder_name", rootName);
    for (const f of files) {
        fd.append("files", f, f.name);
        fd.append("paths", f.webkitRelativePath || f.name);
    }
    try {
        const r = await fetch("/api/upload", { method: "POST", body: fd });
        if (!r.ok) {
            const j = await r.json().catch(() => ({}));
            $("#step-folder-state").textContent = `upload failed: ${j.detail || r.status}`;
            $("#step-folder").dataset.status = "warning";
            return;
        }
        const j = await r.json();
        state.upload = j;
        localStorage.setItem(UI_UPLOAD_KEY, j.upload_id);
        $("#run-step-app").checked = true;
        saveUiPreferences();
        renderFolderStepFromState();
        renderEimList(j.eim_files || []);
        updateStartButtons();
    } catch (err) {
        $("#step-folder-state").textContent = `upload error: ${err}`;
        $("#step-folder").dataset.status = "warning";
    }
}

function renderEimList(eimFiles) {
    const wrap = $("#eim-info");
    const ul = $("#eim-list");
    ul.innerHTML = "";
    if (eimFiles == null || eimFiles.length === 0) {
        wrap.hidden = eimFiles == null;
        if (eimFiles && eimFiles.length === 0) {
            wrap.hidden = false;
            const li = document.createElement("li");
            li.className = "eim-empty";
            li.textContent = "no .eim files";
            ul.appendChild(li);
        }
        return;
    }
    wrap.hidden = false;
    for (const e of eimFiles) {
        const li = document.createElement("li");
        const name = document.createElement("span");
        name.className = "eim-name";
        name.textContent = e.path;
        const size = document.createElement("span");
        size.className = "eim-size";
        size.textContent = formatBytes(e.size_bytes);
        li.appendChild(name);
        li.appendChild(size);
        ul.appendChild(li);
    }
}

function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n >= 1024 * 1024 * 1024) return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function approxSize(files) {
    let total = 0;
    for (const f of files) total += f.size;
    return formatBytes(total);
}

// ---------- Step 4: run buttons ----------

function updateStartButtons() {
    const haveDevices = state.devices.length > 0;
    const haveFolder = !!state.upload;
    const idle = state.runId === null;
    const doWifi = !!$("#run-step-wifi")?.checked;
    const doApp = !!$("#run-step-app")?.checked;
    const doProperties = !!$("#run-step-properties")?.checked;
    const doSetup = !!$("#run-step-setup")?.checked;
    const doPost = !!$("#run-step-post-update")?.checked;
    const doPrune = !!$("#run-step-prune")?.checked;
    const useCache = !!$("#run-use-package-cache")?.checked;
    const postCmd = (($("#post-update-cmd")?.value) || "").trim();
    const setupAvailable = !!state.health?.setup_script_available;
    const propertiesAvailable = !!state.health?.properties_available
        || !!state.upload?.properties_available;

    const step1ok = state.wifiOk;
    const step2ok = haveFolder;

    const needsStep1Inputs = doWifi;
    const needsStep2Inputs = doApp;
    const needsPostCommand = doPost;
    const hasSelectedAction = doWifi || doApp || doProperties || doSetup || doPrune || doPost;
    const ready = haveDevices
        && idle
        && hasSelectedAction
        && (!needsStep1Inputs || step1ok)
        && (!needsStep2Inputs || step2ok)
        && (!needsPostCommand || postCmd.length > 0)
        && (!doSetup || setupAvailable)
        && (!doProperties || propertiesAvailable)
        && (!useCache || state.cacheIntegrity !== "invalid")
        && state.settingsLoaded;

    const btn = $("#run-btn");
    btn.disabled = !ready;
    if (ready) {
        const selected = ["run setup"];
        if (doWifi) selected.unshift("WiFi/password");
        if (doApp) selected.unshift("app push");
        if (doProperties) selected.push("properties");
        if (!doSetup) selected.splice(selected.indexOf("run setup"), 1);
        if (doPrune) selected.push("docker prune");
        if (doPost) selected.push("post-update");
        if (selected.length === 0) selected.push("no-op");
        btn.title = `Run selected steps: ${selected.join(", ")}`;
    } else if (!idle) {
        btn.title = "A run is in progress";
    } else if (!haveDevices) {
        btn.title = "Connect at least one UNO Q";
    } else {
        btn.title = missingFor({
            haveDevices,
            step1ok,
            step2ok,
            idle,
            needsStep1Inputs,
            needsStep2Inputs,
            needsPostCommand,
            postCmd,
            doSetup,
            setupAvailable,
            doProperties,
            propertiesAvailable,
        });
    }

    updatePreflightStatus({ready, haveDevices, haveFolder, doWifi, doApp, doPost,
        postCmd, doSetup, setupAvailable, doProperties, propertiesAvailable, step1ok, useCache});
    syncDeviceOverrides({doWifi, doProperties, doPost});
    updateCommandStepState(doPost, postCmd);
    updateRunStepState();
}

function syncDeviceOverrides({doWifi, doProperties, doPost}) {
    const enabled = {
        change_password: doWifi,
        push_properties: doProperties,
        post_update: doPost,
    };
    for (const card of state.cards.values()) {
        for (const input of card.skipInputs) {
            input.closest("label").hidden = !enabled[input.dataset.stage];
        }
    }
}

function updateCommandStepState(enabled, commandText) {
    const step = $("#step-commands");
    const stateEl = $("#step-commands-state");
    const commandCount = commandText.split("\n").filter((line) => line.trim()).length;
    step.dataset.status = !enabled ? "skipped" : commandCount ? "configured" : "warning";
    stateEl.textContent = !enabled
        ? "off"
        : commandCount
            ? `${commandCount} command${commandCount === 1 ? "" : "s"}`
            : "command required";
    $("#post-update-hint").textContent = !enabled
        ? "Off: no custom commands will run."
        : commandCount
            ? `${commandCount} command${commandCount === 1 ? "" : "s"} will run after setup.`
            : "Add at least one command or turn this step off.";
}

function updatePreflightStatus({ready, haveDevices, haveFolder, doWifi, doApp, doPost,
    postCmd, doSetup, setupAvailable, doProperties, propertiesAvailable, step1ok, useCache}) {
    const issues = [];
    if (!state.settingsLoaded) issues.push("settings did not load");
    if (!haveDevices) issues.push("connect at least one board over USB-C");
    if (doWifi && !step1ok) issues.push("save the WiFi name and password in Section 1");
    if (doApp && !haveFolder) issues.push("choose an app folder in Section 2 or turn deployment off");
    if (doPost && !postCmd) issues.push("add commands in Section 3 or turn commands off");
    if (doSetup && !setupAvailable) issues.push("unoq-setup.sh is missing");
    if (doProperties && !propertiesAvailable) issues.push("properties.msgpack is missing");
    if (useCache && state.cacheIntegrity === "invalid") issues.push("prepare the damaged cache again or turn cache restoration off");
    const el = $("#preflight-status");
    el.dataset.status = ready ? "ready" : "waiting";
    el.replaceChildren();
    if (ready) {
        el.textContent = `${state.devices.length} board${state.devices.length === 1 ? "" : "s"} ready for the recommended setup.`;
        return;
    }
    const heading = document.createElement("strong");
    heading.textContent = "Before setup:";
    const list = document.createElement("ul");
    for (const issue of issues) {
        const item = document.createElement("li");
        item.textContent = issue;
        list.appendChild(item);
    }
    el.append(heading, list);
}

function missingFor({
    haveDevices,
    step1ok,
    step2ok,
    idle,
    needsStep1Inputs,
    needsStep2Inputs,
    needsPostCommand,
    postCmd,
    doSetup,
    setupAvailable,
    doProperties,
    propertiesAvailable,
}) {
    if (!idle) return "A run is in progress";
    const missing = [];
    if (!haveDevices) missing.push("connect at least one UNO Q");
    if (needsStep1Inputs && !step1ok) missing.push("configure WiFi or uncheck Step 1 in Run");
    if (needsStep2Inputs && !step2ok) missing.push("choose a folder in Section 2 or turn deployment off");
    if (needsPostCommand && !postCmd) missing.push("add a command in Step 3 or turn that step off");
    if (doSetup && !setupAvailable) missing.push("restore unoq-setup.sh to the project root");
    if (doProperties && !propertiesAvailable) missing.push("restore properties.msgpack or select an app folder containing it");
    if (state.cacheIntegrity === "invalid") missing.push("prepare the cache again");
    if (!state.settingsLoaded) missing.push("reload settings");
    return missing.length ? "Needed: " + missing.join("; ") : "";
}

function updateRunStepState(override) {
    const step = $("#step-run");
    const stateEl = $("#step-run-state");
    if (override) {
        stateEl.textContent = override;
        step.dataset.status = "warning";
        return;
    }
    const n = state.devices.length;
    if (state.runId !== null) {
        step.dataset.status = "ready";
        stateEl.textContent = `running on ${n} board${n === 1 ? "" : "s"}`;
        return;
    }
    if (n === 0) {
        step.dataset.status = "pending";
        stateEl.textContent = "no boards detected";
        return;
    }
    const doWifi = !!$("#run-step-wifi")?.checked;
    const doApp = !!$("#run-step-app")?.checked;
    const doProperties = !!$("#run-step-properties")?.checked;
    const doSetup = !!$("#run-step-setup")?.checked;
    const doPrune = !!$("#run-step-prune")?.checked;
    const doPost = !!$("#run-step-post-update")?.checked;
    const ready = (!doWifi || state.wifiOk);
    step.dataset.status = ready ? "ready" : "pending";
    const selected = [];
    if (doWifi) selected.push("WiFi");
    if (doProperties) selected.push("properties");
    if (doSetup) selected.push("setup");
    if (doPrune) selected.push("prune");
    if (doApp) selected.push("app folder");
    if (doPost) selected.push("commands");
    if (selected.length === 0) selected.push("no-op");
    stateEl.textContent = `${n} board${n === 1 ? "" : "s"} · ${selected.join(", ")}`;
}

// ---------- runs ----------

async function startRun(targetSerial = null, warmCache = false) {
    if (state.devices.length === 0) return;
    state.runFinalStatusText = null;

    const doWifi = !!$("#run-step-wifi")?.checked;
    const doApp = !!$("#run-step-app")?.checked;
    const doProperties = !!$("#run-step-properties")?.checked;
    const doSetup = !!$("#run-step-setup")?.checked;
    const doPostUpdate = !!$("#run-step-post-update")?.checked;
    const doPrune = !!$("#run-step-prune")?.checked;
    const doWarmApp = !!$("#run-step-warm-app")?.checked;
    const usePackageCache = !!$("#run-use-package-cache")?.checked;

    if (warmCache && doWarmApp && !state.upload) {
        $("#run-status").textContent = "Choose an app folder before preparing it on the cache board.";
        return;
    }

    if (warmCache && !window.confirm(
        `Prepare and save the shared cache from board ${targetSerial}?\n\nOnly this board will run. It will perform the enabled setup actions, prepare ${selectedExampleIds().length} selected example${selectedExampleIds().length === 1 ? "" : "s"}${doWarmApp ? " and the uploaded app" : ""}, then verify the files saved for later boards.`,
    )) return;

    const baseSkip = [];
    if (!doWifi) baseSkip.push(...SKIP_STEP_WIFI_STAGES);
    if (!doApp) baseSkip.push("push_app");
    if (!doProperties) baseSkip.push("push_properties");
    if (!doSetup) baseSkip.push("push_setup_script", "chmod_script", "run_setup");
    if (!doPrune) baseSkip.push("prune_docker_images");
    if (!usePackageCache) baseSkip.push("restore_workshop_cache");
    if (!warmCache || !doWarmApp) baseSkip.push("prepare_uploaded_app");
    if (!doPostUpdate) baseSkip.push("post_update");

    const targetDevices = targetSerial
        ? state.devices.filter((device) => device.serial === targetSerial)
        : state.devices;
    const devices = targetDevices.map((d) => {
        const userSkip = collectSkip(d.serial);
        const skip = Array.from(new Set([...baseSkip, ...userSkip]));
        return { serial: d.serial, skip_stages: skip };
    });

    const postUpdateCmd = doPostUpdate
        ? (($("#post-update-cmd").value || "").trim())
        : "";
    const pruneDockerBeforePostUpdate = doPrune;
    const sendUpload = state.upload && (
        doApp
        || doProperties
        || (warmCache && doWarmApp)
    );

    if (!warmCache) {
        const actions = [];
        if (doWifi) actions.push("configure WiFi and device password");
        if (doProperties) actions.push("complete App Lab onboarding");
        if (doSetup) actions.push("update system and App Lab software");
        if (doApp) actions.push(`copy ${state.upload.folder_name}`);
        if (doPrune) actions.push("remove unused Docker data");
        if (doPostUpdate) actions.push("run custom commands");
        const prompt = `Set up ${devices.length} board${devices.length === 1 ? "" : "s"}?\n\n${actions.map((action) => `• ${action}`).join("\n")}\n\nAll connected boards start together.`;
        if (!window.confirm(prompt)) return;
    }

    if (targetSerial) resetCard(targetSerial);
    else resetAllCards();

    const r = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            upload_id: sendUpload ? state.upload.upload_id : null,
            devices,
            post_update_cmd: postUpdateCmd || null,
            prune_docker_before_post_update: pruneDockerBeforePostUpdate,
            use_package_cache: usePackageCache,
            warm_cache: warmCache,
            prepare_uploaded_app: warmCache && doWarmApp,
            example_apps: warmCache ? selectedExampleIds() : [],
        }),
    });
    if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        $("#run-status").textContent = `start failed: ${j.detail || r.status}`;
        return;
    }
    const j = await r.json();
    state.runId = j.run_id;
    $("#run-status").textContent = warmCache
        ? `Preparing and saving cache from ${targetSerial}…`
        : `Run ${j.run_id} in progress…`;
    updateStartButtons();
    openWs(j.run_id);
}

function collectSkip(serial) {
    const card = state.cards.get(serial);
    if (!card) return [];
    return card.skipInputs.filter((i) => i.checked).map((i) => i.dataset.stage);
}

async function retryDevice(serial) {
    const runId = state.cards.get(serial)?.runId || state.runId;
    if (!runId) return;
    const skip = collectSkip(serial);
    resetCard(serial);
    const r = await fetch(`/api/runs/${runId}/devices/${serial}/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skip_stages: skip }),
    });
    if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        appendLog(serial, `retry failed: ${j.detail || r.status}`, "err");
        return;
    }
    state.runId = runId;
    state.runFinalStatusText = null;
    updateStartButtons();
    openWs(runId);
}

async function identifyDevice(serial) {
    const c = state.cards.get(serial);
    if (!c) return;
    const btn = c.identifyBtn;
    const prevText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Blinking…";
    try {
        const r = await fetch(`/api/devices/${encodeURIComponent(serial)}/identify`, {
            method: "POST",
        });
        if (!r.ok) {
            const j = await r.json().catch(() => ({}));
            appendLog(serial, `identify failed: ${j.detail || r.status}`, "err");
        }
    } catch (err) {
        appendLog(serial, `identify error: ${err}`, "err");
    } finally {
        btn.disabled = false;
        btn.textContent = prevText;
    }
}

async function wifiCheckDevice(serial) {
    const c = state.cards.get(serial);
    if (!c) return;
    const btn = c.wifiCheckBtn;
    const prevText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Checking…";
    setWifiBadge(c, "unknown", "checking...");
    appendLog(serial, "--- wifi check started ---", "info");
    try {
        const r = await fetch(`/api/devices/${encodeURIComponent(serial)}/wifi-check`, {
            method: "POST",
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
            appendLog(serial, `wifi check failed: ${j.detail || r.status}`, "err");
            return;
        }

        const out = String(j.output || "").trim();
        if (out) {
            for (const line of out.split("\n")) {
                appendLog(serial, line, "info");
            }
        }
        appendLog(
            serial,
            j.ok
                ? "--- wifi check passed ---"
                : `--- wifi check failed (exit ${j.exit_code}) ---`,
            j.ok ? "info" : "err",
        );
        if (j.ok) {
            setWifiBadge(c, "pass", "connectivity check passed");
        } else {
            setWifiBadge(c, "fail", `connectivity check failed (exit ${j.exit_code})`);
        }
    } catch (err) {
        setWifiBadge(c, "fail", "connectivity check error");
        appendLog(serial, `wifi check error: ${err}`, "err");
    } finally {
        btn.disabled = false;
        btn.textContent = prevText;
    }
}

async function wifiCheckAllDevices() {
    const btn = $("#wifi-check-all-btn");
    if (!btn) return;
    const serials = state.devices.map((d) => d.serial).filter((s) => state.cards.has(s));
    if (serials.length === 0) {
        $("#run-status").textContent = "No connected boards to check";
        return;
    }

    const prevText = btn.textContent;
    btn.disabled = true;
    btn.textContent = `Checking ${serials.length} board${serials.length === 1 ? "" : "s"}…`;

    const results = await Promise.allSettled(serials.map((serial) => wifiCheckDevice(serial)));
    const passed = serials.filter((serial) => {
        const c = state.cards.get(serial);
        return c?.wifiBadgeEl?.dataset?.status === "pass";
    }).length;
    const failed = serials.length - passed;

    const rejected = results.filter((r) => r.status === "rejected").length;
    $("#run-status").textContent = rejected > 0
        ? `WiFi checks complete: ${passed} passed, ${failed} failed (${rejected} request error${rejected === 1 ? "" : "s"})`
        : `WiFi checks complete: ${passed} passed, ${failed} failed`;

    btn.disabled = false;
    btn.textContent = prevText;
}

// ---------- WebSocket ----------

function openWs(runId) {
    if (state.ws) {
        state.ws.onclose = null;
        state.ws.onerror = null;
        state.ws.close();
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/runs/${runId}`);
    state.ws = ws;
    ws.onmessage = (e) => {
        let ev;
        try { ev = JSON.parse(e.data); } catch { return; }
        handleEvent(ev);
    };
    ws.onclose = () => {
        if (state.ws !== ws) return;
        state.ws = null;
        if (state.runId !== null && !state.runFinalStatusText) {
            $("#run-status").textContent =
                `run ${runId} disconnected before completion`; 
        } else if (state.runFinalStatusText) {
            $("#run-status").textContent = state.runFinalStatusText;
        }
        state.runId = null;
        for (const serial of state.cards.keys()) stopLiveTimer(serial);
        updateStartButtons();
    };
    ws.onerror = () => console.log("ws error");
}

async function diagnoseWithCopilot(serial) {
    const c = state.cards.get(serial);
    if (!c) return;

    const allLogs = c.logEl.innerText.trim();
    const recentLogs = allLogs.slice(-12000) || "No device log output was captured.";
    const failureReason = c.failureEl.textContent.trim() || "No failure reason was reported.";
    const prompt = [
        "Diagnose and fix this failed Arduino UNO Q setup run.",
        "Inspect the current uno-q-flasher workspace, identify the root cause from the logs, implement the smallest robust fix, validate it locally, and retry only this board when hardware verification is necessary. Do not run a fleet-wide update.",
        "",
        `Board ID: ${serial}`,
        `Run ID: ${c.runId || state.runId || "unknown"}`,
        `Failure reason: ${failureReason}`,
        "",
        "Recent board logs:",
        "```text",
        recentLogs,
        "```",
    ].join("\n");

    c.diagnoseBtn.disabled = true;
    c.diagnoseBtn.textContent = "Opening Copilot...";
    try {
        const response = await fetch("/api/copilot/diagnose", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ prompt }),
        });
        if (response.ok) return;
        const body = await response.json().catch(() => ({}));
        try { await navigator.clipboard.writeText(prompt); } catch (_) { /* no-op */ }
        appendLog(serial, `Could not open Copilot: ${body.detail || response.status}. Prompt copied to clipboard.`, "err");
    } catch (error) {
        try { await navigator.clipboard.writeText(prompt); } catch (_) { /* no-op */ }
        appendLog(serial, `Could not open Copilot: ${error.message}. Prompt copied to clipboard.`, "err");
    } finally {
        c.diagnoseBtn.disabled = false;
        window.setTimeout(() => {
            c.diagnoseBtn.textContent = "Diagnose and fix with Copilot";
        }, 1500);
    }
}

function handleEvent(ev) {
    switch (ev.type) {
        case "device_started": {
            const c = state.cards.get(ev.device);
            if (!c) return;
            c.runId = state.runId;
            c.badgeEl.dataset.status = "running";
            c.badgeEl.textContent = "running";
            c.retryBtn.hidden = true;
            c.diagnoseBtn.hidden = true;
            c.failureEl.hidden = true;
            c.summaryEl.hidden = false;
            c.summary = makeSummaryState();
            c.summary.overallStatus = "running";
            c.summary.overallText = "running";
            c.summary.phase = "starting update";
            renderSummary(c);
            startLiveTimer(ev.device);
            break;
        }
        case "device_retry": {
            const c = state.cards.get(ev.device);
            if (!c) return;
            // A new attempt is about to begin — clear the previous attempt's
            // FAILED summary/reason so the card doesn't look done. Keep the
            // live timer running (elapsed accumulates across attempts).
            c.badgeEl.dataset.status = "running";
            c.badgeEl.textContent = `retry ${ev.attempt}/${ev.max_attempts}`;
            c.diagnoseBtn.hidden = true;
            c.failureEl.hidden = true;
            c.summaryEl.hidden = false;
            c.summary = makeSummaryState();
            c.summary.overallStatus = "running";
            c.summary.overallText = "running";
            c.summary.phase = `retrying (${ev.attempt}/${ev.max_attempts})`;
            c.progressEl.style.width = "0%";
            c.stageEl.textContent = "retrying…";
            renderSummary(c);
            appendLog(
                ev.device,
                `--- retry attempt ${ev.attempt}/${ev.max_attempts}${ev.reason ? " · " + ev.reason : ""} ---`,
                "info",
            );
            break;
        }
        case "stage": {
            const c = state.cards.get(ev.device);
            if (!c) return;
            const idx = STAGES.indexOf(ev.stage);
            const pct = ev.status === "completed" || ev.status === "skipped"
                ? ((idx + 1) / STAGES.length) * 100
                : (idx / STAGES.length) * 100;
            c.progressEl.style.width = `${pct}%`;
            c.stageEl.textContent = `${ev.stage} · ${ev.status}`;
            updateSummaryFromStage(c, ev.stage, ev.status);
            renderSummary(c);
            appendLog(ev.device, `[${ev.stage}] ${ev.status}`, "stage");
            break;
        }
        case "log": {
            const cls = ev.stream === "stderr" ? "err" : "";
            const prefix = ev.stage ? `[${ev.stage}] ` : "";
            const c = state.cards.get(ev.device);
            if (c) {
                collectSummaryWarnings(c, ev);
                renderSummary(c);
            }
            appendLog(ev.device, prefix + ev.line, cls);
            break;
        }
        case "setup_summary": {
            const c = state.cards.get(ev.device);
            if (!c) return;
            c.summaryEl.hidden = false;
            c.summary.setupStatus = ev.status;
            c.summary.setupTime = ev.elapsed_seconds;
            c.summary.setupErrors = Array.isArray(ev.errors) ? ev.errors : [];
            c.summary.phase = ev.status === "SUCCESS"
                ? "setup script succeeded, continuing"
                : "setup script reported failure";
            renderSummary(c);
            break;
        }
        case "device_finished": {
            const c = state.cards.get(ev.device);
            if (!c) return;
            stopLiveTimer(ev.device);
            c.badgeEl.dataset.status = ev.result;
            c.badgeEl.textContent = ev.result;
            c.stageEl.textContent = ev.result;
            if (ev.elapsed_seconds != null) {
                const t = Math.round(ev.elapsed_seconds);
                c.elapsedEl.textContent = formatElapsed(t);
                c.elapsedEl.dataset.state = "final";
                c.elapsedEl.hidden = false;
            }
            if (ev.failure_reason) {
                c.failureEl.textContent = ev.failure_reason;
                c.failureEl.hidden = false;
            }
            if (ev.result === "failed") {
                c.retryBtn.hidden = false;
                c.diagnoseBtn.hidden = false;
            } else {
                c.progressEl.style.width = "100%";
            }
            c.summaryEl.hidden = false;
            c.summary.overallStatus = ev.result;
            c.summary.overallText = ev.result === "success" ? "complete" : "failed";
            c.summary.phase = ev.result === "success"
                ? "all stages finished"
                : `failed${ev.failure_reason ? ": " + ev.failure_reason : ""}`;
            if (c.summary.postUpdate.status === "pending") {
                c.summary.postUpdate = {
                    status: "skipped",
                    text: "not run",
                };
            }
            renderSummary(c);
            break;
        }
        case "run_finished": {
            state.runFinalStatusText =
                `run complete · ${ev.successful.length} ok, ${ev.failed.length} failed`;
            $("#run-status").textContent = state.runFinalStatusText;
            state.runId = null;
            updateStartButtons();
            verifyWorkshopCache();
            break;
        }
    }
}

function makeSummaryState() {
    return {
        overallStatus: "running",
        overallText: "running",
        phase: "waiting for first stage",
        setupStatus: "—",
        setupTime: null,
        setupErrors: [],
        postUpdate: {
            status: "pending",
            text: "pending",
        },
        warnings: [],
    };
}

function updateSummaryFromStage(c, stage, status) {
    if (!c.summary) c.summary = makeSummaryState();

    if (stage === "run_setup" && status === "started") {
        c.summary.phase = "running setup script";
    } else if (stage === "run_setup" && (status === "completed" || status === "skipped")) {
        c.summary.phase = "setup script done, finalizing";
    } else if (stage === "run_setup" && status === "failed") {
        c.summary.phase = "setup script failed";
    }

    if (stage === "post_update") {
        if (status === "started") {
            c.summary.postUpdate = { status: "running", text: "running" };
            c.summary.phase = "running post-update";
        } else if (status === "completed") {
            c.summary.postUpdate = { status: "completed", text: "completed" };
            c.summary.phase = "post-update complete";
        } else if (status === "skipped") {
            c.summary.postUpdate = { status: "skipped", text: "skipped" };
        } else if (status === "failed") {
            c.summary.postUpdate = { status: "failed", text: "failed (non-fatal)" };
            if (!c.summary.warnings.includes("post-update command failed (setup can still pass)")) {
                c.summary.warnings.push("post-update command failed (setup can still pass)");
            }
            c.summary.phase = "post-update failed (non-fatal)";
        }
    }

    if (stage === "prune_docker_images") {
        if (status === "started") {
            c.summary.phase = "pruning old docker images";
        } else if (status === "completed") {
            c.summary.phase = "docker prune complete";
        } else if (status === "failed") {
            if (!c.summary.warnings.includes("docker prune failed (continuing)")) {
                c.summary.warnings.push("docker prune failed (continuing)");
            }
            c.summary.phase = "docker prune failed (continuing)";
        }
    }
}

function collectSummaryWarnings(c, ev) {
    if (!ev || !ev.line || ev.stage !== "change_password") return;
    if (!c.summary) c.summary = makeSummaryState();
    const line = String(ev.line).toLowerCase();
    if (
        line.includes("authentication token manipulation error") ||
        line.includes("password unchanged")
    ) {
        const warning = "password change did not complete";
        if (!c.summary.warnings.includes(warning)) c.summary.warnings.push(warning);
    }
}

function renderSummary(c) {
    if (!c || !c.summaryEl) return;
    if (!c.summary) c.summary = makeSummaryState();

    const overallEl = c.summaryEl.querySelector(".summary-overall");
    const phaseEl = c.summaryEl.querySelector(".summary-phase");
    const setupEl = c.summaryEl.querySelector(".summary-status");
    const timeEl = c.summaryEl.querySelector(".summary-time");
    const postEl = c.summaryEl.querySelector(".summary-post-update");
    const warnUl = c.summaryEl.querySelector(".summary-warnings");
    const errUl = c.summaryEl.querySelector(".summary-errors");

    overallEl.dataset.status = c.summary.overallStatus;
    overallEl.textContent = c.summary.overallText;

    phaseEl.textContent = c.summary.phase;

    setupEl.dataset.status = c.summary.setupStatus;
    setupEl.textContent = c.summary.setupStatus;

    const t = c.summary.setupTime;
    timeEl.textContent =
        t == null ? "—" : `${Math.floor(t / 60)}m ${String(t % 60).padStart(2, "0")}s`;

    postEl.dataset.status = c.summary.postUpdate.status;
    postEl.textContent = c.summary.postUpdate.text;

    warnUl.innerHTML = "";
    for (const warning of c.summary.warnings) {
        const li = document.createElement("li");
        li.textContent = `warning: ${warning}`;
        warnUl.appendChild(li);
    }

    errUl.innerHTML = "";
    for (const err of c.summary.setupErrors || []) {
        const li = document.createElement("li");
        li.textContent = err;
        errUl.appendChild(li);
    }
}

// ---------- log helpers ----------

function appendLog(serial, line, cls = "") {
    const c = state.cards.get(serial);
    if (!c) return;
    const span = document.createElement("span");
    if (cls === "stage") span.className = "log-stage";
    else if (cls === "err") span.className = "log-err";
    else if (cls === "info") span.className = "log-info";
    span.textContent = line + "\n";
    c.logEl.appendChild(span);
    if (c.logFollowState?.follow !== false) {
        c.logEl.scrollTop = c.logEl.scrollHeight;
    }
}

function resetAllCards() {
    for (const [serial] of state.cards) resetCard(serial);
}

function resetCard(serial) {
    const c = state.cards.get(serial);
    if (!c) return;
    stopLiveTimer(serial);
    c.badgeEl.dataset.status = "idle";
    c.badgeEl.textContent = "idle";
    c.progressEl.style.width = "0%";
    c.stageEl.textContent = "—";
    c.logEl.innerHTML = "";
    if (c.logFollowState) c.logFollowState.follow = true;
    c.retryBtn.hidden = true;
    c.diagnoseBtn.hidden = true;
    c.failureEl.hidden = true;
    c.summaryEl.hidden = true;
    c.summary = makeSummaryState();
    if (c.wifiBadgeEl) {
        setWifiBadge(c, "unknown", "No WiFi check yet");
    }
    c.elapsedEl.hidden = true;
    c.elapsedEl.dataset.state = "";
}

function setWifiBadge(card, status, title) {
    if (!card || !card.wifiBadgeEl) return;
    card.wifiBadgeEl.dataset.status = status;
    card.wifiBadgeEl.title = title;
    if (status === "pass") {
        card.wifiBadgeEl.textContent = "WiFi OK";
    } else if (status === "fail") {
        card.wifiBadgeEl.textContent = "WiFi Fail";
    } else {
        card.wifiBadgeEl.textContent = "WiFi ?";
    }
}

// ---------- per-device live elapsed timer ----------

function formatElapsed(seconds) {
    const t = Math.max(0, Math.floor(seconds));
    return `${Math.floor(t / 60)}m ${String(t % 60).padStart(2, "0")}s`;
}

function startLiveTimer(serial) {
    const c = state.cards.get(serial);
    if (!c) return;
    stopLiveTimer(serial);
    const startedAt = performance.now();
    c.elapsedEl.hidden = false;
    c.elapsedEl.dataset.state = "live";
    c.elapsedEl.textContent = formatElapsed(0);
    const timerId = setInterval(() => {
        const secs = (performance.now() - startedAt) / 1000;
        c.elapsedEl.textContent = formatElapsed(secs);
    }, 1000);
    c.timerId = timerId;
}

function stopLiveTimer(serial) {
    const c = state.cards.get(serial);
    if (!c || !c.timerId) return;
    clearInterval(c.timerId);
    c.timerId = null;
}

init();
