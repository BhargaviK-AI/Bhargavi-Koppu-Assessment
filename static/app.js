const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const errText = (j) => j.detail || j.error || "Something went wrong.";
const localToday = () => new Date().toLocaleDateString("en-CA");
const addDays = (d, n) => new Date(Date.parse(d + "T12:00:00") + n * 86_400_000).toLocaleDateString("en-CA");

const LEVEL_TEXT = { info: "No signals", low: "Low risk", moderate: "Moderate risk", high: "High risk", severe: "Severe risk" };
const BASIS_TEXT = { observed: "Observed now", forecast: "Forecast", inference: "AI inference", "official-alert": "Official alert" };
const ROLE_TEXT = { origin: "Origin", destination: "Destination", connection1: "Connection", "alternate-origin": "Alternate origin", "alternate-destination": "Alternate destination", "alternate-connection1": "Alternate connection" };

let health = { ai: false };
let current = null;

init();

async function init() {
  $("#date").value = addDays(localToday(), 1);
  health = await fetch("/api/health").then((r) => r.json()).catch(() => health);
  $("#nl-form").addEventListener("submit", onNaturalLanguage);
  $("#form").addEventListener("submit", (e) => { e.preventDefault(); runCheck(readForm()); });
  for (const id of ["origin", "destination", "via"]) $("#" + id).addEventListener("input", suggest);
}

let suggestTimer;
function suggest(e) {
  clearTimeout(suggestTimer);
  suggestTimer = setTimeout(async () => {
    const list = await fetch(`/api/airports?q=${encodeURIComponent(e.target.value)}`).then((r) => r.json());
    $("#ap-list").innerHTML = list.map((a) => `<option value="${esc(a.iata)}">${esc(a.city)}, ${esc(a.state)} — ${esc(a.name)}</option>`).join("");
  }, 150);
}

const readForm = () => ({ origin: $("#origin").value, destination: $("#destination").value, via: $("#via").value ? [$("#via").value] : [], date: $("#date").value });

async function onNaturalLanguage(e) {
  e.preventDefault();
  const text = $("#nl").value.trim();
  if (!text) return;
  setStatus(health.ai ? "Claude is reading your request…" : "Reading your request…");
  const res = await fetch("/api/parse", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, today: localToday() }) });
  const p = await res.json();
  if (!res.ok) return setStatus(errText(p), "error");
  $("#origin").value = p.origin; $("#destination").value = p.destination;
  $("#via").value = p.connections?.[0] ?? ""; if (p.date) $("#date").value = p.date;
  const note = $("#parse-note");
  note.hidden = false;
  note.innerHTML = `Interpreted ${p.parsed_by === "claude" ? "by Claude" : "by simple rules (AI off)"} as: <b>${esc(p.origin || "?")}</b> → <b>${esc(p.destination || "?")}</b>${p.connections?.length ? ` via <b>${esc(p.connections.join(", "))}</b>` : ""} on <b>${esc(p.date || "?")}</b>${p.time_of_day ? `, ${esc(p.time_of_day)}` : ""}. <a href="#" id="edit-link">Edit</a>`;
  $("#edit-link").onclick = (ev) => { ev.preventDefault(); $("#manual").open = true; };
  if (p.clarification || !p.origin || !p.destination || !p.date) {
    $("#manual").open = true;
    return setStatus(p.clarification || "Please complete the missing details below.", "warn");
  }
  runCheck(readForm());
}

function setStatus(msg, kind = "busy") {
  const el = $("#status");
  el.hidden = !msg;
  el.className = `status ${kind}`;
  el.innerHTML = kind === "busy" ? `<span class="spinner"></span>${esc(msg)}` : esc(msg);
}

async function runCheck(input) {
  $("#result").hidden = true;
  setStatus("Gathering live data from FAA, National Weather Service, Aviation Weather Center and Open-Meteo…");
  const res = await fetch("/api/evidence", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) });
  const bundle = await res.json();
  if (!res.ok) return setStatus(errText(bundle), "error");
  current = { bundle, assessment: null };
  setStatus(health.ai ? "Claude is weighing the evidence…" : "");
  render();
  const ar = await fetch("/api/assess", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: bundle.id }) });
  const a = await ar.json();
  setStatus("");
  current.assessment = ar.ok ? a : { error: errText(a) };
  render();
}

