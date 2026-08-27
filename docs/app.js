/* Oven Monitor dashboard.
 *
 * Talks to api/app.py, deployed on Render per the naming convention
 * mirroring granco_monitor's granco-monitor.onrender.com. If the Render
 * service ends up with a different name, update API_DEFAULT to match -
 * or just open this page with ?api=<actual-url> once, which remembers it
 * in localStorage from then on.
 */

const API_DEFAULT = "https://oven-monitor.onrender.com";
const REFRESH_MS = 5000;

function apiBase() {
  const fromQuery = new URLSearchParams(location.search).get("api");
  if (fromQuery) {
    try { localStorage.setItem("ovenApiBase", fromQuery); } catch (e) { /* private mode */ }
    return fromQuery.replace(/\/$/, "");
  }
  try {
    const saved = localStorage.getItem("ovenApiBase");
    if (saved) return saved.replace(/\/$/, "");
  } catch (e) { /* private mode */ }
  return API_DEFAULT;
}

function selectedOven() {
  const fromQuery = new URLSearchParams(location.search).get("oven");
  if (fromQuery) return fromQuery;
  try { return localStorage.getItem("ovenId") || "small"; } catch (e) { return "small"; }
}

/* --- formatting ------------------------------------------------ */

function fmtNum(v, digits = 1, suffix = "") {
  if (v === null || v === undefined || typeof v !== "number") return "&ndash;";
  return v.toFixed(digits) + suffix;
}

function fmtValue(value, type) {
  if (value === null || value === undefined) return '<span class="v-null">null</span>';
  if (type === "bool" || typeof value === "boolean") {
    return value
      ? '<span class="v-true">true</span>'
      : '<span class="v-false">false</span>';
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  }
  return String(value);
}

function fmtAge(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 60) return Math.round(seconds) + "s ago";
  if (seconds < 3600) return Math.round(seconds / 60) + "m ago";
  return (seconds / 3600).toFixed(1) + "h ago";
}

