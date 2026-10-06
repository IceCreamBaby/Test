/* Podcast Animator – Oberfläche (ohne Frameworks, läuft komplett lokal) */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let STATE = null;          // /api/state
let CURRENT = null;        // aktuelles Projekt
let pollTimer = null;
const clipCache = {};      // clipId -> Signatur der zuletzt gezeichneten Karte

const HAIR = { swoop: "Pony zur Seite", quiff: "Tolle", messy: "Wuschelig", long: "Lang", curly: "Locken", buzz: "Kurz", bun: "Dutt", bald: "Glatze" };
const OUTFIT = { hoodie: "Hoodie", tshirt: "T-Shirt", shirt: "Hemd", jacket: "Jacke" };
const BEARD = { none: "Kein Bart", stubble: "Dreitagebart", full: "Vollbart" };
const THEME_NAMES = { lila: "Lila Studio", nacht: "Nacht", warm: "Warm", gruen: "Grün" };
const STEPS = ["Audio", "Transkription", "Sprecher", "Clips", "Rendern"];

// --------------------------------------------------------------------------- Helfer
async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) {
    o.method = o.method || "POST";
    o.headers = { "Content-Type": "application/json", ...(o.headers || {}) };
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  const r = await fetch(path, o);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch (e) { /* ignore */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
}

function toast(msg, err = false) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.textContent = msg;
  $("#toast-root").appendChild(el);
  setTimeout(() => el.remove(), err ? 7000 : 3500);
}

function ts(sec) {
  sec = Math.max(0, Number(sec) || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const ss = s.toFixed(1).padStart(4, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
function dur(sec) { sec = Math.round(sec); return sec >= 3600 ? `${Math.floor(sec / 3600)} h ${Math.round((sec % 3600) / 60)} min` : sec >= 60 ? `${Math.floor(sec / 60)} min ${sec % 60} s` : `${sec} s`; }
function fileUrl(pid, path, download = false) { return `/api/projects/${pid}/file?path=${encodeURIComponent(path)}${download ? "&download=1" : ""}`; }
function charName(id) { const c = (STATE?.characters || []).find((c) => c.id === id); return c ? c.name : id; }
function charColor(id) { const c = (STATE?.characters || []).find((c) => c.id === id); return c ? c.color : "#888"; }
function opt(value, label, selected) { return `<option value="${esc(value)}" ${String(value) === String(selected) ? "selected" : ""}>${esc(label)}</option>`; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function openModal(html, onClose) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-bg"><div class="modal"><button class="close ghost" data-close>✕</button>${html}</div></div>`;
  const close = () => { root.innerHTML = ""; onClose && onClose(); };
  $("[data-close]", root).onclick = close;
  $(".modal-bg", root).addEventListener("mousedown", (e) => { if (e.target.classList.contains("modal-bg")) close(); });
  return { root: $(".modal", root), close };
}

async function refreshState() {
  STATE = await api("/api/state");
  const w = STATE.worker;
  const badge = $("#worker-badge");
  if (w.current) {
    const p = STATE.projects.find((p) => p.id === w.current.pid);
    const live = p?.live;
    badge.className = "worker-badge busy";
    badge.textContent = live ? `${live.step}: ${Math.round(live.progress * 100)} % – ${live.message}` : "Arbeitet …";
    badge.title = badge.textContent + (w.pending.length ? ` (+${w.pending.length} in der Warteschlange)` : "");
  } else {
    badge.className = "worker-badge idle";
    badge.textContent = w.pending.length ? `${w.pending.length} Aufträge warten` : "Bereit";
  }
  return STATE;
}

function schedulePoll(fn, ms = 1500) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try { await fn(); } catch (e) { console.warn(e); }
  }, ms);
}

// --------------------------------------------------------------------------- Router
async function route() {
  clearTimeout(pollTimer);
  CURRENT = null;
  Object.keys(clipCache).forEach((k) => delete clipCache[k]);
  const hash = location.hash || "#/";
  $$("[data-nav]").forEach((a) => a.classList.remove("active"));
  try { await refreshState(); } catch (e) { $("#app").innerHTML = `<div class="alert err">Server nicht erreichbar: ${esc(e.message)}</div>`; return; }
  if (hash.startsWith("#/p/")) return viewProject(hash.slice(4));
  if (hash.startsWith("#/figuren")) { $('[data-nav="figuren"]').classList.add("active"); return viewCharacters(); }
  if (hash.startsWith("#/einstellungen")) { $('[data-nav="einstellungen"]').classList.add("active"); return viewSettings(); }
  $('[data-nav="home"]').classList.add("active");
  return viewHome();
}
window.addEventListener("hashchange", route);

