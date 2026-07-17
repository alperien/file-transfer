let currentPath = "";
let remoteFiles = [];
let selectedFiles = [];
let refreshInterval = null;
let browseAbort = null;
let _localLogs = [];

async function api(url, method = "GET", body = null, signal = null) {
    const opts = { method };
    if (body) {
        opts.headers = { "Content-Type": "application/json" };
        opts.body = JSON.stringify(body);
    }
    if (signal) opts.signal = signal;
    try {
        const res = await fetch(url, opts);
        const text = await res.text();
        let data;
        try { data = JSON.parse(text); }
        catch (e) { data = { ok: false, message: "Invalid JSON response" }; }
        if (data === null || typeof data !== "object") {
            data = { ok: false, message: "Unexpected response" };
        }
        if (!res.ok) {
            data.ok = false;
            if (!data.message) data.message = "HTTP " + res.status + ": " + res.statusText;
        }
        return data;
    } catch (e) {
        return { ok: false, message: "Network error: " + e.message };
    }
}

async function loadConfig() {
    try {
        const cfg = await api("/api/config");
        if (!cfg || !cfg.ssh) return;
        if (cfg.ssh) {
            document.getElementById("ssh-host").value = cfg.ssh.host || "";
            document.getElementById("ssh-port").value = cfg.ssh.port || 22;
            document.getElementById("ssh-user").value = cfg.ssh.user || "";
            document.getElementById("ssh-key").value = cfg.ssh.key_path || "";
        }
        if (cfg.paths) {
            document.getElementById("path-source").value = cfg.paths.source || "";
            document.getElementById("path-dest").value = cfg.paths.destination || "";
        }
    } catch (e) {
        addLog("ERROR", "load config failed: " + e.message);
    }
}

async function saveConfig() {
    const portVal = parseInt(document.getElementById("ssh-port").value, 10);
    const payload = {
        ssh: {
            host: document.getElementById("ssh-host").value,
            port: isNaN(portVal) ? 22 : portVal,
            user: document.getElementById("ssh-user").value,
            key_path: document.getElementById("ssh-key").value,
        },
        paths: {
            source: document.getElementById("path-source").value,
            destination: document.getElementById("path-dest").value,
        },
    };
    const pw = document.getElementById("ssh-password").value;
    if (pw) payload.ssh.password = pw;
    const res = await api("/api/config", "POST", payload);
    if (res.ok) addLog("INFO", "config saved");
    else addLog("ERROR", "save failed: " + res.message);
}

function setConnectedState(connected) {
    document.getElementById("btn-disconnect").disabled = !connected;
    document.getElementById("btn-add").disabled = !connected;
    document.getElementById("btn-select-all").disabled = !connected;
    document.getElementById("btn-deselect-all").disabled = !connected;
    document.getElementById("btn-start").disabled = !connected;
}

async function connect() {
    const btn = document.getElementById("btn-connect");
    btn.disabled = true;
    btn.textContent = "connecting...";
    try {
        const res = await api("/api/connect", "POST");
        if (res.ok) {
            addLog("INFO", "connected");
            currentPath = document.getElementById("path-source").value;
            await browseFiles(currentPath);
            setConnectedState(true);
            startAutoRefresh();
        } else {
            addLog("ERROR", "connect failed: " + res.message);
        }
    } catch (e) {
        addLog("ERROR", "connect error: " + e.message);
    }
    btn.disabled = false;
    btn.textContent = "connect";
}

async function disconnect() {
    if (browseAbort) { browseAbort.abort(); browseAbort = null; }
    const status = await api("/api/transfer/status");
    if (status && status.running) {
        const stopRes = await stopTransfer();
        if (!stopRes) addLog("WARNING", "stop transfer failed during disconnect");
    }
    const res = await api("/api/disconnect", "POST");
    stopAutoRefresh();
    setConnectedState(false);
    selectedFiles = [];
    remoteFiles = [];
    currentPath = "";
    document.getElementById("file-list").innerHTML = '<div class="dim" style="padding:10px;text-align:center;">disconnected</div>';
    document.getElementById("queue-list").innerHTML = '<div class="dim" style="padding:10px;text-align:center;">empty</div>';
    if (res.ok) addLog("INFO", "disconnected");
    else addLog("ERROR", "disconnect failed: " + res.message);
}

