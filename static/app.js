(() => {
  const APP_VERSION = "v3";
  console.log("[HASP Messenger]", APP_VERSION, "loaded");
  const $ = (id) => document.getElementById(id);

  // ---------- State ----------
  // meInfo: /api/me — visible to every authenticated user. Drives the main
  //         UI (plate selector, preview, role badge).
  // brokerCfg: /api/config — admin-only. Only populated when Setup opens.
  let meInfo = { email: "", is_admin: false, is_lan: false, plates: [] };
  let brokerCfg = { plates: [] };
  let users = [];
  let currentPlateId = localStorage.getItem("hasp.currentPlateId") || null;
  let suppressPreviewBroadcast = false;
  let previewFocused = false;

  function currentPlate() {
    return meInfo.plates.find((p) => p.id === currentPlateId)
        || meInfo.plates[0]
        || null;
  }

  function setCurrentPlate(plateId) {
    currentPlateId = plateId;
    localStorage.setItem("hasp.currentPlateId", plateId);
    const p = currentPlate();
    $("preview-plate-name").textContent = p ? p.name : "—";
    $("snapshot-plate-name").textContent = p ? p.name : "—";
    applyPlateSize();
    refreshPlateStatusForCurrent();
    updatePreview();
    refreshSnapshot();
    // Templates are per-plate now — refresh the list when switching.
    loadTemplates();
  }

  // ---------- Spec ----------
  function readSpec() {
    return {
      text: $("f-text").value,
      x: +$("f-x").value || 0,
      y: +$("f-y").value || 0,
      w: +$("f-w").value || 0,
      h: +$("f-h").value || 0,
      text_font: +$("f-font").value,
      text_color: $("f-text-color").value.toUpperCase(),
      bg_color: $("f-bg-color").value.toUpperCase(),
      bg_opa: +$("f-bg-opa").value,
      align: $("f-align").value,
      mode: $("f-mode").value,
      pad_top: +$("f-pt").value || 0,
      pad_bottom: +$("f-pb").value || 0,
      pad_left: +$("f-pl").value || 0,
      pad_right: +$("f-pr").value || 0,
    };
  }

  function writeSpec(s) {
    if (!s) return;
    $("f-text").value = s.text ?? "";
    $("f-x").value = s.x ?? 0;
    $("f-y").value = s.y ?? 0;
    $("f-w").value = s.w ?? 0;
    $("f-h").value = s.h ?? 0;
    $("f-font").value = s.text_font ?? 48;
    $("f-text-color").value = (s.text_color ?? "#FFFFFF");
    $("f-bg-color").value = (s.bg_color ?? "#FF0000");
    $("f-bg-opa").value = s.bg_opa ?? 255;
    $("f-bg-opa-val").textContent = s.bg_opa ?? 255;
    $("f-align").value = s.align ?? "center";
    $("f-mode").value = s.mode ?? "break";
    $("f-pt").value = s.pad_top ?? 0;
    $("f-pb").value = s.pad_bottom ?? 0;
    $("f-pl").value = s.pad_left ?? 0;
    $("f-pr").value = s.pad_right ?? 0;
    updatePreview();
  }

  // OpenHASP's recolor parser requires the closing `#` to be followed by
  // whitespace (or end of string). If the next char is a regular character,
  // the parser appears to abort and drop the remainder of the text — the
  // plate renders only what came before. Mirror that behaviour so the
  // preview matches what actually shows up on the LCD.
  function renderHaspText(text, defaultColor) {
    if (!text) return "";
    const re = /#([0-9A-Fa-f]{6})\s([^#]*)#/g;
    let out = "";
    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      out += escapeHtml(text.slice(last, m.index));
      out += `<span style="color:#${m[1]}">${escapeHtml(m[2])}</span>`;
      last = re.lastIndex;
      const nextChar = text[last];
      if (nextChar !== undefined && nextChar !== " " && nextChar !== "\n") {
        // Malformed close — bail. Anything after is dropped (matches plate).
        return out;
      }
    }
    out += escapeHtml(text.slice(last));
    return out;
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
  }

  function fontToPx(n) {
    return Math.max(8, n);
  }

  function hexToRgba(hex, alpha255) {
    const m = /^#?([0-9a-fA-F]{6})$/.exec(hex || "");
    if (!m) return `rgba(0,0,0,${alpha255/255})`;
    const r = parseInt(m[1].slice(0,2),16);
    const g = parseInt(m[1].slice(2,4),16);
    const b = parseInt(m[1].slice(4,6),16);
    return `rgba(${r},${g},${b},${(alpha255/255).toFixed(3)})`;
  }

  function updatePreview() {
    const s = readSpec();
    const el = $("overlay");
    el.style.left = s.x + "px";
    el.style.top = s.y + "px";
    el.style.width = s.w + "px";
    el.style.height = s.h + "px";
    el.style.background = hexToRgba(s.bg_color, s.bg_opa);
    el.style.color = s.text_color;
    el.style.fontSize = fontToPx(s.text_font) + "px";
    el.style.lineHeight = "1";
    el.style.paddingTop = s.pad_top + "px";
    el.style.paddingBottom = s.pad_bottom + "px";
    el.style.paddingLeft = s.pad_left + "px";
    el.style.paddingRight = s.pad_right + "px";
    el.style.justifyContent = s.align === "left" ? "flex-start"
                            : s.align === "right" ? "flex-end" : "center";
    el.style.textAlign = s.align;
    el.style.whiteSpace = s.mode === "break" ? "pre-wrap" : "nowrap";

    // Mimic LVGL: a line is rendered when its top is within the content area.
    // Clip the inner div to `maxLines * lineHeight` so anything past that is
    // hidden without forcing an ellipsis (line-clamp would add "...").
    let innerStyle = "width:100%;";
    if (s.mode === "break") {
      const lh = fontToPx(s.text_font);   // LVGL bitmap: line_height = font_size
      const contentH = Math.max(0, s.h - s.pad_top - s.pad_bottom);
      const maxLines = Math.max(1, Math.floor(contentH / lh));
      innerStyle += `max-height:${maxLines * lh}px;overflow:hidden;`;
    }
    el.innerHTML = `<div class="ov-text" style="${innerStyle}">${renderHaspText(s.text, s.text_color)}</div>`;

    $("raw-payload").textContent = JSON.stringify(buildPayload(s), null, 2);

    if (!suppressPreviewBroadcast && currentPlateId) {
      sendWs({type: "preview", plate_id: currentPlateId, spec: s});
    }
  }

  function buildPayload(s) {
    const p = currentPlate() || {overlay_page: 1, overlay_id: 240};
    return {
      page: p.overlay_page || 1,
      id: p.overlay_id || 240,
      obj: "label",
      x: s.x, y: s.y, w: s.w, h: s.h,
      text: s.text,
      text_font: s.text_font,
      text_color: s.text_color,
      align: s.align,
      mode: s.mode,
      bg_opa: s.bg_opa,
      bg_color: s.bg_color,
      hidden: false,
      pad_top: s.pad_top,
      pad_bottom: s.pad_bottom,
      pad_left: s.pad_left,
      pad_right: s.pad_right,
    };
  }

  // ---------- Plate selector / plate sizing ----------
  function renderPlateSelector() {
    const sel = $("plate-select");
    sel.innerHTML = "";
    for (const p of meInfo.plates) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      sel.appendChild(opt);
    }
    if (currentPlateId && meInfo.plates.some((p) => p.id === currentPlateId)) {
      sel.value = currentPlateId;
    } else if (meInfo.plates.length) {
      currentPlateId = meInfo.plates[0].id;
      localStorage.setItem("hasp.currentPlateId", currentPlateId);
      sel.value = currentPlateId;
    }
  }

  function applyPlateSize() {
    const p = currentPlate();
    const pw = p ? p.plate_width : 480;
    const ph = p ? p.plate_height : 320;
    const plate = $("plate");
    plate.style.width = pw + "px";
    plate.style.height = ph + "px";
    const showClock = pw === 480 && ph === 320;
    $("bg-clock").style.display = showClock ? "" : "none";
    $("bg-date").style.display = showClock ? "" : "none";
    $("f-x").max = pw; $("f-w").max = pw;
    $("f-y").max = ph; $("f-h").max = ph;
    const lg = $("ps-legend");
    if (lg) lg.textContent = `Position & size (${pw} × ${ph})`;
    applyPlateScale();
  }

  // The .plate is kept at its native pixel size internally (so overlay px
  // coordinates remain accurate) and scaled visually via CSS `zoom` when the
  // viewport is too narrow to fit it. Unlike `transform: scale`, `zoom`
  // updates the element's effective box dimensions, so the surrounding
  // frame shrinks naturally without negative-margin hacks.
  function applyPlateScale() {
    const plate = $("plate");
    const frame = plate.parentElement;
    if (!plate.style.width) return;
    // Reset first so we measure parent width without our previous scale.
    plate.style.zoom = "";

    const pw = parseFloat(plate.style.width);
    const frameStyle = getComputedStyle(frame);
    const framePad = parseFloat(frameStyle.paddingLeft) + parseFloat(frameStyle.paddingRight);
    const frameParent = frame.parentElement;
    const fParentStyle = getComputedStyle(frameParent);
    const fParentPad = parseFloat(fParentStyle.paddingLeft) + parseFloat(fParentStyle.paddingRight);
    const available = frameParent.clientWidth - fParentPad - framePad;
    if (available <= 0) return;
    const scale = Math.min(1, available / pw);
    if (scale < 1) {
      plate.style.zoom = scale;
    }
  }

  // Recompute the plate scale whenever the window changes size.
  window.addEventListener("resize", () => {
    clearTimeout(window._plateScaleTimer);
    window._plateScaleTimer = setTimeout(applyPlateScale, 80);
  });

  // ---------- Presets ----------
  function computePresets() {
    const p = currentPlate() || {plate_width: 480, plate_height: 320};
    const pw = p.plate_width, ph = p.plate_height;
    const margin = Math.round(pw * 0.02);
    const banner = Math.round(ph * 0.19);
    const midH  = Math.round(ph * 0.47);
    const midY  = Math.round(ph * 0.27);
    return {
      full:   { x: margin, y: 0, w: pw - margin*2, h: ph,
                pad_top: Math.round(ph*0.31), pad_bottom: Math.round(ph*0.31) },
      middle: { x: margin, y: midY, w: pw - margin*2, h: midH,
                pad_top: Math.round(ph*0.08), pad_bottom: Math.round(ph*0.08) },
      top:    { x: 0, y: 0, w: pw, h: banner, pad_top: 8, pad_bottom: 8 },
      bottom: { x: 0, y: ph - banner, w: pw, h: banner, pad_top: 8, pad_bottom: 8 },
    };
  }

  // ---------- WebSocket ----------
  let ws = null, wsReady = false, reconnectTimer = null;
  const lastPlateLwt = {};      // plate_id -> "online"/"offline"/raw
  const lastActiveState = {};   // plate_id -> bool (last known display.active)

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { wsReady = true; };
    ws.onclose = () => {
      wsReady = false;
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connectWs, 2000);
    };
    ws.onerror = () => { try { ws.close(); } catch(_){} };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      handleWsMessage(msg);
    };
  }

  function sendWs(obj) {
    if (wsReady && ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }

  function handleWsMessage(msg) {
    if (msg.type === "mqtt_state") {
      const dot = $("mqtt-dot"), s = $("mqtt-status");
      dot.className = msg.connected ? "dot dot-on" : "dot dot-off";
      s.textContent = "MQTT";
      // Surface details (host, port, error) as a hover tooltip
      s.title = msg.connected
        ? `Connected to ${msg.host}:${msg.port}${msg.tls ? " (TLS)" : ""}`
        : `Disconnected${msg.last_error ? " — " + msg.last_error : ""}`;
    } else if (msg.type === "plate_lwt") {
      lastPlateLwt[msg.plate_id] = msg.value;
      if (msg.plate_id === currentPlateId) refreshPlateStatusForCurrent();
    } else if (msg.type === "display_state") {
      if (msg.plate_id === currentPlateId) {
        activeUntil = msg.active_until;
        $("display-state").textContent = msg.active ? "Message displayed" : "Idle";
        // Any plate state change (send, clear, auto-clear, MQTT trigger,
        // another client's action) refreshes the snapshot on every client.
        // 500ms gives the plate time to repaint before we fetch.
        setTimeout(refreshSnapshot, 500);
      }
      lastActiveState[msg.plate_id] = !!msg.active;
    } else if (msg.type === "preview") {
      if (!previewFocused && msg.spec && msg.plate_id === currentPlateId) {
        suppressPreviewBroadcast = true;
        writeSpec(msg.spec);
        suppressPreviewBroadcast = false;
      }
    } else if (msg.type === "error") {
      toast(msg.error || "Error", "error");
    }
  }

  function refreshPlateStatusForCurrent() {
    // Status is just "Plate" + the colour of the dot. No need for the word.
    const dot = $("plate-dot"), s = $("plate-status");
    s.textContent = "Plate";
    const v = (lastPlateLwt[currentPlateId] || "").toLowerCase();
    if (!currentPlateId) dot.className = "dot dot-off";
    else if (v.includes("online")) dot.className = "dot dot-on";
    else if (v.includes("offline")) dot.className = "dot dot-off";
    else dot.className = "dot dot-warn";
  }

  // ---------- Countdown ----------
  let activeUntil = null;
  setInterval(() => {
    const el = $("display-countdown");
    if (activeUntil) {
      const remain = activeUntil - Date.now() / 1000;
      el.textContent = remain > 0 ? `clears in ${Math.ceil(remain)}s` : "";
    } else {
      el.textContent = "";
    }
  }, 500);

  // ---------- Send / clear / init ----------
  function requireCurrentPlate() {
    if (!currentPlateId) {
      toast("No plate selected — add one in Setup", "error");
      return null;
    }
    return currentPlateId;
  }

  async function apiSend() {
    const pid = requireCurrentPlate(); if (!pid) return;
    const dur = parseFloat($("f-duration").value);
    const body = {
      spec: readSpec(),
      duration_seconds: Number.isFinite(dur) && dur > 0 ? dur : null,
    };
    try {
      const r = await fetch(`/api/plates/${pid}/send`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        toast(`Send failed: ${err.detail || r.statusText}`, "error");
        return;
      }
      toast("Sent", "ok");
      // Snapshot refresh is handled centrally via the display_state WS
      // broadcast, so every client (including this one) updates in sync.
    } catch (e) { toast(`Send failed: ${e}`, "error"); }
  }

  // ---------- Snapshot ----------
  function refreshSnapshot() {
    const p = currentPlate();
    const img = $("snapshot-img");
    const empty = $("snapshot-empty");
    const status = $("snapshot-status");
    if (!p) {
      img.removeAttribute("src");
      empty.style.display = "";
      status.textContent = "No plate selected";
      return;
    }
    // /api/me only exposes `has_ip` (boolean) to keep the actual IP admin-only;
    // the snapshot itself is fetched via the server proxy at /api/plates/{id}/snapshot
    // so the client never needs the IP directly.
    if (!p.has_ip) {
      img.removeAttribute("src");
      empty.style.display = "";
      empty.textContent = `No IP address set for "${p.name}". Open Setup and add one.`;
      status.textContent = "No IP";
      return;
    }
    empty.style.display = "none";
    status.textContent = "Loading…";
    img.onload = () => {
      status.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    };
    img.onerror = () => {
      img.removeAttribute("src");
      empty.style.display = "";
      empty.textContent = "Failed to fetch snapshot — is the plate reachable from the server?";
      status.textContent = "Fetch failed";
    };
    // Cache-buster so the browser always re-requests.
    img.src = `/api/plates/${p.id}/snapshot?t=${Date.now()}`;
  }

  async function apiInit() {
    const pid = requireCurrentPlate(); if (!pid) return;
    try {
      const r = await fetch(`/api/plates/${pid}/init`, {method: "POST"});
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        toast(`Init failed: ${err.detail || r.statusText}`, "error");
        return;
      }
      toast("Overlay created (hidden)", "ok");
    } catch (e) { toast(`Init failed: ${e}`, "error"); }
  }

  async function apiClear() {
    const pid = requireCurrentPlate(); if (!pid) return;
    try {
      const r = await fetch(`/api/plates/${pid}/clear`, {method: "POST"});
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        toast(`Clear failed: ${err.detail || r.statusText}`, "error");
        return;
      }
      toast("Cleared", "ok");
      // Snapshot refresh handled by the display_state WS broadcast.
    } catch (e) { toast(`Clear failed: ${e}`, "error"); }
  }

  // ---------- Templates ----------
  let templates = [];

  async function loadTemplates() {
    try {
      const pid = currentPlateId;
      const url = pid ? `/api/templates?plate_id=${encodeURIComponent(pid)}` : "/api/templates";
      const r = await fetch(url);
      templates = await r.json();
      renderTemplates();
    } catch (e) { toast(`Templates load failed: ${e}`, "error"); }
  }

  function slugify(s) {
    return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  }

  function renderTemplates() {
    const ul = $("tpl-list");
    ul.innerHTML = "";
    const plate = currentPlate();
    const plateSlug = plate ? slugify(plate.name) : "";
    for (const t of templates) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "name"; name.textContent = t.name;
      const tplSlug = slugify(t.name);
      const topic = `hasp-messenger/${plateSlug}/${tplSlug}`;
      name.title = `Click to load. Trigger via MQTT: ${topic}`;
      const loadTemplate = () => {
        writeSpec(t.spec);
        // Restore the saved auto-clear value too (empty/blank = manual clear).
        $("f-duration").value = (t.duration_seconds != null) ? t.duration_seconds : "";
      };
      name.addEventListener("click", loadTemplate);
      const send = document.createElement("button");
      send.textContent = "Send";
      send.addEventListener("click", async () => { loadTemplate(); await apiSend(); });
      const del = document.createElement("button");
      del.textContent = "×"; del.title = "Delete";
      del.addEventListener("click", async () => {
        if (!confirm(`Delete template "${t.name}"?`)) return;
        await fetch(`/api/templates/${t.id}`, {method: "DELETE"});
        loadTemplates();
      });
      li.append(name, send, del);
      ul.appendChild(li);
    }
  }

  async function saveTemplate() {
    const name = $("tpl-name").value.trim();
    if (!name) { toast("Give the template a name first", "error"); return; }
    if (!currentPlateId) { toast("Select a plate first", "error"); return; }
    const dur = parseFloat($("f-duration").value);
    const duration_seconds = Number.isFinite(dur) && dur > 0 ? dur : null;
    const r = await fetch("/api/templates", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        name,
        spec: readSpec(),
        plate_id: currentPlateId,
        duration_seconds,
      }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast(`Save failed: ${err.detail || r.statusText}`, "error");
      return;
    }
    $("tpl-name").value = "";
    toast("Saved", "ok");
    loadTemplates();
  }

  // ---------- /api/me — the source of truth for the main UI ----------
  async function loadMe() {
    const r = await fetch("/api/me");
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      showAccessDenied(err.detail || r.statusText);
      return false;
    }
    meInfo = await r.json();
    if (!meInfo.plates) meInfo.plates = [];
    // Users without any access yet get a friendlier landing screen than the
    // raw empty UI. Pending users were auto-added on first login; non-pending
    // no-access users were explicitly denied by an admin.
    if (meInfo.pending) {
      showPendingScreen();
      return false;
    }
    if (!meInfo.is_admin && meInfo.plates.length === 0) {
      showNoAccessScreen();
      return false;
    }
    applyMeToUi();
    renderPlateSelector();
    return true;
  }

  function showPendingScreen() {
    document.body.innerHTML = `
      <div style="padding:60px 40px;max-width:640px;margin:0 auto;color:#e6edf3;font-family:sans-serif">
        <h1 style="color:#d29922">Waiting for approval</h1>
        <p>You're signed in as <strong>${escapeHtml(meInfo.email)}</strong>.</p>
        <p>An admin needs to grant you access to one or more plates before you can use the app. They've been notified that you're waiting.</p>
        <p style="color:#8b949e;font-size:13px;margin-top:24px">Refresh this page after the admin updates your account.</p>
      </div>`;
  }

  function showNoAccessScreen() {
    document.body.innerHTML = `
      <div style="padding:60px 40px;max-width:640px;margin:0 auto;color:#e6edf3;font-family:sans-serif">
        <h1 style="color:#f85149">No plate access</h1>
        <p>You're signed in as <strong>${escapeHtml(meInfo.email)}</strong>, but no plates are assigned to you.</p>
        <p>Ask an admin to grant access in Setup → Users.</p>
      </div>`;
  }

  function applyMeToUi() {
    console.log("[applyMeToUi]", APP_VERSION, meInfo);
    $("user-email").textContent = meInfo.is_lan ? "LAN" : (meInfo.email || "—");
    $("user-email").title = meInfo.is_lan ? "Direct LAN access (no Cloudflare auth)" : meInfo.email;
    const badge = $("user-role");
    badge.textContent = meInfo.is_admin ? "Admin" : "User";
    badge.className = "role-badge " + (meInfo.is_admin ? "admin" : "user");
    // Setup + Users are admin-only (LAN counts as admin).
    $("open-broker").style.display = meInfo.is_admin ? "" : "none";
    $("open-users").style.display = meInfo.is_admin ? "" : "none";
  }

  function showAccessDenied(detail) {
    document.body.innerHTML = `
      <div style="padding:60px 40px;max-width:640px;margin:0 auto;color:#e6edf3;font-family:sans-serif">
        <h1 style="color:#f85149">Access denied</h1>
        <p>${detail}</p>
        <p style="color:#8b949e;font-size:13px">If you should have access, ask an admin to add you in Setup → Users.</p>
      </div>`;
  }

  // ---------- Setup modal (admin only) ----------
  async function loadAdminConfig() {
    const r = await fetch("/api/config");
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      toast(`Failed to load setup: ${err.detail || r.statusText}`, "error");
      return false;
    }
    brokerCfg = await r.json();
    if (!brokerCfg.plates) brokerCfg.plates = [];
    renderPlatesEditor();
    // Broker fields
    $("b-host").value = brokerCfg.host || "";
    $("b-port").value = brokerCfg.port || 8883;
    $("b-tls").checked = !!brokerCfg.use_tls;
    $("b-user").value = brokerCfg.username || "";
    $("b-pass").value = "";
    $("b-pass").placeholder = brokerCfg.password_set ? "(unchanged)" : "(none)";
    $("b-cid").value = brokerCfg.client_id || "hasp-messenger";
    // Cloudflare fields
    const cf = brokerCfg.cloudflare || {};
    $("cf-enabled").checked = !!cf.enabled;
    $("cf-account").value = cf.account_id || "";
    $("cf-appname").value = cf.application_name || "";
    $("cf-policyname").value = cf.policy_name || "";
    $("cf-token").value = "";
    $("cf-token").placeholder = cf.api_token_set ? "(unchanged)" : "(none)";
    $("cf-clear-token").checked = false;
    $("cf-status-msg").textContent = "";
    $("cf-status-msg").className = "cf-status-msg";
    return true;
  }

  // ---------- Users page (separate dialog) ----------
  async function openUsersPage() {
    // The plate list shown on each row comes from meInfo.plates (admins see
    // every plate via /api/me anyway).
    const ok = await loadUsers();
    if (!ok) return;
    $("users-modal").showModal();
  }

  async function loadUsers() {
    try {
      const r = await fetch("/api/users");
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        toast(`Failed to load users: ${err.detail || r.statusText}`, "error");
        console.warn("loadUsers failed:", r.status, err);
        return false;
      }
      users = await r.json();
      renderUsers();
      return true;
    } catch (e) {
      toast(`Failed to load users: ${e}`, "error");
      console.error("loadUsers exception:", e);
      return false;
    }
  }

  function renderUsers() {
    const list = $("users-list");
    list.innerHTML = "";
    if (!users.length) {
      $("users-empty").style.display = "";
      return;
    }
    $("users-empty").style.display = "none";
    // Pending users float to the top so admins notice them.
    const sorted = [...users].sort((a, b) => {
      if (a.pending !== b.pending) return a.pending ? -1 : 1;
      return (a.email || "").localeCompare(b.email || "");
    });
    for (const u of sorted) list.appendChild(buildUserRow(u));
  }

  function buildUserRow(u) {
    const tpl = $("user-row-template");
    const row = tpl.content.firstElementChild.cloneNode(true);
    row.dataset.email = u.email;
    row.querySelector(".u-email").textContent = u.email;
    const pendingBadge = row.querySelector(".u-pending-badge");
    if (u.pending) {
      pendingBadge.hidden = false;
      row.classList.add("user-row-pending");
    }
    const adminCb = row.querySelector(".u-admin");
    adminCb.checked = !!u.is_admin;
    if (u.is_admin) row.classList.add("user-row-admin");

    const checksDiv = row.querySelector(".u-plates-checks");
    // Use meInfo.plates as the source of available plates — admins see all of
    // them via /api/me, so this is the right list to assign from.
    for (const p of meInfo.plates) {
      const lbl = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.dataset.plateId = p.id;
      cb.checked = (u.allowed_plate_ids || []).includes(p.id);
      cb.addEventListener("change", () => persistUser(row));
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(" " + p.name));
      checksDiv.appendChild(lbl);
    }

    adminCb.addEventListener("change", () => {
      row.classList.toggle("user-row-admin", adminCb.checked);
      persistUser(row);
    });
    row.querySelector(".u-delete").addEventListener("click", async () => {
      if (!confirm(`Remove user ${u.email}?`)) return;
      const r = await fetch(`/api/users/${encodeURIComponent(u.email)}`, {method: "DELETE"});
      if (!r.ok) {
        toast("Delete failed", "error");
        return;
      }
      await loadUsers();
      toast("User removed", "ok");
    });
    return row;
  }

  async function persistUser(row) {
    const email = row.dataset.email;
    const is_admin = row.querySelector(".u-admin").checked;
    const allowed_plate_ids = Array.from(row.querySelectorAll(".u-plates-checks input:checked"))
      .map((c) => c.dataset.plateId);
    console.log("persistUser ->", email, {is_admin, allowed_plate_ids});
    try {
      const r = await fetch(`/api/users/${encodeURIComponent(email)}`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ is_admin, allowed_plate_ids }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        toast(`Failed: ${err.detail || r.statusText}`, "error");
        console.warn("persistUser failed:", r.status, err);
        return;
      }
      // Server clears the pending flag on any admin update — mirror that in
      // the DOM and the cached users array so the badge disappears right away.
      const updated = await r.json();
      row.querySelector(".u-pending-badge").hidden = true;
      row.classList.remove("user-row-pending");
      const idx = users.findIndex((u) => u.email === email);
      if (idx >= 0) users[idx] = updated;
      toast(`Updated ${email}`, "ok");
    } catch (e) {
      toast(`Failed: ${e}`, "error");
      console.error("persistUser exception:", e);
    }
  }

  async function addNewUser() {
    const input = $("new-user-email");
    const email = input.value.trim().toLowerCase();
    console.log("addNewUser called, email:", email);
    if (!email || !email.includes("@")) {
      toast("Enter a valid email", "error");
      return;
    }
    try {
      const r = await fetch(`/api/users/${encodeURIComponent(email)}`, {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ is_admin: false, allowed_plate_ids: [] }),
      });
      console.log("addNewUser response:", r.status);
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        toast(`Add failed: ${err.detail || r.statusText}`, "error");
        console.warn("addNewUser failed:", r.status, err);
        return;
      }
      input.value = "";
      await loadUsers();
      toast(`Added ${email}`, "ok");
    } catch (e) {
      toast(`Add failed: ${e}`, "error");
      console.error("addNewUser exception:", e);
    }
  }

  function renderPlatesEditor() {
    const list = $("plates-list");
    list.innerHTML = "";
    for (const p of brokerCfg.plates) {
      list.appendChild(buildPlateRow(p));
    }
  }

  function buildPlateRow(plate) {
    const tpl = $("plate-row-template");
    const row = tpl.content.firstElementChild.cloneNode(true);
    row.dataset.plateId = plate.id || "";
    row.querySelector(".p-name").value = plate.name ?? "Plate";
    row.querySelector(".p-prefix").value = plate.topic_prefix ?? "hasp/plate";
    row.querySelector(".p-ip").value = plate.ip_address ?? "";
    row.querySelector(".p-w").value = plate.plate_width ?? 480;
    row.querySelector(".p-h").value = plate.plate_height ?? 320;
    row.querySelector(".p-oid").value = plate.overlay_id ?? 240;
    row.querySelector(".p-opg").value = plate.overlay_page ?? 1;
    row.querySelector(".p-delete").addEventListener("click", () => {
      if (!confirm(`Remove plate "${row.querySelector('.p-name').value}"?`)) return;
      row.remove();
    });
    row.querySelectorAll("[data-size]").forEach((b) => {
      b.addEventListener("click", () => {
        const [w, h] = b.dataset.size.split("x").map((n) => +n);
        row.querySelector(".p-w").value = w;
        row.querySelector(".p-h").value = h;
      });
    });
    return row;
  }

  function collectPlatesFromEditor() {
    const out = [];
    for (const row of document.querySelectorAll("#plates-list .plate-row")) {
      const id = row.dataset.plateId || undefined;
      out.push({
        ...(id ? {id} : {}),
        name: row.querySelector(".p-name").value.trim() || "Plate",
        topic_prefix: row.querySelector(".p-prefix").value.trim().replace(/\/+$/,"") || "hasp/plate",
        ip_address: row.querySelector(".p-ip").value.trim(),
        plate_width: +row.querySelector(".p-w").value || 480,
        plate_height: +row.querySelector(".p-h").value || 320,
        overlay_id: +row.querySelector(".p-oid").value || 240,
        overlay_page: +row.querySelector(".p-opg").value || 1,
      });
    }
    return out;
  }

  async function saveBroker(ev) {
    ev.preventDefault();
    const body = {
      host: $("b-host").value.trim(),
      port: +$("b-port").value,
      use_tls: $("b-tls").checked,
      username: $("b-user").value,
      client_id: $("b-cid").value.trim(),
      plates: collectPlatesFromEditor(),
    };
    const pw = $("b-pass").value;
    if (pw) body.password = pw;
    if ($("b-clearpw").checked) body.clear_password = true;
    // Cloudflare config
    const cfBody = {
      enabled: $("cf-enabled").checked,
      account_id: $("cf-account").value.trim(),
      application_name: $("cf-appname").value.trim(),
      policy_name: $("cf-policyname").value.trim(),
    };
    const cfToken = $("cf-token").value;
    if (cfToken) cfBody.api_token = cfToken;
    if ($("cf-clear-token").checked) cfBody.clear_api_token = true;
    body.cloudflare = cfBody;
    if (body.plates.length === 0) {
      toast("Need at least one plate", "error");
      return;
    }
    const r = await fetch("/api/config", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    if (!r.ok) { toast("Save failed", "error"); return; }
    brokerCfg = await r.json();
    $("broker-modal").close();
    toast("Setup updated, reconnecting…", "ok");
    // Refresh meInfo so the main UI picks up new/renamed plates immediately.
    await loadMe();
    if (!meInfo.plates.some((p) => p.id === currentPlateId)) {
      currentPlateId = meInfo.plates[0]?.id || null;
      if (currentPlateId) localStorage.setItem("hasp.currentPlateId", currentPlateId);
    }
    setCurrentPlate(currentPlateId);
  }

  // ---------- Toast ----------
  let toastTimer = null;
  function toast(msg, kind="") {
    const el = $("toast");
    el.textContent = msg;
    el.className = `toast show ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = "toast"; }, 2500);
  }

  // ---------- Wire up ----------
  function bindForm() {
    const ids = ["f-text","f-x","f-y","f-w","f-h","f-font","f-text-color","f-bg-color","f-bg-opa","f-align","f-mode","f-pt","f-pb","f-pl","f-pr"];
    for (const id of ids) {
      const el = $(id);
      el.addEventListener("input", () => {
        if (id === "f-bg-opa") $("f-bg-opa-val").textContent = el.value;
        updatePreview();
      });
      el.addEventListener("focus", () => { previewFocused = true; });
      el.addEventListener("blur", () => { previewFocused = false; });
    }
    document.querySelectorAll(".editor .presets button").forEach((b) => {
      b.addEventListener("click", () => {
        const p = computePresets()[b.dataset.preset];
        if (!p) return;
        const map = { x: "f-x", y: "f-y", w: "f-w", h: "f-h",
                      pad_top: "f-pt", pad_bottom: "f-pb",
                      pad_left: "f-pl", pad_right: "f-pr" };
        for (const [k,v] of Object.entries(p)) if (map[k]) $(map[k]).value = v;
        updatePreview();
      });
    });
    $("btn-send").addEventListener("click", apiSend);
    $("btn-clear").addEventListener("click", apiClear);
    $("btn-init").addEventListener("click", apiInit);
    $("tpl-save").addEventListener("click", saveTemplate);
    $("snapshot-refresh").addEventListener("click", refreshSnapshot);

    $("plate-select").addEventListener("change", (e) => setCurrentPlate(e.target.value));

    $("open-broker").addEventListener("click", () => {
      loadAdminConfig().then((ok) => { if (ok) $("broker-modal").showModal(); });
    });
    $("b-cancel").addEventListener("click", () => $("broker-modal").close());
    $("broker-form").addEventListener("submit", saveBroker);
    $("add-plate").addEventListener("click", () => {
      const newPlate = {
        name: `Plate ${$("plates-list").children.length + 1}`,
        topic_prefix: "hasp/plate",
        plate_width: 480, plate_height: 320,
        overlay_id: 240, overlay_page: 1,
      };
      $("plates-list").appendChild(buildPlateRow(newPlate));
    });
    $("add-user").addEventListener("click", addNewUser);
    $("new-user-email").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addNewUser(); }
    });
    $("open-users").addEventListener("click", openUsersPage);
    $("u-close").addEventListener("click", () => $("users-modal").close());
    $("cf-test").addEventListener("click", cfTest);
    $("cf-sync-btn").addEventListener("click", cfSync);
  }

  async function cfTest() {
    const msg = $("cf-status-msg");
    msg.textContent = "Testing…";
    msg.className = "cf-status-msg";
    // First we have to save the current form values so the server's stored
    // config matches what's in the form before the test runs.
    try {
      const cfBody = {
        enabled: $("cf-enabled").checked,
        account_id: $("cf-account").value.trim(),
        application_name: $("cf-appname").value.trim(),
        policy_name: $("cf-policyname").value.trim(),
      };
      const cfToken = $("cf-token").value;
      if (cfToken) cfBody.api_token = cfToken;
      if ($("cf-clear-token").checked) cfBody.clear_api_token = true;
      const sr = await fetch("/api/config", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({cloudflare: cfBody}),
      });
      if (!sr.ok) {
        msg.textContent = "Save failed before test";
        msg.classList.add("error");
        return;
      }
      const r = await fetch("/api/cloudflare/test", {method: "POST"});
      const j = await r.json();
      if (!r.ok) {
        msg.textContent = j.detail || "Failed";
        msg.classList.add("error");
        return;
      }
      msg.textContent = j.detail || "OK";
      msg.classList.add("ok");
    } catch (e) {
      msg.textContent = String(e);
      msg.classList.add("error");
    }
  }

  async function cfSync() {
    try {
      const r = await fetch("/api/cloudflare/sync", {method: "POST"});
      const j = await r.json();
      if (!r.ok) {
        toast(`Sync failed: ${j.detail || r.statusText}`, "error");
        return;
      }
      const added = (j.added_pending || []).length;
      const total = (j.policy_emails || []).length;
      toast(`Synced: ${total} in CF policy, ${added} new pending`, "ok");
      await loadUsers();
    } catch (e) {
      toast(`Sync failed: ${e}`, "error");
    }
  }

  // ---------- Init ----------
  bindForm();
  loadMe().then((ok) => {
    if (!ok) return;
    setCurrentPlate(currentPlateId || (meInfo.plates[0]?.id ?? null));
    // Default starter settings — empty text, everything else sensible so a
    // user can just type and send.
    writeSpec({
      text: "",
      x: 10, y: 85, w: 460, h: 150,
      text_font: 48, text_color: "#FFFFFF",
      bg_color: "#FF0000", bg_opa: 255,
      align: "center", mode: "break",
      pad_top: 25, pad_bottom: 25, pad_left: 0, pad_right: 0,
    });
    loadTemplates();
    connectWs();
  });
})();