// --------------------------------------------------------------------------- Startseite
function viewHome() {
  const d = STATE.defaults;
  const r = d.render;
  const chars = STATE.characters;
  $("#app").innerHTML = `
  <div class="card">
    <div class="card-head"><h1>Neuen Short erstellen</h1>
      <span class="muted">Podcast-Video oder Audio rein – animierte Clips mit Untertiteln raus.</span></div>
    <div class="grid2">
      <div class="stack">
        <div class="dropzone" id="drop">
          <div class="big">📂</div>
          <div><b>Datei hierher ziehen</b> oder klicken zum Auswählen</div>
          <div class="hint">Video (MP4, MKV, MOV, WEBM …) oder Audio (MP3, WAV, M4A …) – ganzer Podcast oder ein Ausschnitt</div>
          <div class="file" id="file-name"></div>
          <input type="file" id="file" accept="video/*,audio/*,.mkv,.m4a,.opus" hidden>
        </div>
        <label class="field">… oder Pfad zur Datei auf deinem PC (schneller bei großen Dateien, kein Kopieren)
          <input type="text" id="path" placeholder="z.B. C:\\Users\\Du\\Videos\\podcast.mp4"></label>
        <label class="field">Projektname (optional)<input type="text" id="name" placeholder="z.B. Folge 123"></label>
      </div>
      <div class="stack">
        <div>
          <div class="field" style="margin-bottom:6px">Was soll passieren?</div>
          <div class="seg">
            <label><input type="radio" name="mode" value="auto" checked><span>🔍 Beste Clips automatisch finden</span></label>
            <label><input type="radio" name="mode" value="single"><span>🎬 Ganze Datei / Bereich als 1 Clip</span></label>
          </div>
        </div>
        <div class="row">
          <label class="field">Nur Bereich (optional): Start<input type="text" id="r-start" class="small" placeholder="1:02:30"></label>
          <label class="field">Ende<input type="text" id="r-end" class="small" placeholder="1:04:00"></label>
          <span class="hint" style="align-self:end;padding-bottom:10px">leer = ganze Datei</span>
        </div>
        <div class="row" id="auto-opts">
          <label class="field">Anzahl Clips<input type="number" id="count" class="tiny" min="1" max="30" value="${d.clip_count}"></label>
          <label class="field">Länge von (s)<input type="number" id="minlen" class="tiny" min="5" max="170" value="${d.min_len}"></label>
          <label class="field">bis (s)<input type="number" id="maxlen" class="tiny" min="10" max="180" value="${d.max_len}"></label>
          <label class="check" style="align-self:end;padding-bottom:8px" title="${STATE.settings.has_key ? "" : "In den Einstellungen einen Claude-API-Key eintragen"}">
            <input type="checkbox" id="claude" ${d.use_claude ? "checked" : ""} ${STATE.settings.has_key ? "" : "disabled"}> Clips mit Claude auswählen ✨</label>
        </div>
        <div class="row">
          <label class="field">Layout<select id="layout">${opt("studio", "Studio (beide am Tisch, Kamera-Schnitte)", r.layout)}${opt("split", "Split-Screen (oben/unten)", r.layout)}</select></label>
          <label class="field">Farbthema<select id="theme">${STATE.themes.map((t) => opt(t, THEME_NAMES[t] || t, r.theme)).join("")}</select></label>
        </div>
        <details class="adv"><summary>Erweiterte Einstellungen</summary>
          <div class="fields">
            <label class="field">Spracherkennung<select id="model">${Object.entries(STATE.models).map(([k, v]) => opt(k, `${k} – ${v}`, d.model)).join("")}</select>
              <span class="hint">${STATE.cuda ? "✅ NVIDIA-Grafikkarte erkannt" : "Läuft auf der CPU (langsamer bei großen Modellen)"}</span></label>
            <label class="field">Anzahl Sprecher<select id="speakers">${opt(2, "2 (Standard)", d.num_speakers)}${opt(3, "3 (mit Gast)", d.num_speakers)}${opt(1, "1", d.num_speakers)}${opt(0, "automatisch erkennen", d.num_speakers)}</select></label>
            <label class="field">Titel im Video<select id="titlemode">${opt("always", "immer anzeigen", r.title_mode)}${opt("start", "nur am Anfang", r.title_mode)}${opt("off", "aus", r.title_mode)}</select></label>
            <label class="field">Wörter pro Untertitel<select id="maxwords">${opt(2, "2", r.max_words)}${opt(3, "3 (Shorts-Stil)", r.max_words)}${opt(5, "5", r.max_words)}${opt(8, "8", r.max_words)}</select></label>
            <label class="check"><input type="checkbox" id="upper" ${r.uppercase ? "checked" : ""}> Untertitel in GROSSBUCHSTABEN</label>
            <label class="check"><input type="checkbox" id="autorender" ${d.auto_render ? "checked" : ""}> Clips sofort rendern</label>
          </div>
        </details>
        <div class="row"><div class="spacer"></div><button class="primary big" id="go">🚀 Los geht's</button></div>
        <div id="upload-progress" hidden><div class="hint" id="upload-text">Lade hoch …</div><div class="progress"><div style="width:0%"></div></div></div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-head"><h2>Deine Projekte</h2><span class="muted">${STATE.projects.length} Projekt(e)</span></div>
    <div class="projects" id="projects"></div>
  </div>
  ${chars.length ? "" : `<div class="alert warn">Keine Figuren gefunden.</div>`}`;

  renderProjectList();
  let file = null;
  const drop = $("#drop"), input = $("#file");
  drop.onclick = () => input.click();
  input.onchange = () => { if (input.files[0]) { file = input.files[0]; $("#file-name").textContent = `${file.name} (${(file.size / 1e6).toFixed(1)} MB)`; } };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => {
    e.preventDefault(); drop.classList.remove("over");
    if (e.dataTransfer.files[0]) { file = e.dataTransfer.files[0]; $("#file-name").textContent = `${file.name} (${(file.size / 1e6).toFixed(1)} MB)`; }
  };
  const syncMode = () => { $("#auto-opts").style.display = $('input[name="mode"]:checked').value === "auto" ? "" : "none"; };
  $$('input[name="mode"]').forEach((el) => (el.onchange = syncMode));

  $("#go").onclick = async () => {
    const path = $("#path").value.trim();
    if (!file && !path) return toast("Bitte zuerst eine Datei auswählen oder einen Pfad eingeben.", true);
    const body = {
      name: $("#name").value.trim(),
      range: [$("#r-start").value.trim() || null, $("#r-end").value.trim() || null],
      settings: {
        mode: $('input[name="mode"]:checked').value,
        clip_count: +$("#count").value, min_len: +$("#minlen").value, max_len: +$("#maxlen").value,
        use_claude: $("#claude").checked, model: $("#model").value, num_speakers: +$("#speakers").value,
        auto_render: $("#autorender").checked,
        render: { layout: $("#layout").value, theme: $("#theme").value, title_mode: $("#titlemode").value,
                  uppercase: $("#upper").checked, max_words: +$("#maxwords").value },
      },
    };
    $("#go").disabled = true;
    try {
      if (file && !path) {
        body.upload_id = (await uploadFile(file)).upload_id;
        body.filename = file.name;
      } else {
        body.path = path;
      }
      const p = await api("/api/projects", { json: body });
      location.hash = `#/p/${p.id}`;
    } catch (e) {
      toast(e.message, true);
      $("#go").disabled = false;
      $("#upload-progress").hidden = true;
    }
  };
  pollHome();
}

function renderProjectList() {
  const el = $("#projects");
  if (!el) return;
  if (!STATE.projects.length) { el.innerHTML = `<div class="empty">Noch keine Projekte – lade oben deinen ersten Podcast hoch.</div>`; return; }
  el.innerHTML = STATE.projects.map((p) => {
    const live = p.live;
    const status = live ? `<span class="badge info">${esc(live.step)} ${Math.round(live.progress * 100)} %</span>` :
      p.status === "ready" ? `<span class="badge ok">Fertig</span>` : p.status === "error" ? `<span class="badge err">Fehler</span>` :
      p.status === "processing" ? `<span class="badge info">Läuft</span>` : `<span class="badge">Wartet</span>`;
    return `<a class="project-card" href="#/p/${p.id}">
      <div class="name">${esc(p.name)}</div>
      <div class="row">${status}<span class="muted">${p.done}/${p.clips} Clips gerendert</span></div>
      <div class="hint">${new Date(p.created * 1000).toLocaleString("de-DE")}</div></a>`;
  }).join("");
}