async function browseFiles(path) {
    if (browseAbort) browseAbort.abort();
    browseAbort = new AbortController();
    currentPath = path;
    selectedFiles = [];
    updateBreadcrumb(path);
    try {
        const result = await api("/api/files?path=" + encodeURIComponent(path), "GET", null, browseAbort.signal);
        if (!result || !result.ok) {
            addLog("ERROR", "browse failed: " + (result ? result.message : "no response"));
            return;
        }
        const files = result.files || result;
        if (!Array.isArray(files)) {
            addLog("ERROR", "invalid response");
            return;
        }
        remoteFiles = files;
        renderFileList(files);
    } catch (e) {
        if (e.name !== "AbortError") addLog("ERROR", "browse error: " + e.message);
    }
}

function updateBreadcrumb(path) {
    const bc = document.getElementById("breadcrumb");
    bc.innerHTML = "";
    const parts = path.split("/").filter(Boolean);
    const root = document.createElement("a");
    root.textContent = "/";
    root.href = "#";
    root.addEventListener("click", (e) => { e.preventDefault(); browseFiles("/"); });
    bc.appendChild(root);
    let accumulated = "";
    for (const part of parts) {
        accumulated += "/" + part;
        const sep = document.createTextNode("/");
        bc.appendChild(sep);
        const link = document.createElement("a");
        link.textContent = part;
        link.href = "#";
        const p = accumulated;
        link.addEventListener("click", (e) => { e.preventDefault(); browseFiles(p); });
        bc.appendChild(link);
    }
}

let _clickTimer = null;
let _clickTarget = null;

function renderFileList(files) {
    const list = document.getElementById("file-list");
    if (files.length === 0) {
        list.innerHTML = '<div class="dim" style="padding:10px;text-align:center;">empty</div>';
        return;
    }
    list.innerHTML = files.map((f, i) => {
        const icon = f.is_dir ? "[d]" : "[f]";
        const size = f.is_dir ? "" : formatSize(f.size);
        const checked = selectedFiles.some(s => s.path === f.path) ? "checked" : "";
        return '<div class="file-item" data-index="' + i + '">' +
            '<input type="checkbox" class="checkbox" ' + checked + ' data-index="' + i + '">' +
            '<span class="icon">' + icon + "</span>" +
            '<span class="name">' + escapeHtml(f.name) + "</span>" +
            '<span class="size">' + size + "</span></div>";
    }).join("");

    list.querySelectorAll(".file-item").forEach(el => {
        el.addEventListener("click", (e) => {
            if (e.target.type === "checkbox") return;
            const idx = parseInt(el.dataset.index);
            const file = remoteFiles[idx];
            if (file && file.is_dir) {
                if (_clickTimer && _clickTarget === idx) { clearTimeout(_clickTimer); _clickTimer = null; _clickTarget = null; return; }
                if (_clickTimer) { clearTimeout(_clickTimer); }
                _clickTarget = idx;
                _clickTimer = setTimeout(() => { _clickTimer = null; _clickTarget = null; toggleFile(idx); }, 250);
            } else {
                if (_clickTimer) { clearTimeout(_clickTimer); _clickTimer = null; _clickTarget = null; }
                toggleFile(idx);
            }
        });
        el.addEventListener("dblclick", (e) => {
            const idx = parseInt(el.dataset.index);
            const file = remoteFiles[idx];
            if (file && file.is_dir) {
                if (_clickTimer) { clearTimeout(_clickTimer); _clickTimer = null; _clickTarget = null; }
                browseFiles(file.path);
            }
        });
    });
    list.querySelectorAll('input[type="checkbox"]').forEach(el => {
        el.addEventListener("click", (e) => {
            e.stopPropagation();
            if (_clickTimer) { clearTimeout(_clickTimer); _clickTimer = null; _clickTarget = null; }
            toggleFile(parseInt(el.dataset.index));
        });
    });
}

function toggleFile(index) {
    const file = remoteFiles[index];
    if (!file) return;
    const idx = selectedFiles.findIndex(s => s.path === file.path);
    if (idx >= 0) selectedFiles.splice(idx, 1);
    else selectedFiles.push(file);
    const checkbox = document.querySelector('.file-item[data-index="' + index + '"] input[type="checkbox"]');
    if (checkbox) checkbox.checked = idx < 0;
}

function selectAll() {
    selectedFiles = remoteFiles.slice();
    renderFileList(remoteFiles);
}

function deselectAll() {
    selectedFiles = [];
    renderFileList(remoteFiles);
}

