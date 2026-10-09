/* The Street Talks Back — front end (vanilla JS, no deps). */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const BASE = location.pathname.replace(/\/(index\.html)?$/, ""); // "" at root, "/app" behind the ingress prefix
const url = (p) => (p.startsWith("/") ? BASE + p : p);
const api = async (path, opts) => {
  const r = await fetch(url(path), opts && opts.body ? { method: "POST", headers: { "Content-Type": "application/json" }, ...opts, body: JSON.stringify(opts.body) } : opts);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};
const toast = (msg) => { const t = document.createElement("div"); t.className = "toast"; t.textContent = msg; document.body.appendChild(t); setTimeout(() => t.remove(), 3200); };

// Browser TTS — plays on whatever machine is rendering the page (open the App on your laptop, not inside the VM).
function pickVoice() {
  const vs = speechSynthesis.getVoices();
  const want = (v) => /en/i.test(v.lang) && /female|samantha|zira|google uk english female|susan|karen|moira/i.test(v.name);
  return vs.find(want) || vs.find((v) => /en/i.test(v.lang) && /female/i.test(v.name)) || vs.find((v) => /en-US/i.test(v.lang)) || vs[0] || null;
}
if (window.speechSynthesis) speechSynthesis.onvoiceschanged = pickVoice;
function stopSpeak() { try { speechSynthesis.cancel(); } catch { /* no TTS */ } }
function speakBriefing(parts) {
  if (!window.speechSynthesis) { toast("This browser has no speech engine — open the App on your laptop in Chrome."); return; }
  stopSpeak();
  const text = parts.filter(Boolean).join(". ");
  const u = new SpeechSynthesisUtterance(text);
  u.voice = pickVoice();
  u.rate = 1.12; u.pitch = 1.38; u.lang = (u.voice && u.voice.lang) || "en-US";
  speechSynthesis.speak(u);
}

let PERSONAS = {}, TYPES = {}, DASH = null, liveCount = 0;

// ---------------------------------------------------------------- tabs
function showView(name) {
  $$("#tabs button").forEach((x) => x.classList.toggle("active", x.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  ({ dashboard: loadDashboard, complaints: loadComplaints, offenders: loadOffenders, matches: loadMatches })[name]?.();
  window.scrollTo(0, 0);
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  showView(b.dataset.view);
});
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]"); if (!g) return;
  e.preventDefault();
  showView(g.dataset.goto);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#modal").classList.contains("hidden")) $("#modal-close").click();
});

// ---------------------------------------------------------------- rendering helpers
const sevBadge = (s) => `<span class="sev sev-${s}" title="severity ${s}/5">${s}</span>`;
const levelPill = (l) => l ? `<span class="level ${l.name}">${l.name}${l.count > 1 ? " · " + l.count + "×" : ""}</span>` : "";
function complaintCard(c, compact = false) {
  return `<div class="complaint s${c.severity}" data-id="${c.id}">
    <div class="who"><b>${c.persona_emoji} ${esc(c.persona_name)}</b><span>${levelPill(c.level)} ${sevBadge(c.severity)}</span></div>
    <div class="say">“${esc(c.complaint)}”</div>
    <div class="meta"><span>${esc(c.violation_label)}</span>${c.offender ? `<span>offender: ${esc(c.offender)}</span>` : ""}<span>${esc(c.camera_id)}</span><span>${esc(c.filename)} · ${Math.round(c.segment_start)}–${Math.round(c.segment_end)}s</span><span>conf ${Math.round(c.confidence * 100)}%</span></div>
    ${compact ? "" : `<div class="ev">Evidence: “${esc(c.evidence)}”</div>`}
  </div>`;
}
function gridCard(c) {
  return `<div class="card" data-id="${c.id}">
    <video src="${url(c.stream_url)}" muted loop preload="metadata" onmouseenter="this.play()" onmouseleave="this.pause()"></video>
    <div class="body">
      <div class="row"><b>${c.persona_emoji} ${esc(c.persona_name)}</b><span>${levelPill(c.level)} ${sevBadge(c.severity)}</span></div>
      <div class="say">“${esc(c.complaint)}”</div>
      <div class="row"><span class="tag">${esc(c.violation_label)}${c.offender ? " · " + esc(c.offender) : ""}</span><span class="tag">${esc(c.camera_id)}</span></div>
    </div></div>`;
}
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-id]"); if (el && !e.target.closest("button")) openDetail(el.dataset.id);
});