// ---------- Rendering ----------

function render() {
  const { bundle: b, assessment: a } = current;
  const ev = Object.fromEntries(b.evidence.map((e) => [e.id, e]));
  const t = b.trip;
  const route = [t.origin, ...t.connections, t.destination].map((x) => `<b>${esc(x.iata)}</b> <span class="muted">${esc(x.city)}</span>`).join(' <span class="arrow">→</span> ');
  const notes = [t.origin, t.destination, ...t.connections].map((x) => x.note).filter(Boolean);

  $("#result").hidden = false;
  $("#result").innerHTML = `
    <div class="trip-head">
      <div class="route">${route}</div>
      <div class="muted">${esc(fmtDate(t.date))} · ${t.lead_days === 0 ? "today" : t.lead_days === 1 ? "tomorrow" : `in ${t.lead_days} days`}</div>
    </div>
    ${notes.map((n) => `<p class="warnline">${esc(n)}</p>`).join("")}
    ${a ? verdict(a, b) : `<div class="card verdict pending"><div class="skeleton"></div><p class="muted">${health.ai ? "Claude is writing the assessment from the evidence below…" : "Building summary…"}</p></div>`}
    ${a && !a.error ? risksAndActions(a, ev) : ""}
    ${airportsStrip(b)}
    ${evidenceSection(b)}
    ${sourcesSection(b, a)}`;

  document.querySelectorAll("[data-ev]").forEach((chip) =>
    chip.addEventListener("click", () => focusEvidence(chip.dataset.ev)));
}