async function addSelectedToQueue() {
    if (selectedFiles.length === 0) {
        addLog("WARNING", "no files selected");
        return true;
    }
    const res = await api("/api/queue", "POST", { files: selectedFiles });
    if (res.ok) {
        addLog("INFO", "queued " + res.added + " files");
        selectedFiles = [];
        renderFileList(remoteFiles);
        await refreshQueue();
        return true;
    } else {
        addLog("ERROR", "queue failed: " + res.message);
        return false;
    }
}

async function startTransfer() {
    if (selectedFiles.length > 0) {
        const ok = await addSelectedToQueue();
        if (!ok) return;
    }
    const queueCheck = await api("/api/queue/files");
    const queueFiles = queueCheck.ok && Array.isArray(queueCheck.files) ? queueCheck.files : [];
    const pendingFiles = queueFiles.filter(f => f.status === "pending" || f.status === "transferring");
    if (pendingFiles.length === 0) {
        addLog("WARNING", "no files in queue to transfer");
        return;
    }
    const res = await api("/api/transfer/start", "POST");
    if (res.ok) { addLog("INFO", "transfer started"); startAutoRefresh(); }
    else addLog("ERROR", "start failed: " + res.message);
}

async function pauseTransfer() {
    const res = await api("/api/transfer/pause", "POST");
    if (res.ok) addLog("INFO", "paused");
    else addLog("ERROR", "pause failed: " + res.message);
}

async function resumeTransfer() {
    const res = await api("/api/transfer/resume", "POST");
    if (res.ok) addLog("INFO", "resumed");
    else addLog("ERROR", "resume failed: " + res.message);
}

async function stopTransfer() {
    const res = await api("/api/transfer/stop", "POST");
    if (res.ok) { addLog("INFO", "stopped"); return true; }
    else { addLog("ERROR", "stop failed: " + res.message); return false; }
}

async function clearCompleted() {
    const queueCheck = await api("/api/queue/files");
    const queueFiles = queueCheck.ok && Array.isArray(queueCheck.files) ? queueCheck.files : [];
    const hasFailed = queueFiles.some(f => f.status === "failed" || f.status === "error");
    if (hasFailed) {
        addLog("WARNING", "clearing completed files (failed files also removed)");
    }
    const res = await api("/api/queue/clear", "POST");
    if (res.ok) await refreshQueue();
    else addLog("ERROR", "clear failed: " + res.message);
}

async function refreshQueue() {
    try {
        const result = await api("/api/queue/files");
        let files;
        if (result && result.ok && Array.isArray(result.files)) {
            files = result.files;
        } else {
            return;
        }
        const list = document.getElementById("queue-list");
        if (files.length === 0) {
            list.innerHTML = '<div class="dim" style="padding:10px;text-align:center;">empty</div>';
            return;
        }
        list.innerHTML = files.map(f => {
            const name = (f.remote_path || "").split("/").pop() || "unknown";
            const pct = f.size > 0 ? Math.min(100, Math.round((f.bytes_transferred / f.size) * 100)) : 0;
            const transferred = formatSize(f.bytes_transferred);
            const total = formatSize(f.size);
            const bar = f.status === "transferring"
                ? '<div class="progress-bar"><div class="progress-fill" style="width:' + pct + '%"></div></div>'
                : "";
            return '<div class="queue-item">' +
                '<span class="name">' + escapeHtml(name) + "</span>" +
                '<span class="meta">' + transferred + "/" + total + " " + pct + "%</span>" +
                '<span class="st st-' + escapeHtml(f.status) + '">' + escapeHtml(f.status) + "</span></div>" + bar;
        }).join("");
    } catch (e) {
        addLog("ERROR", "refresh queue failed: " + e.message);
    }
}