async function pollHome() {
  schedulePoll(async () => {
    if (location.hash && location.hash !== "#/") return;
    await refreshState();
    renderProjectList();
    pollHome();
  }, STATE.worker.current ? 1500 : 4000);
}

function uploadFile(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/upload?filename=${encodeURIComponent(file.name)}`);
    $("#upload-progress").hidden = false;
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const f = e.loaded / e.total;
      $("#upload-progress .progress > div").style.width = `${(f * 100).toFixed(1)}%`;
      $("#upload-text").textContent = `Lade hoch … ${(e.loaded / 1e6).toFixed(0)} / ${(e.total / 1e6).toFixed(0)} MB`;
    };
    xhr.onload = () => (xhr.status < 300 ? resolve(JSON.parse(xhr.responseText)) : reject(new Error(xhr.responseText)));
    xhr.onerror = () => reject(new Error("Upload fehlgeschlagen"));
    xhr.send(file);
  });
}

// --------------------------------------------------------------------------- Projektseite
async function viewProject(pid) {
  let p;
  try { p = await api(`/api/projects/${pid}`); } catch (e) { $("#app").innerHTML = `<div class="alert err">${esc(e.message)}</div>`; return; }
  CURRENT = p;
  const r = p.settings.render;
  $("#app").innerHTML = `
  <div class="row" style="margin-bottom:16px">
    <a href="#/" class="btn ghost">← Projekte</a>
    <h1 style="margin:0" id="pname" title="Klicken zum Umbenennen">${esc(p.name)}</h1>
    <span class="muted" id="pinfo"></span>
    <div class="spacer"></div>
    <button id="open-folder">📁 Ordner öffnen</button>
    <button class="danger" id="del-project">🗑 Projekt löschen</button>
  </div>
  <div id="status"></div>
  <div id="notes"></div>
  <div class="card speakers" id="speakers-card"></div>
  <div>
    <div class="card" id="style-card">
      <div class="card-head"><h2>🎨 Stil für alle Clips</h2></div>
      <div class="fields">
        <label class="field">Layout<select id="s-layout">${opt("studio", "Studio", r.layout)}${opt("split", "Split-Screen", r.layout)}</select></label>
        <label class="field">Farbthema<select id="s-theme">${STATE.themes.map((t) => opt(t, THEME_NAMES[t] || t, r.theme)).join("")}</select></label>
        <label class="field">Titel im Video<select id="s-titlemode">${opt("always", "immer", r.title_mode)}${opt("start", "nur am Anfang", r.title_mode)}${opt("off", "aus", r.title_mode)}</select></label>
        <label class="field">Wörter pro Untertitel<select id="s-maxwords">${[2, 3, 5, 8].map((n) => opt(n, n, r.max_words)).join("")}</select></label>
        <label class="field">Neon-Schild<input type="text" id="s-sign" value="${esc(r.sign_text)}" maxlength="14"></label>
        <label class="field">Wasserzeichen<input type="text" id="s-wm" value="${esc(r.watermark)}"></label>
        <label class="field">Lebendigkeit<input type="range" id="s-live" min="0.3" max="1.6" step="0.1" value="${r.liveliness}"></label>
        <label class="check"><input type="checkbox" id="s-upper" ${r.uppercase ? "checked" : ""}> GROSSBUCHSTABEN</label>
        <label class="check"><input type="checkbox" id="s-names" ${r.show_names ? "checked" : ""}> Namen über Untertiteln</label>
        <label class="check"><input type="checkbox" id="s-subs" ${r.subtitles ? "checked" : ""}> Untertitel anzeigen</label>
      </div>
      <div class="row" style="margin-top:14px"><div class="spacer"></div><button id="save-style">Stil speichern</button></div>
    </div>
  </div>
  <div class="card">
    <div class="card-head"><h2>🎬 Clips</h2>
      <div class="row">
        <button id="add-clip">➕ Clip manuell</button>
        <button id="more-clips">🔍 Weitere Clips finden</button>
        <button class="primary" id="render-all">▶ Alle rendern</button>
      </div>
    </div>
    <div class="clips" id="clips"></div>
  </div>`;

  $("#pname").onclick = async () => {
    const name = prompt("Neuer Projektname:", CURRENT.name);
    if (name) { await api(`/api/projects/${pid}/rename`, { json: { name } }); $("#pname").textContent = name; }
  };
  $("#open-folder").onclick = async () => { try { const r = await api(`/api/projects/${pid}/open`, { method: "POST" }); toast(`Ordner: ${r.path}`); } catch (e) { toast(e.message, true); } };
  $("#del-project").onclick = async () => {
    if (!confirm("Projekt inklusive aller gerenderten Clips löschen?")) return;
    await api(`/api/projects/${pid}`, { method: "DELETE" });
    location.hash = "#/";
  };
  $("#save-style").onclick = async () => {
    const render = {
      layout: $("#s-layout").value, theme: $("#s-theme").value, title_mode: $("#s-titlemode").value,
      max_words: +$("#s-maxwords").value, sign_text: $("#s-sign").value, watermark: $("#s-wm").value,
      liveliness: +$("#s-live").value, uppercase: $("#s-upper").checked, show_names: $("#s-names").checked,
      subtitles: $("#s-subs").checked,
    };
    CURRENT = await api(`/api/projects/${pid}/settings`, { json: { render } });
    toast("Stil gespeichert – gerenderte Clips bitte neu rendern.");
    renderClips(true);
  };
  $("#render-all").onclick = async () => {
    const r = await api(`/api/projects/${pid}/render_all`, { json: { only_missing: true } });
    toast(r.queued ? `${r.queued} Clip(s) in der Warteschlange` : "Alle Clips sind schon aktuell.");
    refreshProject();
  };
  $("#more-clips").onclick = () => moreClipsDialog(pid);
  $("#add-clip").onclick = () => addClipDialog(pid);

  renderSpeakers();
  renderStatus();
  renderClips(true);
  pollProject();
}

function renderStatus() {
  const p = CURRENT;
  const live = p.live;
  const info = [];
  if (p.source_name) info.push(p.source_name);
  if (p.duration) info.push(dur(p.duration));
  if (p.settings.range && (p.settings.range[0] != null || p.settings.range[1] != null)) info.push(`Bereich ${ts(p.settings.range[0] || 0)}–${p.settings.range[1] != null ? ts(p.settings.range[1]) : "Ende"}`);
  $("#pinfo").textContent = info.join(" · ");
  let html = "";
  const w = p.worker || {};
  const queued = (w.pending || []).filter((j) => j.pid === p.id).length;
  if (live) {
    const idx = STEPS.indexOf(live.step);
    html = `<div class="card"><div class="steps">${STEPS.map((s, i) => `<span class="${i < idx ? "done" : i === idx ? "active" : ""}">${i < idx ? "✓ " : ""}${s}</span>`).join("")}</div>
      <div class="row"><div style="flex:1"><div class="progress"><div style="width:${(live.progress * 100).toFixed(1)}%"></div></div></div>
      <b>${Math.round(live.progress * 100)} %</b><button id="cancel">Abbrechen</button></div>
      <div class="hint" style="margin-top:6px">${esc(live.message)}${queued ? ` · ${queued} weitere Aufträge warten` : ""}</div></div>`;
  } else if (p.status === "error") {
    html = `<div class="alert err"><b>Fehler:</b> ${esc(p.error)}<div class="row" style="margin-top:10px">
      <button id="retry">🔁 Erneut verarbeiten</button></div></div>`;
  } else if (p.status === "new" || p.status === "processing") {
    html = `<div class="alert info">Wartet auf Verarbeitung …</div>`;
  }
  $("#status").innerHTML = html;
  if ($("#cancel")) $("#cancel").onclick = () => api(`/api/projects/${p.id}/cancel`, { method: "POST" });
  if ($("#retry")) $("#retry").onclick = async () => { await api(`/api/projects/${p.id}/reprocess`, { json: {} }); refreshProject(); };
  $("#notes").innerHTML = (p.notes || []).slice(-3).map((n) => `<div class="alert warn">${esc(n)}</div>`).join("");
}

function renderSpeakers() {
  const p = CURRENT;
  const el = $("#speakers-card");
  renderSpeakers.sig = JSON.stringify(p.speakers);
  if (!p.speakers.length) {
    el.innerHTML = `<div class="card-head"><h2>🗣️ Sprecher & Figuren</h2></div><div class="empty">Sprecher werden nach der Transkription erkannt …</div>`;
    return;
  }
  const chars = STATE.characters;
  const seatName = (i, n) => (n === 2 ? (i === 0 ? "links" : "rechts") : `Platz ${i + 1}`);
  el.innerHTML = `<div class="card-head"><h2>🗣️ Sprecher & Figuren</h2></div>
    <div class="hint" style="margin-bottom:10px">Hör kurz rein (▶) und prüfe, ob jede Stimme die richtige Figur hat. Mit ↑↓ änderst du die Sitzordnung.</div>
    <div id="spk-list">${p.speakers.map((sp, i) => `
      <div class="spk" data-spk="${sp.spk}">
        <div><div class="seat">${seatName(i, p.speakers.length)}</div>
          <button class="ghost" data-play="${sp.spk}" title="Hörprobe">▶</button></div>
        <div><b>${sp.other ? "Sonstige Stimmen" : `Sprecher ${sp.spk + 1}`}</b> <span class="muted">· ${dur(sp.talk_time)}${sp.other ? " (z.B. Werbung, Einspieler)" : " Redezeit"}</span>
          ${sp.matched ? '<span class="badge ok">Stimme wiedererkannt</span>' : ""}
          ${sp.by_name ? '<span class="badge info" title="Erkannt daran, wer wen beim Namen nennt – bitte kurz prüfen">am Namen erkannt</span>' : ""}
          <div class="sample">„${esc(sp.sample_text)}“</div></div>
        <select data-char>${opt("none", "– keine Figur –", sp.char)}${chars.map((c) => opt(c.id, c.name, sp.char)).join("")}</select>
        <div class="row" style="gap:4px"><button class="ghost" data-up>↑</button><button class="ghost" data-down>↓</button></div>
      </div>`).join("")}</div>
    <audio id="spk-audio" hidden></audio>
    <div class="row" style="margin-top:10px">
      <label class="check"><input type="checkbox" id="remember" checked> Stimmen für nächste Podcasts merken</label>
      <div class="spacer"></div><button id="save-speakers">Zuordnung speichern</button></div>
    <details class="adv"><summary>Stimmen falsch erkannt? Sprecher neu erkennen</summary>
      <div class="row">
        <label class="field">Anzahl Sprecher<select id="rd-n">${[[2, "2"], [3, "3 (mit Gast)"], [0, "automatisch"]].map(([v, l]) => opt(v, l, p.settings.num_speakers)).join("")}</select></label>
        <label class="check" style="align-self:end;padding-bottom:8px"><input type="checkbox" id="rd-refind"> danach Clips neu suchen</label>
        <button id="rd-go" style="align-self:end">🔄 Neu erkennen</button>
      </div>
      <div class="hint">Die Transkription bleibt erhalten – das dauert nur ein paar Minuten.</div>
    </details>`;
  $$("[data-play]", el).forEach((b) => (b.onclick = () => {
    const sp = p.speakers.find((s) => String(s.spk) === b.dataset.play);
    const a = $("#spk-audio");
    a.src = `/api/projects/${p.id}/audio?start=${sp.sample[0]}&end=${sp.sample[1]}`;
    a.play();
  }));
  $$("[data-up]", el).forEach((b) => (b.onclick = () => { const row = b.closest(".spk"); if (row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling); }));
  $$("[data-down]", el).forEach((b) => (b.onclick = () => { const row = b.closest(".spk"); if (row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row); }));
  $("#rd-go").onclick = async () => {
    if (!confirm("Sprecher neu erkennen? Manuelle Untertitel-Korrekturen in den Clips werden dabei zurückgesetzt.")) return;
    await api(`/api/projects/${p.id}/rediarize`, { json: { num_speakers: +$("#rd-n").value, refind: $("#rd-refind").checked } });
    toast("Sprecher werden neu erkannt – das dauert ein paar Minuten …");
    refreshProject();
  };
  $("#save-speakers").onclick = async () => {
    const speakers = $$(".spk", el).map((row) => ({ spk: +row.dataset.spk, char: $("[data-char]", row).value }));
    try {
      const r = await api(`/api/projects/${p.id}/speakers`, { json: { speakers, remember: $("#remember").checked } });
      toast(`Gespeichert${r.remembered ? ` – ${r.remembered} Stimme(n) gemerkt` : ""}. Clips bitte neu rendern.`);
      await refreshProject(true);
      renderSpeakers();
    } catch (e) { toast(e.message, true); }
  };
}

function clipSignature(c) {
  return JSON.stringify([c.status, c.video, c.rendered, c.title, c.start, c.end, c.stale, c.error,
    c.status === "rendering" ? Math.round((c.progress || 0) * 50) : 0]);
}

function renderClips(force = false) {
  const p = CURRENT;
  const el = $("#clips");
  if (!el) return;
  if (!p.clips.length) {
    el.innerHTML = p.status === "ready" ? `<div class="empty">Keine Clips. Mit „Weitere Clips finden“ oder „Clip manuell“ hinzufügen.</div>` : `<div class="empty">Clips erscheinen hier, sobald die Analyse fertig ist.</div>`;
    Object.keys(clipCache).forEach((k) => delete clipCache[k]);
    return;
  }
  if (el.querySelector(".empty")) el.innerHTML = "";
  const ids = p.clips.map((c) => c.id);
  $$(".clip", el).forEach((card) => { if (!ids.includes(card.dataset.id)) { card.remove(); delete clipCache[card.dataset.id]; } });
  p.clips.forEach((c, i) => {
    const sig = clipSignature(c);
    let card = el.querySelector(`.clip[data-id="${c.id}"]`);
    if (card && !force && clipCache[c.id] === sig) return;
    const html = clipCard(p, c);
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    const fresh = tmp.firstElementChild;
    if (card) {
      // laufende Videos nicht unterbrechen, wenn sich nur Kleinigkeiten ändern
      const vid = $("video", card);
      if (vid && !vid.paused && c.status === "done" && $("video", fresh)?.getAttribute("src") === vid.getAttribute("src")) {
        $(".body", card).replaceWith($(".body", fresh));
      } else card.replaceWith(fresh);
    } else {
      el.appendChild(fresh);
    }
    card = el.querySelector(`.clip[data-id="${c.id}"]`);
    if (el.children[i] !== card) el.insertBefore(card, el.children[i]);
    clipCache[c.id] = sig;
    bindClipCard(p, c, card);
  });
}

function clipCard(p, c) {
  const len = c.end - c.start;
  let media;
  if (c.status === "done" && c.video) {
    media = `<video controls preload="metadata" playsinline src="${fileUrl(p.id, c.video)}&v=${c.rendered || 0}" poster="${c.thumb ? fileUrl(p.id, c.thumb) + "&v=" + (c.rendered || 0) : ""}"></video>`;
  } else {
    media = `<div class="ph">🎞️<br>Noch nicht gerendert<br><button class="ghost" data-preview style="margin-top:8px">Vorschau-Bild</button></div>`;
  }
  let status = "";
  if (c.status === "rendering") status = `<div class="progress"><div style="width:${((c.progress || 0) * 100).toFixed(0)}%"></div></div><div class="hint">${esc(c.message || "Rendert …")}</div>`;
  else if (c.status === "queued") status = `<span class="badge info">In der Warteschlange</span>`;
  else if (c.status === "error") status = `<div class="alert err" style="margin:0">${esc(c.error || c.message)}</div>`;
  else if (c.status === "done" && c.stale) status = `<span class="badge warn">Geändert – neu rendern</span>`;
  else if (c.status === "done") status = `<span class="badge ok">Fertig</span>`;
  const src = c.source === "Claude" ? `<span class="badge pink">✨ Claude</span>` : c.source === "lokal" ? `<span class="badge">Automatisch</span>` : `<span class="badge">${esc(c.source || "manuell")}</span>`;
  return `<div class="clip" data-id="${c.id}">
    <div class="media">${media}</div>
    <div class="body">
      <div class="title">${esc(c.title || "(ohne Titel)")}</div>
      <div class="meta">${ts(c.start)} – ${ts(c.end)} · ${Math.round(len)} s ${len > 180 ? "⚠️ länger als 3 min" : ""}</div>
      <div class="row" style="gap:6px">${src}${c.score ? `<span class="muted" style="font-size:12px">Score ${(+c.score).toFixed(1)}</span>` : ""}</div>
      ${c.reason ? `<div class="meta">${esc(c.reason)}</div>` : ""}
      ${status}
      <div class="actions">
        <button data-edit>✏️ Bearbeiten</button>
        <button data-render ${c.status === "rendering" || c.status === "queued" ? "disabled" : ""}>${c.status === "done" ? "🔁 Neu" : "▶ Rendern"}</button>
        ${c.status === "done" && c.video ? `<a class="btn" href="${fileUrl(p.id, c.video, true)}">⬇ MP4</a>` : ""}
        ${c.status === "done" && c.srt ? `<a class="btn" href="${fileUrl(p.id, c.srt, true)}" title="Untertitel-Datei für YouTube">SRT</a>` : ""}
        <button class="ghost danger" data-del title="Clip löschen">🗑</button>
      </div>
    </div></div>`;
}

function bindClipCard(p, c, card) {
  const b = (sel, fn) => { const el = $(sel, card); if (el) el.onclick = fn; };
  b("[data-edit]", () => clipEditor(p.id, c.id));
  b("[data-render]", async () => { await api(`/api/projects/${p.id}/clips/${c.id}/render`, { method: "POST" }); refreshProject(); });
  b("[data-del]", async () => { if (!confirm("Clip löschen?")) return; await api(`/api/projects/${p.id}/clips/${c.id}`, { method: "DELETE" }); refreshProject(); });
  b("[data-preview]", (e) => {
    const media = $(".media", card);
    media.innerHTML = `<div class="ph">Erzeuge Vorschau …</div>`;
    const img = new Image();
    img.onload = () => { media.innerHTML = ""; media.appendChild(img); };
    img.onerror = () => { media.innerHTML = `<div class="ph">Vorschau fehlgeschlagen</div>`; };
    img.src = `/api/projects/${p.id}/clips/${c.id}/preview.jpg?r=${Date.now()}`;
  });
}

async function refreshProject(silent = false) {
  if (!CURRENT) return;
  const pid = CURRENT.id;
  const p = await api(`/api/projects/${pid}`);
  if (!CURRENT || CURRENT.id !== pid) return;
  CURRENT = p;
  renderStatus();
  if (JSON.stringify(p.speakers) !== renderSpeakers.sig) renderSpeakers();
  renderClips();
}

function pollProject() {
  schedulePoll(async () => {
    if (!CURRENT || !location.hash.startsWith("#/p/")) return;
    await Promise.all([refreshProject(), refreshState()]);
    const busy = CURRENT.live || CURRENT.status === "processing" || CURRENT.status === "new" ||
      CURRENT.clips.some((c) => c.status === "rendering" || c.status === "queued");
    pollProject.next = busy ? 1000 : 4000;
    pollProject();
  }, pollProject.next || 1000);
}

function moreClipsDialog(pid) {
  const s = CURRENT.settings;
  const m = openModal(`<h2>🔍 Weitere Clips finden</h2>
    <div class="row">
      <label class="field">Anzahl<input type="number" id="mc-count" class="tiny" value="3" min="1" max="20"></label>
      <label class="field">Länge von (s)<input type="number" id="mc-min" class="tiny" value="${s.min_len}"></label>
      <label class="field">bis (s)<input type="number" id="mc-max" class="tiny" value="${s.max_len}"></label>
      <label class="check" style="align-self:end;padding-bottom:8px"><input type="checkbox" id="mc-claude" ${STATE.settings.has_key ? "checked" : "disabled"}> mit Claude ✨</label>
    </div>
    <p class="hint">Bereits vorhandene Clip-Bereiche werden übersprungen.</p>
    <div class="row"><div class="spacer"></div><button class="primary" id="mc-go">Suchen</button></div>`);
  $("#mc-go", m.root).onclick = async () => {
    await api(`/api/projects/${pid}/find_clips`, { json: { count: +$("#mc-count").value, min_len: +$("#mc-min").value, max_len: +$("#mc-max").value, use_claude: $("#mc-claude").checked, append: true } });
    m.close();
    toast("Suche läuft …");
    refreshProject();
  };
}

async function addClipDialog(pid) {
  const m = openModal(`<h2>➕ Clip manuell hinzufügen</h2>
    <p class="hint">Klick im Transkript auf den ersten Satz (Start) und dann auf den letzten Satz (Ende) – oder gib die Zeiten direkt ein.</p>
    <div class="row">
      <label class="field">Start<input type="text" id="ac-start" class="small" placeholder="12:30"></label>
      <label class="field">Ende<input type="text" id="ac-end" class="small" placeholder="13:10"></label>
      <label class="field" style="flex:1">Titel<input type="text" id="ac-title" placeholder="Titel im Video"></label>
      <label class="check" style="align-self:end;padding-bottom:8px"><input type="checkbox" id="ac-render" checked> sofort rendern</label>
      <button class="primary" id="ac-go" style="align-self:end">Hinzufügen</button>
    </div>
    <input type="text" id="ac-search" placeholder="🔎 Im Transkript suchen …" style="width:100%;margin:12px 0">
    <div class="lines" id="ac-lines"><div class="empty">Lade Transkript …</div></div>`);
  const data = await api(`/api/projects/${pid}/transcript`);
  let pick = 0;
  const renderLines = (q = "") => {
    const ql = q.toLowerCase();
    const lines = data.lines.filter((l) => !ql || l.text.toLowerCase().includes(ql)).slice(0, 800);
    $("#ac-lines", m.root).innerHTML = lines.map((l) => `<div class="line" style="grid-template-columns:70px 110px 1fr;cursor:pointer" data-s="${l.s}" data-e="${l.e}">
      <span class="t">${ts(l.s)}</span><span style="color:${charColor(spkChar(l.spk))};font-weight:700">${esc(spkName(l.spk))}</span><span>${esc(l.text)}</span></div>`).join("") || `<div class="empty">Nichts gefunden.</div>`;
    $$(".line", m.root).forEach((row) => (row.onclick = () => {
      if (pick % 2 === 0) { $("#ac-start").value = ts(row.dataset.s); $("#ac-end").value = ""; }
      else { $("#ac-end").value = ts(+row.dataset.e + 0.3); }
      pick++;
      $$(".line", m.root).forEach((r) => (r.style.background = ""));
      row.style.background = "#2a2240";
    }));
  };
  renderLines();
  $("#ac-search", m.root).oninput = debounce((e) => renderLines(e.target.value), 200);
  $("#ac-go", m.root).onclick = async () => {
    try {
      await api(`/api/projects/${pid}/clips`, { json: { start: $("#ac-start").value, end: $("#ac-end").value, title: $("#ac-title").value, render: $("#ac-render").checked } });
      m.close();
      refreshProject();
    } catch (e) { toast(e.message, true); }
  };
}

function spkChar(spk) { const sp = (CURRENT?.speakers || []).find((s) => s.spk === spk); return sp ? sp.char : null; }
function spkName(spk) {
  const c = spkChar(spk);
  if (c && c !== "none") return charName(c);
  const sp = (CURRENT?.speakers || []).find((s) => s.spk === spk);
  return sp?.other ? "Sonstige" : `Sprecher ${spk + 1}`;
}

// --------------------------------------------------------------------------- Clip-Editor
async function clipEditor(pid, cid) {
  const c = CURRENT.clips.find((x) => x.id === cid);
  const r = c.render || {};
  const data = await api(`/api/projects/${pid}/clips/${cid}/lines`);
  const speakers = CURRENT.speakers;
  const m = openModal(`<h2>✏️ Clip bearbeiten</h2>
    <div class="editor">
      <div class="preview stack">
        <img id="ed-img" alt="Vorschau">
        <input type="range" id="ed-t" min="0" max="${(c.end - c.start).toFixed(1)}" step="0.1" value="${((c.end - c.start) * 0.3).toFixed(1)}">
        <div class="row"><button id="ed-prev">🖼️ Vorschau aktualisieren</button></div>
        <audio id="ed-audio" controls style="width:100%" src="/api/projects/${pid}/audio?start=${c.start}&end=${c.end}"></audio>
      </div>
      <div class="stack">
        <label class="field">Titel (oben im Video)<input type="text" id="ed-title" value="${esc(c.title)}"></label>
        <div class="row">
          <label class="field">Start<span class="row" style="gap:4px"><button class="ghost" data-nudge="start:-1">−1s</button><input type="text" id="ed-start" class="small" value="${ts(c.start)}"><button class="ghost" data-nudge="start:1">+1s</button></span></label>
          <label class="field">Ende<span class="row" style="gap:4px"><button class="ghost" data-nudge="end:-1">−1s</button><input type="text" id="ed-end" class="small" value="${ts(c.end)}"><button class="ghost" data-nudge="end:1">+1s</button></span></label>
          <label class="field">Layout<select id="ed-layout">${opt("", "wie Projekt", r.layout || "")}${opt("studio", "Studio", r.layout)}${opt("split", "Split-Screen", r.layout)}</select></label>
          <label class="field">Thema<select id="ed-theme">${opt("", "wie Projekt", r.theme || "")}${STATE.themes.map((t) => opt(t, THEME_NAMES[t] || t, r.theme)).join("")}</select></label>
        </div>
        <div><div class="row"><b>Untertitel & Sprecher</b><span class="hint">Text korrigieren oder Sprecher einer Zeile ändern. Leere Zeile = kein Untertitel.</span>
          <div class="spacer"></div>${data.edited ? '<button class="ghost" id="ed-reset">↺ Original wiederherstellen</button>' : ""}</div></div>
        <div class="lines" id="ed-lines">${data.lines.map((l, i) => `
          <div class="line" data-i="${i}">
            <span class="t" data-seek="${l.s}" title="Anhören">▶ ${ts(l.s - c.start)}</span>
            <select data-spk>${speakers.map((sp) => opt(sp.spk, spkName(sp.spk), l.spk)).join("")}</select>
            <input type="text" data-text value="${esc(l.text)}">
          </div>`).join("") || '<div class="empty">Keine Wörter in diesem Bereich.</div>'}</div>
        <div class="row"><div class="spacer"></div>
          <button id="ed-save">💾 Speichern</button>
          <button class="primary" id="ed-save-render">💾 Speichern & rendern</button></div>
      </div>
    </div>`);
  const lines = data.lines;
  const loadPreview = () => { $("#ed-img").src = `/api/projects/${pid}/clips/${cid}/preview.jpg?t=${$("#ed-t").value}&r=${Date.now()}`; };
  loadPreview();
  $("#ed-prev").onclick = loadPreview;
  $("#ed-t").onchange = loadPreview;
  $$("[data-seek]", m.root).forEach((el) => (el.onclick = () => { const a = $("#ed-audio"); a.currentTime = Math.max(0, +el.dataset.seek - c.start); a.play(); }));
  $$("[data-nudge]", m.root).forEach((b) => (b.onclick = (e) => {
    e.preventDefault();
    const [k, d] = b.dataset.nudge.split(":");
    const input = $(`#ed-${k}`);
    const v = parseTs(input.value) + Number(d);
    input.value = ts(Math.max(0, v));
  }));
  if ($("#ed-reset")) $("#ed-reset").onclick = async () => {
    await api(`/api/projects/${pid}/clips/${cid}`, { method: "PATCH", json: { reset_words: true } });
    m.close(); await refreshProject(); clipEditor(pid, cid);
  };
  const save = async (renderNow) => {
    const render = { ...(c.render || {}) };
    for (const [k, id] of [["layout", "#ed-layout"], ["theme", "#ed-theme"]]) {
      if ($(id).value) render[k] = $(id).value; else delete render[k];
    }
    const editedLines = $$("#ed-lines .line", m.root).map((row) => {
      const l = lines[+row.dataset.i];
      return { s: l.s, e: l.e, spk: +$("[data-spk]", row).value, text: $("[data-text]", row).value };
    });
    const changed = editedLines.some((l, i) => l.text !== lines[i].text || l.spk !== lines[i].spk);
    const body = { title: $("#ed-title").value, start: $("#ed-start").value, end: $("#ed-end").value, render, render_now: renderNow };
    if (changed) body.lines = editedLines;
    try {
      await api(`/api/projects/${pid}/clips/${cid}`, { method: "PATCH", json: body });
      toast(renderNow ? "Gespeichert – wird gerendert." : "Gespeichert.");
      m.close();
      refreshProject();
    } catch (e) { toast(e.message, true); }
  };
  $("#ed-save").onclick = () => save(false);
  $("#ed-save-render").onclick = () => save(true);
}