function fmtDuration(seconds) {
  if (!seconds) return "0m";
  if (seconds < 3600) return Math.round(seconds / 60) + "m";
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

/* --- tag grouping ---------------------------------------------- */
/* Presentation only. The collector stores a flat canonical snapshot;
 * grouping here just makes 44 fields scannable. Anything not matched
 * falls into "Other" rather than being hidden. */

/* Order matters - first match wins. Faults lead deliberately, because
 * a name like burner1_high_temp_fault would otherwise be claimed by
 * Temperature on the word "temp" and a reader scanning for faults would
 * miss it. Cycle precedes Temperature for the same reason
 * (load_time_setpoint_tenths_hr is cycle config, not a temperature). */
const GROUPS = [
  ["Faults & alarms", /fault|alarm|failure|safeguard/i],
  ["Cycle", /cycle|step|recipe|load_tim/i],
  ["Temperature", /temp|setpoint/i],
  ["Burner & flame", /burner|flame|pilot|purg|pid/i],
  ["Doors", /door/i],
  ["Mode & power", /mode|auto|manual|power/i],
  ["Fans", /exhaust|fan/i],
];

function groupFor(field) {
  for (const [name, re] of GROUPS) if (re.test(field)) return name;
  return "Other";
}

/* --- actionable summary ---------------------------------------- */

function buildAttention(data, states) {
  const items = [];
  const s = data.sample || {};
  const fields = new Map((data.fields || []).map((f) => [f.field, f.value]));

  if (!data.oven.enabled) {
    items.push({ sev: "note", icon: "○", title: "This oven is not being polled",
      detail: "It is configured but disabled in collector/config.py." });
  }

  if (data.stale) {
    items.push({ sev: "warn", icon: "⚠", title: "Collector is not reporting",
      detail: `Newest sample is ${fmtAge(data.age_s)}. Expected every ${data.poll_interval_s || 30}s. ` +
              "Check that run_collector.py is running on the poller host." });
  }

  if (s.state === "FAULT") {
    items.push({ sev: "warn", icon: "⚠", title: "Oven reporting FAULT", detail: s.reason || "" });
  }

  const probes = data.load_temps || [];
  const bad = probes.filter((p) => !p.valid);
  if (bad.length) {
    items.push({ sev: "warn", icon: "⚠",
      title: `${bad.length} of ${probes.length} load thermocouples reading invalid`,
      detail: bad.map((p) => p.probe).join(", ") +
        " — pinned at 2192°F, which is type J full scale (no signal). " +
        "Excluded from the load temperature average." });
  }

  const suspect = (data.fields || []).filter((f) => f.polarity_unconfirmed);
  if (suspect.length) {
    items.push({ sev: "note", icon: "?", title: "Bit polarity still unconfirmed",
      detail: suspect.map((f) => `${f.field}=${f.value}`).join(", ") +
        " — these read as if wired normally-closed. Nothing depends on them yet; " +
        "watching a real cycle will settle it." });
  }

  // The specific trap that produced a false FAULT on the first live poll.
  if (fields.get("cycle_active") === false && typeof s.cycle_time_left_min === "number"
      && s.cycle_time_left_min > 0) {
    items.push({ sev: "note", icon: "ℹ",
      title: "Time remaining is a setpoint, not a countdown",
      detail: `cycle_time_left_min reads ${Math.round(s.cycle_time_left_min)} min but no cycle is ` +
        "running. On this oven that tag holds the operator-entered load time until a cycle starts." });
  }

  // Burner 2 signals, surfaced together because individually they are easy to miss.
  const b2 = [];
  if (fields.get("burner2_flame_failure") === true) b2.push("flame failure fault");
  if (fields.get("burner2_open_tc_fault") === true) b2.push("open thermocouple");
  if (fields.get("burner2_high_temp_fault") === true) b2.push("high temperature fault");
  if (b2.length) {
    items.push({ sev: "warn", icon: "⚠", title: "Burner 2 fault bits set", detail: b2.join(", ") });
  }

  if (!items.length) {
    items.push({ sev: "ok", icon: "✓", title: "Nothing needs attention",
      detail: "Collector reporting on schedule, no faults, all load probes valid." });
  }
  return items;
}

/* --- rendering -------------------------------------------------- */

let latest = null;

function renderStatus(data) {
  const s = data.sample || {};
  const badge = document.getElementById("state-badge");
  const state = s.state || "UNKNOWN";
  badge.textContent = state;
  badge.className = "badge " + state;
  document.getElementById("oven-name").textContent =
    `${data.oven.name} — ${data.oven.ip}`;
  document.getElementById("status-reason").textContent = s.reason || "No data yet.";

  const banner = document.getElementById("stale-banner");
  if (data.stale) {
    banner.textContent = `No fresh data — newest sample ${fmtAge(data.age_s)}.`;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
  document.getElementById("last-updated").textContent =
    s.ts ? `updated ${fmtAge(data.age_s)}` : "no data";
}

function renderAttention(items) {
  document.getElementById("attention-list").innerHTML = items.map((i) => `
    <div class="attention-item sev-${i.sev}">
      <span class="attention-icon">${i.icon}</span>
      <div>
        <div class="attention-title">${i.title}</div>
        ${i.detail ? `<div class="attention-detail">${i.detail}</div>` : ""}
      </div>
    </div>`).join("");
}

function burnerTile(label, flameOn, pilotOn) {
  const value = flameOn === true ? "ON" : flameOn === false ? "OFF" : "&ndash;";
  const sub = flameOn === false && pilotOn === true ? "pilot lit" : "";
  return [label, value, sub];
}

function doorsTile(s) {
  const e = s.entrance_door_closed, x = s.exit_door_closed;
  if (e === null || e === undefined || x === null || x === undefined) {
    return ["Doors", "&ndash;", "polarity-corrected - see footnote below"];
  }
  if (e === x) {
    return ["Doors", e ? "CLOSED" : "OPEN", "polarity-corrected - see footnote below"];
  }
  return ["Doors", `entrance ${e ? "closed" : "open"}, exit ${x ? "closed" : "open"}`,
    "polarity-corrected - see footnote below"];
}

function renderTiles(data) {
  const s = data.sample || {};
  const f = new Map((data.fields || []).map((x) => [x.field, x.value]));
  const tiles = [
    ["Zone 1", fmtNum(s.zone1_temp, 1, "°F"), `setpoint ${fmtNum(s.setpoint, 0, "°F")} - trust this one`],
    // Zone 2's own setpoint tag is not what the oven actually controls to -
    // confirmed 2026-08-27: Zone 2's real temperature tracks Zone 1's
    // setpoint, not its own (which read 305°F while Zone 2 was actually
    // sitting at 365°F, matching Zone 1's 365°F target). Showing Zone 2's
    // setpoint here would just be showing a number nothing is following.
    ["Zone 2", fmtNum(s.zone2_temp, 1, "°F"), "tracks Zone 1's setpoint, not its own"],
    ["Load temp", fmtNum(s.load_temp_mean, 1, "°F"),
      s.load_temp_valid_count
        ? `${fmtNum(s.load_temp_min, 0)}–${fmtNum(s.load_temp_max, 0)} across ${s.load_temp_valid_count} probes`
        : "no valid probes"],
    // On/off, not the raw SCALED_CONTROL_VARIABLE - that tag is not a 0-100
    // percentage (observed live at 599 and 1198, i.e. not %), and showing it
    // as one was actively misleading. MAIN_FLAME_ON is a clean, direct bool.
    // The raw number is still visible in the tag monitor table below.
    burnerTile("Burner 1", f.get("burner1_flame_on"), f.get("burner1_pilot_on")),
    burnerTile("Burner 2", f.get("burner2_flame_on"), f.get("burner2_pilot_on")),
    ["Cycle", f.get("cycle_active") ? "RUNNING" : "not running",
      typeof s.cycle_time_left_min === "number"
        ? `load time ${(s.cycle_time_left_min / 60).toFixed(1)}h` : ""],
    // Computed, not read from the PLC - HR_LOAD_TIME_LEFT_TO_MMI and
    // friends were confirmed live to never actually count down. This is
    // derived from the active recipe's target temp/ramp rate/soak time
    // plus the live actual temperature, gated to RUNNING only (an idle,
    // cooling oven's stale recipe fields would otherwise produce a
    // meaningless number - see api/cycle_time.py).
    ["Time Remaining",
      typeof s.cycle_time_remaining_computed_min === "number"
        ? fmtDuration(s.cycle_time_remaining_computed_min * 60) : "&ndash;",
      "computed from the recipe, not a PLC countdown"],
    ["Exhaust", fmtNum(s.exhaust_fan_active, 0, " Hz"), "VFD output frequency"],
    doorsTile(s),
  ];
  document.getElementById("tile-grid").innerHTML = tiles.map(([label, value, sub]) => `
    <div class="tile">
      <div class="tile-label">${label}</div>
      <div class="tile-value">${value}</div>
      ${sub ? `<div class="tile-sub">${sub}</div>` : ""}
    </div>`).join("");
}

function renderProbes(data) {
  const probes = data.load_temps || [];
  const card = document.getElementById("probe-card");
  if (!probes.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  document.getElementById("probe-grid").innerHTML = probes.map((p) => `
    <div class="probe ${p.valid ? "" : "invalid"}">
      <div class="probe-name">${p.probe.replace(/_/g, " ")}</div>
      <div class="probe-value">${p.valid ? fmtNum(p.value, 1, "°F") : "no signal"}</div>
    </div>`).join("");
  const bad = probes.filter((p) => !p.valid).length;
  document.getElementById("probe-note").innerHTML = bad
    ? `${bad} probe(s) pinned at 2192&deg;F &mdash; type J full scale, i.e. no signal. Excluded from the average.`
    : "All probes reporting.";
}

function renderJob(job, ovenState) {
  const el = document.getElementById("job-content");
  if (!job) {
    el.innerHTML = '<div class="attention-detail">No Plex data yet - plex_sync.py has not reported for this oven.</div>';
    return;
  }

  // A load Plex itself has already closed out is not "current" in any
  // useful sense, confirmed or not - showing job details for it would
  // read as if something were still in the oven when nothing is.
  if (job.furnace_load_status === "Completed") {
    el.innerHTML = `<div class="attention-detail">No active job - the last known load ` +
      `(Furnace Load ${job.furnace_load_no || "?"}) is already Completed.</div>`;
    return;
  }

  let badge, badgeNote;
  if (job.confirmed) {
    badge = '<span class="flag ok">Plex-confirmed</span>';
    badgeNote = "the operator actually toggled \"Started\" on this load in Plex.";
  } else if (ovenState !== "RUNNING") {
    badge = '<span class="flag">Unconfirmed - staged for next load</span>';
    badgeNote = "the oven is not currently RUNNING, so this load - not marked Started in " +
      "Plex - most likely is not physically in the oven yet; it reads as queued/prepped " +
      "for whenever it next runs.";
  } else {
    badge = '<span class="flag">unconfirmed guess</span>';
    badgeNote = "the oven IS running but nothing is marked Started in Plex, so this is the " +
      "most-recently-started load instead - it may genuinely be what's running, but the " +
      "operator has not confirmed it in Plex.";
  }
  const staleNote = job.stale
    ? `<div class="banner" style="margin-top:0.6rem;">Plex data last refreshed ${fmtAge(job.age_s)} - showing the last known job.</div>`
    : "";

  const part = job.part_no
    ? `${job.part_no}${job.part_name ? " &mdash; " + job.part_name : ""}`
    : "&ndash;";

  const containers = job.containers || [];
  const containerRows = containers.map((c) => `
    <tr>
      <td class="tag-name">${c.serial_no || "&ndash;"}</td>
      <td>${c.job_no || "&ndash;"}</td>
      <td>${c.part_no || "&ndash;"}</td>
      <td>${c.part_name || "&ndash;"}</td>
      <td class="value">${fmtNum(c.quantity, 0)}</td>
    </tr>`).join("");

  el.innerHTML = `
    <div class="status-row" style="margin-bottom: 0.75rem;">
      ${badge}
      <div style="font-weight: 700; font-size: 16px;">${part}</div>
    </div>
    <div class="tile-grid">
      <div class="tile"><div class="tile-label">Job No</div><div class="tile-value">${job.job_no || "&ndash;"}</div></div>
      <div class="tile"><div class="tile-label">Quantity</div><div class="tile-value">${fmtNum(job.quantity, 0)}</div></div>
      <div class="tile"><div class="tile-label">Furnace Load</div><div class="tile-value">${job.furnace_load_no || "&ndash;"}</div><div class="tile-sub">${job.furnace_load_status || ""}</div></div>
      <div class="tile"><div class="tile-label">Target Temp</div><div class="tile-value">${fmtNum(job.temperature, 0, "°F")}</div></div>
    </div>
    ${staleNote}
    ${containers.length > 1 ? `
    <div class="overflow-wrap" style="margin-top: 0.9rem;">
      <table>
        <thead><tr><th>Serial No</th><th>Job No</th><th>Part No</th><th>Part Name</th><th>Qty</th></tr></thead>
        <tbody>${containerRows}</tbody>
      </table>
    </div>` : ""}
    <p class="footnote">${badge} &mdash; ${badgeNote}</p>`;
}

function renderTags(data) {
  const filter = document.getElementById("tag-filter").value.trim().toLowerCase();
  const onlyFlagged = document.getElementById("only-flagged").checked;
  const grouped = document.getElementById("group-tags").checked;

  let fields = (data.fields || []).filter((f) => {
    if (onlyFlagged && !f.polarity_unconfirmed && f.value !== null) return false;
    if (!filter) return true;
    return f.field.toLowerCase().includes(filter)
      || (f.tag || "").toLowerCase().includes(filter);
  });

  const rowFor = (f) => `
    <tr>
      <td class="field-name">${f.field}</td>
      <td class="tag-name">${f.tag || "&ndash;"}</td>
      <td class="value">${fmtValue(f.value, f.type)}</td>
      <td>${f.polarity_unconfirmed ? '<span class="flag">polarity?</span>' : ""}</td>
    </tr>`;

  let html;
  if (grouped) {
    const buckets = new Map();
    for (const f of fields) {
      const g = groupFor(f.field);
      if (!buckets.has(g)) buckets.set(g, []);
      buckets.get(g).push(f);
    }
    const order = GROUPS.map((g) => g[0]).concat(["Other"]);
    html = order.filter((g) => buckets.has(g)).map((g) =>
      `<tr class="group-row"><td colspan="4">${g}</td></tr>` +
      buckets.get(g).map(rowFor).join("")).join("");
  } else {
    html = fields.map(rowFor).join("");
  }
  document.querySelector("#tag-table tbody").innerHTML =
    html || '<tr><td colspan="4" style="color: var(--color-text-muted);">No matching tags.</td></tr>';
}

function renderStates(states) {
  const totals = states.totals_s || {};
  const keys = Object.keys(totals).sort((a, b) => totals[b] - totals[a]);
  if (!keys.length) {
    document.getElementById("state-summary").innerHTML =
      '<div class="attention-detail">No state history yet.</div>';
    return;
  }
  document.getElementById("state-summary").innerHTML = `
    <div class="tile-grid">${keys.map((k) => `
      <div class="tile">
        <div class="tile-label">${k}</div>
        <div class="tile-value">${fmtDuration(totals[k])}</div>
        <div class="tile-sub">${(states.share_pct[k] || 0).toFixed(1)}% of observed</div>
      </div>`).join("")}</div>`;
}

/* --- polling ---------------------------------------------------- */

async function refresh() {
  const base = apiBase();
  const oven = selectedOven();
  try {
    const [cur, st, jobResp] = await Promise.all([
      fetch(`${base}/api/oven/${oven}/current`).then((r) => r.json()),
      fetch(`${base}/api/oven/${oven}/states?hours=24`).then((r) => r.json()),
      fetch(`${base}/api/oven/${oven}/job`).then((r) => r.json()),
    ]);
    if (cur.error) throw new Error(cur.error);
    latest = cur;
    renderStatus(cur);
    renderAttention(buildAttention(cur, st));
    renderTiles(cur);
    renderProbes(cur);
    renderTags(cur);
    renderStates(st);
    renderJob(jobResp.load, (cur.sample || {}).state);
  } catch (err) {
    document.getElementById("last-updated").textContent = "API unreachable";
    const banner = document.getElementById("stale-banner");
    banner.innerHTML = `Cannot reach the API at <code>${base}</code>. ` +
      "Start it with <code>python -m api.app</code>, or pass <code>?api=&lt;url&gt;</code>.";
    banner.classList.remove("hidden");
  }
}

async function initOvenPicker() {
  try {
    const res = await fetch(`${apiBase()}/api/ovens`).then((r) => r.json());
    const current = selectedOven();
    const wrap = document.getElementById("oven-picker-wrap");
    if (!res.ovens || res.ovens.length < 2) return;
    wrap.innerHTML = res.ovens.map((o) =>
      `<a href="?oven=${o.id}" class="app-header-subtitle" style="color:#fff; ${
        o.id === current ? "font-weight:700; text-decoration:underline;" : "opacity:0.75;"
      }">${o.name}${o.enabled ? "" : " (off)"}</a>`).join(" &middot; ");
  } catch (e) { /* header nicety only - the dashboard works without it */ }
}

for (const id of ["tag-filter", "only-flagged", "group-tags"]) {
  document.getElementById(id).addEventListener("input", () => { if (latest) renderTags(latest); });
}

try { localStorage.setItem("ovenId", selectedOven()); } catch (e) { /* private mode */ }
initOvenPicker();
refresh();
setInterval(refresh, REFRESH_MS);