// ---------------------------------------------------------------- detail modal
async function openDetail(id) {
  const m = $("#modal"); m.classList.remove("hidden"); $("#modal-body").innerHTML = `<div class="muted">Loading…</div>`;
  try {
    const d = await api("/api/complaints/" + id); const c = d.complaint;
    const vid = (s, lbl, mid = false) => s ? `<div class="${mid ? "mid" : ""}"><div class="lbl">${lbl}</div><video src="${url("/api/stream?source=" + encodeURIComponent(s.source))}" controls ${mid ? "autoplay" : ""} muted playsinline></video></div>` : `<div><div class="lbl">${lbl}</div><div class="muted">—</div></div>`;
    const counts = Object.entries(JSON.parse(c.object_counts || "{}")).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<span>${esc(k)} ×${v}</span>`).join("");
    $("#modal-body").innerHTML = `<div class="detail">
      <h3>${c.persona_emoji} ${esc(c.persona_name)} <span class="muted" style="font-size:13px;font-family:var(--font)">${esc(PERSONAS[c.persona]?.tagline || "")}</span></h3>
      <div>${levelPill(c.level)} ${sevBadge(c.severity)} <span class="pill">${esc(c.violation_label)}</span> ${c.rule ? `<span class="pill">ref ${esc(c.rule)}</span>` : ""} <span class="pill">confidence ${Math.round(c.confidence * 100)}%</span></div>
      <div class="say">“${esc(c.complaint)}”</div>
      <div class="trip">${vid(d.before, "5 s before")}${vid({ source: c.source }, "the moment", true)}${vid(d.after, "5 s after")}</div>
      <div class="grid">
        <div class="box"><h4>Evidence (verbatim from caption)</h4>“${esc(c.evidence)}”<br><br><span class="muted">${esc(c.filename)} · ${Math.round(c.segment_start)}–${Math.round(c.segment_end)}s · ${esc(c.camera_id)} · ${esc(c.location)}</span></div>
        <div class="box"><h4>YOLO detections (max per frame)</h4><div class="counts">${counts || "<span>none</span>"}</div>${c.offender ? `<br><b>Offender signature:</b> ${esc(c.offender)}` : ""}${c.actors?.length ? `<br><b>Actors:</b> ${esc(c.actors.join(", "))}` : ""}</div>
        <div class="box" style="grid-column:1/-1"><h4>Full caption (Cosmos Reason)</h4>${esc(c.caption)}</div>
      </div>
      <div class="actions">
        <button class="primary" id="esc-one">Escalate this complaint → ticket</button>
        ${c.offender ? `<button id="esc-off">Escalate everything on “${esc(c.offender)}” (${c.level.same_offender}×)</button>` : ""}
        <button id="esc-type">Escalate pattern “${esc(c.violation_label)}” (${c.level.same_type}×)</button>
        <span class="muted">Tickets are drafted by the agent from evidence only${DASH?.live ? "" : ""}.</span>
      </div></div>`;
    $("#esc-one").onclick = () => escalate({ persona: c.persona, complaint_ids: [c.id] });
    $("#esc-off") && ($("#esc-off").onclick = () => escalate({ persona: c.persona, offender: c.offender }));
    $("#esc-type").onclick = () => escalate({ persona: c.persona, violation_type: c.violation_type });
  } catch (e) { $("#modal-body").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}
$("#modal-close").onclick = () => { stopSpeak(); $("#modal").classList.add("hidden"); $$("#modal video").forEach((v) => v.pause()); };
$("#modal").addEventListener("click", (e) => { if (e.target === $("#modal")) $("#modal-close").click(); });

async function escalate(body) {
  const btns = $$(".actions button"); btns.forEach((b) => (b.disabled = true));
  try {
    const { ticket } = await api("/api/escalate", { body });
    showTicket(ticket); toast(`Ticket ${ticket.id} filed · webhook: ${ticket.webhook_status}`);
  } catch (e) { toast("Escalation failed: " + e.message); } finally { btns.forEach((b) => (b.disabled = false)); }
}
function showTicket(t) {
  $("#modal").classList.remove("hidden");
  $("#modal-body").innerHTML = `<div class="detail"><h3>🎫 ${esc(t.title)}</h3>
    <div><span class="pill">${t.id}</span> ${sevBadge(t.severity)} <span class="pill">${esc(PERSONAS[t.persona]?.name || t.persona)}</span> <span class="pill">${t.complaint_ids.length} clips</span> <span class="pill">webhook: ${esc(t.webhook_status)}</span></div>
    <div class="box" style="margin-top:12px"><pre style="white-space:pre-wrap;font:13px/1.6 var(--font);margin:0">${esc(t.body)}</pre></div></div>`;
}

// ---------------------------------------------------------------- live
let es = null;
function connectEvents() {
  es = new EventSource(url("/api/events"));
  es.onopen = () => setStatus(true);
  es.onerror = () => setStatus(false);
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "segment") renderNow(ev);
    if (ev.type === "complaint") { liveCount++; $("#live-counter").textContent = `${liveCount} filed`; $("#live-feed").insertAdjacentHTML("afterbegin", complaintCard(ev.complaint)); }
    if (ev.type === "ticket") toast(`Ticket ${ev.ticket.id} filed`);
    if (ev.type === "live_stopped") $("#live-now").insertAdjacentHTML("afterbegin", `<div class="muted">Replay finished.</div>`);
  };
}
function setStatus(ok) { $("#status").innerHTML = `<span class="dot ${ok ? "" : "off"}"></span>${ok ? "live link up" : "reconnecting…"}${DASH ? ` · ${DASH.totals.complaints} complaints · ${DASH.totals.segments_scanned} segments judged · store: ${DASH.store_backend}` : ""}`; }
function renderNow(ev) {
  const s = ev.segment; const v = $("#live-video");
  const src = url("/api/stream?source=" + encodeURIComponent(s.source));
  if (!v.src.endsWith(encodeURIComponent(s.source))) { v.src = src; v.play().catch(() => {}); }
  const p = PERSONAS[s.persona];
  $("#live-now").innerHTML = `<div class="seg"><span>${p ? p.emoji + " " + esc(p.name) + " is watching" : ""} · ${esc(s.camera_id)}</span><span>segment ${ev.index + 1}/${ev.total} · ${esc(s.filename)}</span></div>
    <div class="caption">${esc(s.caption)}</div>
    <div class="counts">${Object.entries(s.object_counts || {}).sort((a, b) => b[1] - a[1]).slice(0, 10).map(([k, n]) => `<span>${esc(k)} ×${n}</span>`).join("")}</div>`;
}
$("#live-start").onclick = async () => {
  liveCount = 0; $("#live-feed").innerHTML = ""; $("#live-counter").textContent = "0 filed";
  await api("/api/live/start", { body: { camera_id: $("#live-camera").value, speed: +$("#live-speed").value } });
  toast("Live replay started");
};
$("#live-stop").onclick = () => api("/api/live/stop", { body: {} });

// ---------------------------------------------------------------- dashboard
async function loadDashboard() {
  const d = (DASH = await api("/api/dashboard")); setStatus(true);
  const t = d.totals;
  $("#kpis").innerHTML = [["Complaints filed", t.complaints], ["High severity (4–5)", t.high_severity], ["Segments judged", `${t.segments_scanned}${t.archive_segments ? " / " + t.archive_segments : ""}`], ["Repeat offenders", t.offenders], ["Ticket-eligible", t.ticket_eligible], ["Tickets sent", t.tickets]]
    .map(([l, v]) => `<div class="kpi"><b>${v}</b><span>${l}</span></div>`).join("");
  $("#dash-personas").innerHTML = d.by_persona.map((p) => `<div class="prow" data-persona="${p.id}"><div class="em">${p.emoji}</div><div><div class="nm">${esc(p.name)}</div><div class="tg">${esc(p.tagline)} · ${p.scanned} judged</div></div><div class="ct"><b>${p.count}</b><span>${p.high} high · avg ${p.avg_severity}</span></div></div>`).join("");
  $$("#dash-personas .prow").forEach((r) => (r.onclick = () => { $("#f-persona").value = r.dataset.persona; $$("#tabs button")[2].click(); }));
  const max = Math.max(1, ...d.by_type.map((x) => x.count));
  $("#dash-types").innerHTML = d.by_type.slice(0, 10).map((x) => `<div class="bar"><div class="lbl"><span>${esc(x.label)}</span><small>${x.count}</small></div><div class="trk"><div class="fil" style="width:${(x.count / max) * 100}%"></div></div></div>`).join("") || `<div class="muted">No complaints yet. Start a scan or go live.</div>`;
  const smax = Math.max(1, ...d.by_severity);
  $("#dash-severity").innerHTML = d.by_severity.map((n, i) => `<div class="bar"><div class="lbl"><span>${sevBadge(i + 1)} ${["minor", "low", "moderate", "serious", "someone could get hurt"][i]}</span><small>${n}</small></div><div class="trk"><div class="fil" style="width:${(n / smax) * 100}%;background:var(--sev${i + 1})"></div></div></div>`).join("");
  const s = d.scan; const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
  $("#dash-scan").innerHTML = `<div class="muted">state: <b style="color:var(--text)">${s.state}</b>${s.current_camera ? " · " + esc(s.current_camera) : ""}</div><div class="progress"><div style="width:${pct}%"></div></div><div class="muted">${s.done}/${s.total} segments · ${s.skipped} skipped by trigger · ${s.errors} errors · store: ${d.store_backend}</div>`;
  $("#scan-start").disabled = s.state === "running" || s.state === "loading";
  $("#dash-tickets").innerHTML = d.tickets.map((t) => `<div class="ticket" data-ticket="${t.id}"><b>🎫 ${esc(t.title)}</b><span class="muted">${t.id} · ${esc(PERSONAS[t.persona]?.name || "")} · ${t.complaint_ids.length} clips · ${esc(t.webhook_status)}</span><pre>${esc(t.body)}</pre></div>`).join("") || `<div class="muted">No tickets yet. Open a complaint and escalate it.</div>`;
  $$("#dash-tickets .ticket").forEach((el) => (el.onclick = () => showTicket(d.tickets.find((t) => t.id === el.dataset.ticket))));
}
$("#scan-start").onclick = async () => { await api("/api/scan/start", { body: {} }); toast("Archive scan started"); loadDashboard(); };

// ---------------------------------------------------------------- complaints
async function loadComplaints() {
  const q = new URLSearchParams(); const p = $("#f-persona").value, t = $("#f-type").value, s = $("#f-sev").value;
  if (p) q.set("persona", p); if (t) q.set("type", t); if (s) q.set("min_severity", s); q.set("limit", "60");
  const d = await api("/api/complaints?" + q);
  $("#f-count").textContent = `${d.total} complaints`;
  $("#complaints-list").innerHTML = d.complaints.map(gridCard).join("") || `<div class="muted">Nothing filed for this filter yet.</div>`;
}
["#f-persona", "#f-type", "#f-sev"].forEach((s) => ($(s).onchange = loadComplaints));

// ---------------------------------------------------------------- offenders
async function loadOffenders() {
  const { offenders } = await api("/api/offenders");
  $("#offenders-list").innerHTML = offenders.map((o) => `<div class="off">
    <div class="top"><div><div class="name">${esc(o.offender)}</div><div class="muted">${o.persona_emoji} ${esc(o.persona_name)}</div></div><div>${levelPill({ name: o.level, count: o.count })} ${sevBadge(o.max_severity)}</div></div>
    <div class="strikes">${"<i></i>".repeat(Math.min(o.count, 24))}</div>
    <div class="muted">${o.types.map(([t, n]) => `${esc(TYPES[t]?.label || t)} ×${n}`).join(" · ")}</div>
    <div class="actions"><button class="small" data-view-off="${esc(o.offender)}" data-p="${o.persona}">View ${o.count} complaints</button>
      <button class="small match" data-match-off="${esc(o.offender)}" data-p="${o.persona}">💘 Match briefing</button>
      <button class="small ${o.level === "ticket" ? "primary" : ""}" data-esc-off="${esc(o.offender)}" data-p="${o.persona}">${o.ticketed ? "Ticket filed · file another" : "Escalate → ticket"}</button></div>
  </div>`).join("") || `<div class="muted">No repeat offenders yet.</div>`;
  $$("[data-match-off]").forEach((b) => (b.onclick = () => showMatch(b.dataset.p, b.dataset.matchOff)));
  $$("[data-view-off]").forEach((b) => (b.onclick = async () => {
    const d = await api(`/api/complaints?persona=${b.dataset.p}&offender=${encodeURIComponent(b.dataset.viewOff)}`);
    $("#modal").classList.remove("hidden"); $("#modal-body").innerHTML = `<div class="detail"><h3>${esc(b.dataset.viewOff)}</h3><div class="feed-list" style="max-height:75vh">${d.complaints.map((c) => complaintCard(c)).join("")}</div></div>`;
  }));
  $$("[data-esc-off]").forEach((b) => (b.onclick = () => escalate({ persona: b.dataset.p, offender: b.dataset.escOff })));
}

// ---------------------------------------------------------------- match briefing (dating-profile view of a persona x offender pair)
async function showMatch(persona, offender, refresh = false) {
  const m = $("#modal"); m.classList.remove("hidden");
  $("#modal-body").innerHTML = `<div class="detail"><h3>💘 ${esc(PERSONAS[persona]?.name || persona)} × <span style="text-transform:capitalize">${esc(offender)}</span></h3><div class="muted">Matchmaker's watching the clips… this'll be petty. 10–20 s.</div></div>`;
  try {
    const { match: d } = await api("/api/match", { body: { persona, offender, refresh } });
    const flags = (arr, cls) => arr.map((f) => `<li class="${cls}">${esc(f)}</li>`).join("");
    $("#modal-body").innerHTML = `<div class="detail match-card">
      <div class="match-head">
        <div><div class="muted" style="text-transform:uppercase;letter-spacing:.08em;font-size:11px">Matchmaker says</div>
          <h3>${d.persona_emoji} ${esc(d.persona_name)} × <span style="text-transform:capitalize">${esc(d.offender)}</span></h3>
          <div class="say">“${esc(d.headline)}”</div></div>
        <div class="score"><b>${d.compatibility}%</b><span>compatible</span><span class="muted">${d.encounter_count} encounters · ${d.clips_read} clips re-watched</span></div>
      </div>
      <div class="grid">
        <div class="box"><h4>Okay so, the look</h4>${esc(d.appearance)}</div>
        <div class="box"><h4>What they actually do</h4>${esc(d.functionality)}</div>
        <div class="box" style="grid-column:1/-1"><h4>If they were people</h4>${esc(d.as_humans)}</div>
        <div class="box"><h4>Red flags</h4><ul class="flags">${flags(d.red_flags, "red")}</ul></div>
        <div class="box"><h4>Green flags</h4><ul class="flags">${flags(d.green_flags, "green") || "<li class='muted'>none found</li>"}</ul></div>
        <div class="box" style="grid-column:1/-1"><h4>Verdict</h4><b>${esc(d.verdict)}</b></div>
      </div>
      <h4 class="muted" style="margin:16px 0 6px;text-transform:uppercase;letter-spacing:.08em;font-size:11px">Every encounter, reviewed</h4>
      <div class="evidence">${d.clips.map((c) => `<div class="ev"><video src="${url(c.stream_url)}" muted loop preload="metadata" onmouseenter="this.play()" onmouseleave="this.pause()"></video><div><b style="color:var(--text)">#${c.n} · ${esc(c.violation)} · sev ${c.severity}</b><br>${esc(c.note)}<br><span class="muted">${esc(c.filename)} · ${Math.round(c.start)}–${Math.round(c.end)}s</span></div></div>`).join("")}</div>
      <div class="actions"><button class="small primary" id="match-speak">▶ Read it out loud</button><button class="small" id="match-shush">■ Stop</button><button class="small" id="match-refresh">Rewrite briefing</button><button class="small" id="match-esc">Break up → file ticket</button><span class="muted">Open this page on your laptop (not the VM) to hear it. Hover a clip to play it.</span></div>
    </div>`;
    $("#match-speak").onclick = () => speakBriefing([d.headline, d.appearance, d.functionality, d.as_humans, d.verdict]);
    $("#match-shush").onclick = stopSpeak;
    $("#match-refresh").onclick = () => { stopSpeak(); showMatch(persona, offender, true); };
    $("#match-esc").onclick = () => escalate({ persona, offender });
  } catch (e) { $("#modal-body").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- vehicle matches (dating briefings)
async function loadMatches() {
  const { matches } = await api("/api/matches");
  const lede = document.querySelector("#view-matches .lede");
  if (lede) lede.innerHTML = `Your matchmaker. Cute, sarcastic, a little mean — she talks the briefing like a gossip-club president. <b>${matches.length} pairings</b> from vehicles that shared a clip, a camera, or the city.`;
  $("#matches-list").innerHTML = matches.map((m) => `<div class="pair">
    <div class="pair-vids">
      <div><video src="${url(m.preview_a.stream_url)}" muted loop preload="metadata" onmouseenter="this.play()" onmouseleave="this.pause()"></video><div class="who">${esc(m.a)} <span class="muted">· ${m.count_a}× on camera</span></div></div>
      <div class="heart">💘</div>
      <div><video src="${url(m.preview_b.stream_url)}" muted loop preload="metadata" onmouseenter="this.play()" onmouseleave="this.pause()"></video><div class="who">${esc(m.b)} <span class="muted">· ${m.count_b}× on camera</span></div></div>
    </div>
    <div class="pair-meta">
      <span class="pill">${m.kind === "met" ? "met in the same footage" : m.kind === "same_block" ? "haunt the same block" : "citywide situationship"}</span>
      ${m.shared_videos ? `<span class="pill">${m.shared_videos} shared videos</span>` : ""}
      ${(m.cameras || []).slice(0, 3).map((c) => `<span class="pill">${esc(c)}</span>`).join("")}
    </div>
    <button class="primary" data-va="${esc(m.a)}" data-vb="${esc(m.b)}">Rewatch the clips → write briefing</button>
  </div>`).join("") || `<div class="muted">Need at least two named vehicles in the complaint ledger.</div>`;
  $$("#matches-list [data-va]").forEach((b) => (b.onclick = () => showVehicleMatch(b.dataset.va, b.dataset.vb)));
}

async function showVehicleMatch(a, b, refresh = false) {
  const m = $("#modal"); m.classList.remove("hidden");
  $("#modal-body").innerHTML = `<div class="detail"><div style="display:flex;gap:12px;align-items:center"><img src="${url("/matchmaker-avatar.jpg?v=10")}" alt="" width="56" height="56" style="border-radius:12px;object-fit:cover;object-position:center 12%"><div><h3 style="margin:0">💘 ${esc(a)} × ${esc(b)}</h3><div class="muted">Okay so I'm watching every clip of these two… give me a second to be mean. 10–25 s.</div></div></div></div>`;
  try {
    const { match: d } = await api("/api/matches/briefing", { body: { a, b, refresh } });
    const flags = (arr, cls) => (arr || []).map((f) => `<li class="${cls}">${esc(f)}</li>`).join("");
    const reel = (clips, title) => `<h4 class="muted" style="margin:16px 0 6px;text-transform:uppercase;letter-spacing:.08em;font-size:11px">${esc(title)}</h4>
      <div class="evidence">${(clips || []).map((c) => `<div class="ev"><video src="${url(c.stream_url)}" muted loop preload="metadata" onmouseenter="this.play()" onmouseleave="this.pause()"></video><div><b style="color:var(--text)">#${c.n} ${esc(c.who)} · ${esc(c.violation || "on camera")} ${c.severity ? "· sev " + c.severity : ""}</b><br>${esc(c.note || c.caption || "")}<br><span class="muted">${esc(c.filename)} · ${Math.round(c.start)}–${Math.round(c.end)}s</span></div></div>`).join("")}</div>`;
    $("#modal-body").innerHTML = `<div class="detail match-card">
      <div class="match-head">
        <div><div class="muted" style="text-transform:uppercase;letter-spacing:.08em;font-size:11px">Matchmaker says · ${d.shared_videos} shared videos · ${d.count_a + d.count_b} complaints</div>
          <h3 style="text-transform:capitalize">${esc(d.a)} × ${esc(d.b)}</h3>
          <div class="say">“${esc(d.headline)}”</div></div>
        <div class="score"><b>${d.compatibility}%</b><span>compatible</span></div>
      </div>
      <div class="grid">
        <div class="box"><h4>Okay so, ${esc(d.a)}</h4>${esc(d.appearance_a)}</div>
        <div class="box"><h4>And ${esc(d.b)}</h4>${esc(d.appearance_b)}</div>
        <div class="box" style="grid-column:1/-1"><h4>Walking in together</h4>${esc(d.appearance_together)}</div>
        <div class="box"><h4>What they actually do</h4>${esc(d.functionality)}</div>
        <div class="box"><h4>Why I even ship this</h4>${esc(d.why_match)}</div>
        <div class="box" style="grid-column:1/-1"><h4>If they were people</h4>${esc(d.as_humans)}</div>
        <div class="box"><h4>Red flags</h4><ul class="flags">${flags(d.red_flags, "red")}</ul></div>
        <div class="box"><h4>Green flags</h4><ul class="flags">${flags(d.green_flags, "green") || "<li class='muted'>none found</li>"}</ul></div>
        <div class="box" style="grid-column:1/-1"><h4>Verdict</h4><b>${esc(d.verdict)}</b></div>
      </div>
      ${reel(d.clips_a, "Every clip of " + d.a + ", with a note")}
      ${reel(d.clips_b, "Every clip of " + d.b + ", with a note")}
      ${d.extras?.length ? reel(d.extras, "Extra footage the search pulled") : ""}
      <div class="actions"><button class="small primary" id="vmatch-speak">▶ Read it out loud</button><button class="small" id="vmatch-shush">■ Stop</button><button class="small" id="vmatch-refresh">Rewrite briefing</button><span class="muted">Open this page on your laptop (not the VM) to hear her. Hover a clip to play video.</span></div>
    </div>`;
    $("#vmatch-speak").onclick = () => speakBriefing([d.headline, d.appearance_together, d.functionality, d.why_match, d.as_humans, d.verdict]);
    $("#vmatch-shush").onclick = stopSpeak;
    $("#vmatch-refresh").onclick = () => { stopSpeak(); showVehicleMatch(a, b, true); };
  } catch (e) { $("#modal-body").innerHTML = `<div class="muted">${esc(e.message)}</div>`; }
}

// ---------------------------------------------------------------- interview
const SUGGESTED = {
  bike_lane: ["What happened on your lane tonight?", "Which vehicles keep parking on you?", "How many cyclists had to merge into traffic?"],
  hydrant_7th: ["Who parked on you today?", "Did any trucks block the crosswalk?", "How busy was the corner this evening?"],
  crosswalk_market: ["Did people cross against the signal?", "Any vehicles blocking you?", "What was the worst moment you saw?"],
  queen_st: ["Did any drivers fail to yield?", "Was anyone on their phone?", "How were pedestrians treated at the crossing?"],
  driveway_maple: ["Who parked in front of you?", "Any vehicles lingering?", "What happened overnight?"],
  i24_corridor: ["Anyone on the shoulder?", "How is traffic flowing?", "Any trucks tailgating?"],
  dock_b: ["Did a forklift get too close to anyone?", "Anyone in the forklift zone?", "Was anything blocking the aisle?"],
};
$("#iv-persona").onchange = () => { $("#iv-chips").innerHTML = (SUGGESTED[$("#iv-persona").value] || []).map((q) => `<button>${esc(q)}</button>`).join(""); $$("#iv-chips button").forEach((b) => (b.onclick = () => { $("#iv-question").value = b.textContent; $("#iv-ask").click(); })); };
$("#iv-ask").onclick = async () => {
  const persona = $("#iv-persona").value, question = $("#iv-question").value.trim(); if (!question) return;
  $("#iv-answer").innerHTML = `<div class="answer"><div class="muted">${PERSONAS[persona].emoji} ${esc(PERSONAS[persona].name)} is reviewing the footage…</div></div>`;
  try {
    const d = await api("/api/interview", { body: { persona, question } });
    $("#iv-answer").innerHTML = `<div class="answer"><div class="say">${PERSONAS[persona].emoji} “${esc(d.answer)}”</div>
      <div class="grounded"><b>Grounded answer from the archive:</b> ${esc(d.grounded_answer || "—")}</div>
      <div class="evidence">${d.evidence.map((e) => `<div class="ev"><video src="${url(e.stream_url)}" muted loop preload="metadata" onmouseenter="this.play()" onmouseleave="this.pause()"></video><div>${esc(e.filename)} · ${Math.round(e.start || 0)}–${Math.round(e.end || 0)}s · ${Math.round((e.score || 0) * 100)}%</div></div>`).join("")}</div>
      ${d.related_complaints.length ? `<h4 class="muted" style="margin:14px 0 6px">Complaints already filed on these clips</h4><div class="feed-list">${d.related_complaints.map((c) => complaintCard(c, true)).join("")}</div>` : ""}</div>`;
  } catch (e) { $("#iv-answer").innerHTML = `<div class="answer muted">${esc(e.message)}</div>`; }
};
$("#iv-question").addEventListener("keydown", (e) => e.key === "Enter" && $("#iv-ask").click());

// ---------------------------------------------------------------- search
$("#s-go").onclick = async () => {
  const q = $("#s-q").value.trim(); if (!q) return;
  $("#s-results").innerHTML = `<div class="muted">Searching…</div>`;
  const d = await api(`/api/search?q=${encodeURIComponent(q)}${$("#s-persona").value ? "&persona=" + $("#s-persona").value : ""}`);
  $("#s-results").innerHTML = d.results.map((r) => r.complaint ? gridCard(r.complaint) : `<div class="card clear" ${r.scanned ? "" : `data-judge="${esc(r.source)}"`}>
    <video src="${url(r.stream_url)}" muted loop preload="metadata" onmouseenter="this.play()" onmouseleave="this.pause()"></video>
    <div class="body"><div class="row"><b>${PERSONAS[r.persona]?.emoji || ""} ${esc(PERSONAS[r.persona]?.name || r.camera_id)}</b><span class="tag">${Math.round(r.score * 100)}% match</span></div>
    <div class="say">${r.scanned ? "No complaint. Nothing in the caption supports one." : "Not judged yet — click to ask the persona now."}</div><div class="cap">${esc(r.caption)}</div></div></div>`).join("") || `<div class="muted">No matches.</div>`;
  $$("[data-judge]").forEach((el) => (el.onclick = async () => {
    el.querySelector(".say").textContent = "Judging…";
    const { verdict } = await api("/api/judge", { body: { source: el.dataset.judge } });
    if (verdict?.violation) el.outerHTML = gridCard(verdict); else el.querySelector(".say").textContent = "No complaint. Nothing in the caption supports one.";
  }));
};
$("#s-q").addEventListener("keydown", (e) => e.key === "Enter" && $("#s-go").click());
["truck parked in the bike lane", "red light runner", "vehicle blocking the crosswalk", "usps truck"].forEach((q) => {
  const b = document.createElement("button"); b.type = "button"; b.textContent = q;
  b.onclick = () => { $("#s-q").value = q; $("#s-go").click(); };
  $("#s-chips").appendChild(b);
});

// ---------------------------------------------------------------- boot
(async function boot() {
  const p = await api("/api/personas"); TYPES = p.violation_types;
  p.personas.forEach((x) => (PERSONAS[x.id] = x));
  const opts = p.personas.map((x) => `<option value="${x.id}">${x.emoji} ${esc(x.name)}</option>`).join("");
  $("#iv-persona").innerHTML = opts; $("#f-persona").insertAdjacentHTML("beforeend", opts); $("#s-persona").insertAdjacentHTML("beforeend", opts);
  $("#f-type").insertAdjacentHTML("beforeend", Object.entries(TYPES).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join(""));
  $("#iv-persona").onchange();
  try {
    const { cameras } = await api("/api/cameras");
    const cams = cameras.filter((c) => c.persona);
    const bike = cams.find((c) => (c.camera_id || "").includes("nyc_bike_gopro"));
    const ordered = bike ? [bike, ...cams.filter((c) => c !== bike)] : cams;
    $("#live-camera").innerHTML = ordered.map((c) => `<option value="${c.camera_id}">${PERSONAS[c.persona].emoji} ${esc(c.camera_id)} · ${c.segments} segments</option>`).join("");
  } catch { $("#live-camera").innerHTML = `<option value="nyc_bike_gopro-1">nyc_bike_gopro-1</option>`; }
  connectEvents(); loadDashboard(); setInterval(() => { if ($("#view-dashboard").classList.contains("active")) loadDashboard(); }, 8000);
})();