async function refreshStatus() {
    try {
        const s = await api("/api/transfer/status");
        if (!s || s.connected === undefined) return;

        const dot = document.getElementById("status-dot");
        const text = document.getElementById("status-text");

        if (s.connected) {
            dot.style.color = s.running ? "#00ff41" : "#ffaa00";
            text.textContent = s.running ? (s.paused ? "paused" : "transferring") : "connected";
            setConnectedState(true);
        } else {
            dot.style.color = "#ff0040";
            text.textContent = "disconnected";
            setConnectedState(false);
        }

        document.getElementById("status-speed").textContent = formatSpeed(s.speed);
        document.getElementById("status-files").textContent = (s.completed_files || 0) + "/" + (s.total_files || 0);
        document.getElementById("status-current").textContent = s.current_file ? s.current_file.remote_path.split("/").pop() : "-";

        const totalBytes = s.total_bytes || 0;
        const pct = totalBytes > 0 ? Math.min(100, Math.round(((s.transferred_bytes || 0) / totalBytes) * 100)) : 0;
        document.getElementById("progress-fill").style.width = pct + "%";
        document.getElementById("progress-bar").setAttribute("aria-valuenow", pct);

        document.getElementById("btn-pause").disabled = !s.running || s.paused;
        document.getElementById("btn-resume").disabled = !s.running || !s.paused;
        document.getElementById("btn-stop").disabled = !s.running;
    } catch (e) {
        addLog("ERROR", "refresh status failed: " + e.message);
    }
}

function parseLogTimestamp(ts) {
    if (!ts) return "--:--:--";
    if (ts.indexOf("T") >= 0) {
        const timePart = ts.split("T")[1];
        if (timePart) return timePart.split(".")[0];
    }
    if (ts.indexOf(" ") >= 0) {
        return ts.split(" ")[1];
    }
    return ts;
}

async function refreshLogs() {
    try {
        const logs = await api("/api/logs?limit=50");
        if (!logs || !Array.isArray(logs)) return;
        const el = document.getElementById("logs");
        const localHtml = _localLogs.map(l => {
            const lvl = (l.level || "I").charAt(0).toLowerCase();
            const cls = lvl === "e" ? "lvl-E" : lvl === "w" ? "lvl-W" : "lvl-I";
            return '<div class="log-entry"><span class="time">' + escapeHtml(l.time) +
                '</span> <span class="' + cls + '">' + escapeHtml(l.level) +
                "</span> " + escapeHtml(l.message) + "</div>";
        }).join("");
        const serverHtml = logs.map(l => {
            const t = parseLogTimestamp(l.timestamp);
            const lvl = (l.level || "I").charAt(0).toLowerCase();
            const cls = lvl === "e" ? "lvl-E" : lvl === "w" ? "lvl-W" : "lvl-I";
            return '<div class="log-entry"><span class="time">' + escapeHtml(t) +
                '</span> <span class="' + cls + '">' + escapeHtml(l.level || "I") +
                "</span> " + escapeHtml(l.message || "") + "</div>";
        }).join("");
        el.innerHTML = serverHtml + localHtml;
        el.scrollTop = el.scrollHeight;
    } catch (e) {
        addLog("ERROR", "refresh logs failed: " + e.message);
    }
}

function startAutoRefresh() {
    if (refreshInterval) return;
    const tick = async () => {
        try {
            await Promise.all([refreshStatus(), refreshQueue(), refreshLogs()]);
        } catch (e) {
            addLog("ERROR", "auto-refresh failed: " + e.message);
        }
        refreshInterval = setTimeout(tick, 1000);
    };
    refreshInterval = setTimeout(tick, 1000);
}

function stopAutoRefresh() {
    if (refreshInterval) { clearTimeout(refreshInterval); refreshInterval = null; }
}

function addLog(level, message) {
    const now = new Date().toTimeString().split(" ")[0];
    _localLogs.push({ time: now, level: level || "INFO", message: message });
    if (_localLogs.length > 50) _localLogs.shift();
    const el = document.getElementById("logs");
    if (!el) return;
    const lvl = (level || "I").charAt(0).toLowerCase();
    const cls = lvl === "e" ? "lvl-E" : lvl === "w" ? "lvl-W" : "lvl-I";
    el.insertAdjacentHTML("beforeend",
        '<div class="log-entry"><span class="time">' + now +
        '</span> <span class="' + cls + '">' + escapeHtml(level) +
        "</span> " + escapeHtml(message) + "</div>");
    el.scrollTop = el.scrollHeight;
}

function formatSize(bytes) {
    if (bytes === null || bytes === undefined || isNaN(bytes)) return "0 B";
    if (bytes === 0) return "0 B";
    if (bytes < 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    if (i < 0 || i >= units.length) return bytes + " B";
    return (bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0) + " " + units[i];
}

function formatSpeed(bps) {
    if (!bps || bps === 0) return "0 B/s";
    return formatSize(bps) + "/s";
}

function escapeHtml(text) {
    if (text === null || text === undefined) return "";
    return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

loadConfig();