function parseTs(text) {
  const parts = String(text).trim().replace(",", ".").split(":").map(Number);
  return parts.reduce((acc, v) => acc * 60 + (isNaN(v) ? 0 : v), 0);
}

// --------------------------------------------------------------------------- Figuren
function viewCharacters() {
  const chars = STATE.characters;
  $("#app").innerHTML = `
  <div class="card">
    <div class="card-head"><h1>🧑‍🎨 Figuren</h1><button class="primary" id="new-char">➕ Neue Figur</button></div>
    <p class="hint">Klick auf eine Figur, um Frisur, Farben und Kleidung anzupassen. Eigene Zeichnungen (PNG) kannst du ebenfalls verwenden – siehe README, Abschnitt „Eigene Figuren“.</p>
    <div class="chars">${chars.map((c) => `<div class="char-card" data-id="${esc(c.id)}">
      <img data-src="${esc(c.id)}" alt="${esc(c.name)}"><div class="n"><span><span class="swatch" style="background:${esc(c.color)}"></span>${esc(c.name)}</span>
      <span class="muted" style="font-size:12px">${c.type === "sprite" ? "PNG" : c.builtin ? "Standard" : "eigene"}</span></div></div>`).join("")}</div>
  </div>`;
  $$(".char-card img").forEach(async (img) => {
    const r = await fetch("/api/characters/preview.jpg", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: img.dataset.src }) });
    img.src = URL.createObjectURL(await r.blob());
  });
  $$(".char-card").forEach((card) => (card.onclick = () => characterEditor(chars.find((c) => c.id === card.dataset.id))));
  $("#new-char").onclick = () => characterEditor({ ...chars.find((c) => c.id === "gast") || {}, id: "neue_figur", name: "Neue Figur", builtin: false });
}