function verdict(a, b) {
  if (a.error) {
    return `<div class="card verdict lvl-${b.rule_assessment.level}">
      <div class="lvl">${LEVEL_TEXT[b.rule_assessment.level]} <span class="tag">rule engine</span></div>
      <p class="warnline">${esc(a.error)} Showing the rule-based level and raw evidence only.</p></div>`;
  }
  const v = a.validation ?? { issues: [] };
  return `<div class="card verdict lvl-${a.overall_risk}">
    <div class="verdict-top">
      <div class="lvl">${LEVEL_TEXT[a.overall_risk]}</div>
      <div class="conf conf-${a.confidence}" title="${esc(a.confidence_explanation)}">Confidence: ${esc(a.confidence)}</div>
    </div>
    <h2>${esc(a.headline)}</h2>
    <p class="muted small">${esc(a.confidence_explanation)}</p>
    <div class="meta small">
      ${a.generated_by === "claude" ? `Written by Claude (${esc(a.model)}) from ${b.evidence.length} evidence items` : esc(a.ai_note)}
      · Rule engine baseline: <b>${esc(b.rule_assessment.level)}</b>
    </div>
    ${a.disagreement_with_rules ? `<p class="small"><b>Why this differs from the rule engine:</b> ${esc(a.disagreement_with_rules)}</p>` : ""}
    ${v.issues.length ? `<ul class="issues">${v.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}
  </div>`;
}

const chips = (ids, ev) => ids.map((id) => `<button class="chip sev-${ev[id]?.severity_label}" data-ev="${esc(id)}" title="${esc(ev[id]?.source + ": " + ev[id]?.title)}">${esc(id)}</button>`).join("");

function risksAndActions(a, ev) {
  const risks = a.risks.length ? a.risks.map((r) => `
    <li class="risk">
      <div class="risk-top"><span class="pill sev-${esc(r.severity)}">${esc(r.severity)}</span>
        <b>${esc(r.title)}</b></div>
      <div class="small muted">${esc(r.airport)} · ${esc(r.phase)} · ${esc(r.timing)} · likelihood: ${esc(r.likelihood)}</div>
      <p>${esc(r.explanation)}</p>
      <div class="cites"><span class="basis basis-${esc(r.basis)}">${BASIS_TEXT[r.basis] ?? esc(r.basis)}</span>
        ${r.uncited ? `<span class="pill sev-high">unverified — no evidence cited</span>` : chips(r.evidence_ids, ev)}</div>
    </li>`).join("") : `<li class="muted">No meaningful risks identified.</li>`;

  const actions = a.actions.map((x) => `
    <li><span class="prio prio-${esc(x.priority.replace(/\s/g, "-"))}">${esc(x.priority)}</span>
      <div><b>${esc(x.action)}</b><div class="small muted">${esc(x.reason)}</div><div class="cites">${chips(x.evidence_ids, ev)}</div></div></li>`).join("");

  return `<div class="two-col">
    <section class="card"><h3>Risks</h3><ul class="risks">${risks}</ul></section>
    <section class="card"><h3>What to do</h3><ul class="actions">${actions}</ul>
      ${a.alternates_note ? `<h4>Nearby airports</h4><p class="small">${esc(a.alternates_note)}</p>` : ""}
      ${a.what_would_change ? `<h4>What would change this</h4><p class="small">${esc(a.what_would_change)}</p>` : ""}
    </section></div>`;
}

function airportsStrip(b) {
  return `<section class="airports">${b.airports.map((ap) => `
    <div class="ap ${ap.role.startsWith("alternate") ? "alt" : ""} lvl-${ap.rule_level}">
      <div class="small muted">${esc(ROLE_TEXT[ap.role] ?? ap.role)}</div>
      <div class="ap-code">${esc(ap.iata)}</div>
      <div class="small">${esc(ap.city)}, ${esc(ap.state)}</div>
      <div class="ap-lvl">${LEVEL_TEXT[ap.rule_level]}</div>
      ${ap.compound ? `<div class="tiny muted" title="${esc(ap.compound)}">compound hazards</div>` : ""}
    </div>`).join("")}</section>`;
}

function evidenceSection(b) {
  const groups = b.airports.map((ap) => {
    const rows = b.evidence.filter((e) => e.airport === ap.iata);
    return `<details class="ev-group" ${ap.role.startsWith("alternate") ? "" : "open"} data-ap="${esc(ap.iata)}">
      <summary><b>${esc(ap.iata)}</b> · ${esc(ROLE_TEXT[ap.role] ?? ap.role)} · ${rows.length} items</summary>
      <table class="ev"><tbody>${rows.map((e) => `
        <tr id="ev-${esc(e.id)}">
          <td class="id">${esc(e.id)}</td>
          <td><span class="pill sev-${esc(e.severity_label)}">${esc(e.severity_label)}</span></td>
          <td><div><b>${esc(e.title)}</b></div>
            <div class="small detail">${esc(e.detail)}</div>
            ${e.window ? `<div class="tiny muted">Valid ${esc(e.window.from)} → ${esc(e.window.to)}</div>` : ""}</td>
          <td class="small src">${esc(e.source)}<div class="tiny muted">${BASIS_TEXT[e.basis] ?? esc(e.basis)}</div>
            <a class="tiny" href="${esc(e.url)}" target="_blank" rel="noopener">source ↗</a></td>
        </tr>`).join("")}</tbody></table></details>`;
  });
  return `<section class="card"><h3>Evidence</h3>
    <p class="small muted">Every item below came from a live API call for this check. Severity is assigned by transparent rules; click an ID chip above to jump here.</p>
    ${groups.join("")}</section>`;
}

function sourcesSection(b, a) {
  return `<section class="card"><h3>Sources & coverage</h3>
    <table class="sources"><thead><tr><th>Source</th><th>Status</th><th>Time</th><th>Items</th><th>Notes</th></tr></thead><tbody>
    ${b.sources.map((s) => `<tr><td><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.name)}</a></td>
      <td><span class="dot ${s.status}"></span>${esc(s.status)}</td><td>${s.ms} ms</td><td>${s.evidence_count}</td>
      <td class="small">${esc(s.error ?? s.note ?? "")}</td></tr>`).join("")}
    ${a?.generated_by === "claude" ? `<tr><td>Claude (${esc(a.model)})</td><td><span class="dot ok"></span>ok</td><td>${a.ms} ms</td><td>—</td><td class="small">${a.usage.input} input / ${a.usage.output} output tokens</td></tr>` : ""}
    </tbody></table>
    <h4>Known limitations</h4><ul class="small">${b.limitations.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
    <p class="tiny muted">Retrieved ${esc(new Date(b.created_at).toLocaleString())}</p></section>`;
}

function focusEvidence(id) {
  const row = document.getElementById(`ev-${id}`);
  if (!row) return;
  row.closest("details").open = true;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 1600);
}

function fmtDate(d) {
  return new Date(d + "T12:00:00").toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" });
}