function characterEditor(ch) {
  const c = { ...ch };
  if (c.type === "sprite") {
    openModal(`<h2>${esc(c.name)}</h2><p>Diese Figur besteht aus eigenen PNG-Bildern (Ordner <code>${esc(c.id)}</code>). Bearbeite die Bilder oder die <code>character.json</code> im Ordner direkt.</p>`);
    return;
  }
  const colorField = (k, label) => `<label class="field">${label}<input type="color" data-k="${k}" value="${esc(c[k] || "#888888")}"></label>`;
  const m = openModal(`<h2>Figur bearbeiten</h2>
    <div class="char-editor">
      <div><img id="ce-img" alt="Vorschau"><div class="hint" style="margin-top:8px">Vorschau aktualisiert sich automatisch.</div></div>
      <div class="stack">
        <div class="row">
          <label class="field">Name<input type="text" data-k="name" value="${esc(c.name)}"></label>
          <label class="field">ID (Dateiname)<input type="text" data-k="id" value="${esc(c.id)}" ${c.builtin ? "readonly" : ""}></label>
          ${colorField("color", "Akzentfarbe (Untertitel)")}
        </div>
        <div class="fields">
          ${colorField("skin", "Haut")}
          ${colorField("hair", "Haare")}
          <label class="field">Frisur<select data-k="hair_style">${Object.entries(HAIR).map(([k, v]) => opt(k, v, c.hair_style)).join("")}</select></label>
          ${colorField("eye_color", "Augen")}
          <label class="field">Kleidung<select data-k="outfit">${Object.entries(OUTFIT).map(([k, v]) => opt(k, v, c.outfit)).join("")}</select></label>
          ${colorField("outfit_color", "Kleidungsfarbe")}
          ${colorField("outfit_accent", "Zweitfarbe (Shirt/Kordeln)")}
          <label class="field">Bart<select data-k="beard">${Object.entries(BEARD).map(([k, v]) => opt(k, v, c.beard)).join("")}</select></label>
          <label class="field">Gesichtsbreite<input type="range" data-k="face_width" min="0.85" max="1.15" step="0.01" value="${c.face_width || 1}"></label>
          <label class="check"><input type="checkbox" data-k="headphones" ${c.headphones ? "checked" : ""}> Kopfhörer</label>
          ${colorField("headphones_color", "Kopfhörerfarbe")}
          <label class="check"><input type="checkbox" data-k="glasses" ${c.glasses ? "checked" : ""}> Brille</label>
          <label class="check"><input type="checkbox" data-k="cap" ${c.cap ? "checked" : ""}> Cap</label>
          ${colorField("cap_color", "Cap-Farbe")}
          <label class="check"><input type="checkbox" data-k="earring" ${c.earring ? "checked" : ""}> Ohrringe</label>
          <label class="check"><input type="checkbox" data-k="chain" ${c.chain ? "checked" : ""}> Kette</label>
        </div>
        <div class="row"><div class="spacer"></div><button class="primary" id="ce-save">💾 Speichern</button></div>
      </div>
    </div>`);
  const read = () => {
    $$("[data-k]", m.root).forEach((el) => {
      const k = el.dataset.k;
      c[k] = el.type === "checkbox" ? el.checked : el.type === "range" ? +el.value : el.value;
    });
    return c;
  };
  const preview = debounce(async () => {
    const r = await fetch("/api/characters/preview.jpg", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: ch.id, style: read() }) });
    if (r.ok) $("#ce-img").src = URL.createObjectURL(await r.blob());
  }, 250);
  $$("[data-k]", m.root).forEach((el) => (el.oninput = preview));
  preview();
  $("#ce-save").onclick = async () => {
    try {
      const data = read();
      delete data.builtin;
      await api("/api/characters", { json: data });
      toast("Figur gespeichert.");
      m.close();
      await refreshState();
      viewCharacters();
    } catch (e) { toast(e.message, true); }
  };
}

// --------------------------------------------------------------------------- Einstellungen
function viewSettings() {
  const s = STATE.settings;
  $("#app").innerHTML = `
  <div class="card">
    <h1>⚙️ Einstellungen</h1>
    <div class="stack" style="max-width:720px">
      <label class="field">Claude-API-Key (optional, für die intelligente Clip-Auswahl und Titel)
        <input type="password" id="key" placeholder="${s.key_hint ? "gespeichert " + esc(s.key_hint) + " – zum Ändern neu eingeben" : "sk-ant-…"}"></label>
      ${s.key_hint ? '<label class="check"><input type="checkbox" id="delkey"> gespeicherten Key löschen</label>' : ""}
      <div class="hint">Ohne Key funktioniert alles lokal und kostenlos. Mit Key versteht Claude den Inhalt (Pointen, Kontext) und schreibt Titel.
        Kosten: grob ein paar Cent pro Podcast-Folge. Key erstellen: <a href="https://console.anthropic.com/" target="_blank">console.anthropic.com</a></div>
      <label class="field">Kontext für die Clip-Auswahl<input type="text" id="ctx" value="${esc(s.podcast_context)}"></label>
      <label class="field">Namen/Wörter, die die Spracherkennung richtig schreiben soll<input type="text" id="hot" value="${esc(s.hotwords)}"></label>
      <label class="field">Standard-Wasserzeichen<input type="text" id="wm" value="${esc(s.watermark)}"></label>
      <label class="field">Standard-Neon-Schild<input type="text" id="sign" value="${esc(s.sign_text)}" maxlength="14"></label>
      <label class="field">Standard-Figuren (Reihenfolge = Sitzplatz links → rechts)
        <input type="text" id="defchars" value="${esc((s.default_chars || []).join(", "))}"></label>
      <div class="row"><div class="spacer"></div><button class="primary" id="save">Speichern</button></div>
      <div class="hint">Datenordner (Projekte, Modelle, Stimmprofile): <code>${esc(STATE.data_dir)}</code><br>
        Grafikkarte: ${STATE.cuda ? "NVIDIA erkannt – Spracherkennung läuft schnell ✅" : "keine NVIDIA-GPU nutzbar – Spracherkennung auf der CPU"}</div>
    </div>
  </div>`;
  $("#save").onclick = async () => {
    const body = { podcast_context: $("#ctx").value, hotwords: $("#hot").value, watermark: $("#wm").value, sign_text: $("#sign").value,
      default_chars: $("#defchars").value.split(",").map((x) => x.trim()).filter(Boolean) };
    if ($("#key").value.trim()) body.anthropic_api_key = $("#key").value.trim();
    if ($("#delkey") && $("#delkey").checked) body.anthropic_api_key = "";
    await api("/api/settings", { json: body });
    toast("Gespeichert.");
    route();
  };
}

route();
