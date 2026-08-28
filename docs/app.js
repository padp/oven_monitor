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

/* --- rendering -------------------------------------------------- */

let latest = null;

function renderStatus(data) {
  const s = data.sample || {};
  const badge = document.getElementById("state-badge");
  const state = s.state || "UNKNOWN";
  badge.textContent = state;
  badge.className = "badge " + state;
  document.getElementById("oven-name").textContent = data.oven.name;
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

function burnerTile(label, flameOn, pilotOn) {
  const value = flameOn === true ? "ON" : flameOn === false ? "OFF" : "&ndash;";
  const sub = flameOn === false && pilotOn === true ? "pilot lit" : "";
  return [label, value, sub];
}

// The large oven has no direct flame-on bit - only burner motor output
// (zone1_burner/zone2_burner). Same threshold Detector._equipment_active()
// already uses to decide RUNNING for that oven: nonzero means firing.
function burnerOn(f, flameField, mtrField) {
  const flame = f.get(flameField);
  if (flame === true || flame === false) return flame;
  const mtr = f.get(mtrField);
  return typeof mtr === "number" ? mtr > 0 : null;
}

function doorsTile(s) {
  const e = s.entrance_door_closed, x = s.exit_door_closed;
  if (e === null || e === undefined || x === null || x === undefined) {
    return ["Doors", "&ndash;"];
  }
  if (e === x) {
    return ["Doors", e ? "CLOSED" : "OPEN"];
  }
  return ["Doors", `entrance ${e ? "closed" : "open"}, exit ${x ? "closed" : "open"}`];
}

function renderTiles(data) {
  const s = data.sample || {};
  const f = new Map((data.fields || []).map((x) => [x.field, x.value]));
  const tiles = [
    ["Zone 1", fmtNum(s.zone1_temp, 1, "°F"), `setpoint ${fmtNum(s.setpoint, 0, "°F")}`],
    ["Zone 2", fmtNum(s.zone2_temp, 1, "°F")],
    ["Load temp", fmtNum(s.load_temp_mean, 1, "°F"),
      s.load_temp_valid_count
        ? `${fmtNum(s.load_temp_min, 0)}–${fmtNum(s.load_temp_max, 0)} across ${s.load_temp_valid_count} probes`
        : "no valid probes"],
    burnerTile("Burner 1", burnerOn(f, "burner1_flame_on", "zone1_burner"), f.get("burner1_pilot_on")),
    burnerTile("Burner 2", burnerOn(f, "burner2_flame_on", "zone2_burner"), f.get("burner2_pilot_on")),
    // Mirrors the state badge above (Detector's actual RUNNING logic), not
    // the raw cycle_active field - that field only exists on the small
    // oven, so reading it directly always read "not running" on the large
    // one regardless of the real state.
    ["Cycle", s.state === "RUNNING" ? "RUNNING" : "not running",
      typeof s.cycle_time_left_min === "number"
        ? `load time ${(s.cycle_time_left_min / 60).toFixed(1)}h` : ""],
    ["Time Remaining",
      typeof s.cycle_time_remaining_computed_min === "number"
        ? fmtDuration(s.cycle_time_remaining_computed_min * 60) : "&ndash;"],
    ["Exhaust", fmtNum(s.exhaust_fan_active, 0, " Hz")],
    doorsTile(s),
    ["Program #", f.has("recipe_number") ? String(f.get("recipe_number")) : "&ndash;"],
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

/* --- temperature trend chart ------------------------------------ */
/* Hand-rolled SVG, deliberately - this dashboard has zero external
 * dependencies (no CDN scripts, no charting library) everywhere else, and
 * a simple two-line time series does not need one. */

function fmtChartTime(ms) {
  const d = new Date(ms);
  return d.toLocaleString(undefined, { month: "numeric", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function renderTempChart(samples) {
  const el = document.getElementById("temp-chart-container");
  const withTemp = (samples || []).filter((s) =>
    typeof s.zone1_temp === "number" || typeof s.zone2_temp === "number");
  if (withTemp.length < 2) {
    el.innerHTML = '<div class="attention-detail">Not enough data in this window to plot.</div>';
    return;
  }

  const W = Math.max(el.clientWidth || 0, 480);
  const H = 260;
  const padL = 46, padR = 12, padT = 10, padB = 26;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const times = withTemp.map((s) => new Date(s.ts).getTime());
  const t0 = times[0], t1 = times[times.length - 1];
  const tSpan = Math.max(t1 - t0, 1);

  const vals = [];
  for (const s of withTemp) {
    if (typeof s.zone1_temp === "number") vals.push(s.zone1_temp);
    if (typeof s.zone2_temp === "number") vals.push(s.zone2_temp);
  }
  const vMin = Math.min(...vals), vMax = Math.max(...vals);
  const vPad = (vMax - vMin) * 0.1 || 10;
  const yMin = vMin - vPad, yMax = vMax + vPad;

  const x = (t) => padL + ((t - t0) / tSpan) * plotW;
  const y = (v) => padT + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  function pathFor(key) {
    let d = "", started = false;
    withTemp.forEach((s, i) => {
      const v = s[key];
      if (typeof v !== "number") { started = false; return; }
      d += (started ? "L" : "M") + x(times[i]).toFixed(1) + "," + y(v).toFixed(1) + " ";
      started = true;
    });
    return d.trim();
  }

  const ySteps = 4;
  let gridlines = "", yLabels = "";
  for (let i = 0; i <= ySteps; i++) {
    const v = yMin + (yMax - yMin) * (i / ySteps);
    const py = y(v).toFixed(1);
    gridlines += `<line x1="${padL}" y1="${py}" x2="${W - padR}" y2="${py}" class="chart-grid" />`;
    yLabels += `<text x="${padL - 6}" y="${(y(v) + 4).toFixed(1)}" class="chart-axis-label" text-anchor="end">${Math.round(v)}&deg;</text>`;
  }

  let xLabels = "";
  [0, 0.5, 1].forEach((f, i) => {
    const t = t0 + tSpan * f;
    const anchor = i === 0 ? "start" : i === 2 ? "end" : "middle";
    xLabels += `<text x="${x(t).toFixed(1)}" y="${H - 6}" class="chart-axis-label" text-anchor="${anchor}">${fmtChartTime(t)}</text>`;
  });

  el.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" class="temp-chart">
      ${gridlines}${yLabels}${xLabels}
      <path d="${pathFor("zone1_temp")}" class="chart-line chart-line-zone1" fill="none" />
      <path d="${pathFor("zone2_temp")}" class="chart-line chart-line-zone2" fill="none" />
    </svg>
    <div class="chart-legend">
      <span class="chart-legend-item"><span class="chart-swatch chart-swatch-zone1"></span>Zone 1</span>
      <span class="chart-legend-item"><span class="chart-swatch chart-swatch-zone2"></span>Zone 2</span>
    </div>`;
}

async function fetchAndRenderChart() {
  const base = apiBase();
  const oven = selectedOven();
  const mode = document.getElementById("chart-mode").value;
  const label = document.getElementById("chart-range-label");

  let url;
  if (mode === "live") {
    url = `${base}/api/oven/${oven}/history?hours=6`;
    label.textContent = "";
  } else {
    const opt = document.getElementById("chart-mode").selectedOptions[0];
    const start = opt.dataset.start, end = opt.dataset.end;
    url = `${base}/api/oven/${oven}/history?start=${encodeURIComponent(start)}` +
      (end ? `&end=${encodeURIComponent(end)}` : "") + "&limit=5000";
    label.textContent = end
      ? `${fmtChartTime(new Date(start).getTime())} – ${fmtChartTime(new Date(end).getTime())}`
      : `from ${fmtChartTime(new Date(start).getTime())} (load still open)`;
  }

  try {
    const resp = await fetch(url).then((r) => r.json());
    renderTempChart(resp.samples || []);
  } catch (e) {
    document.getElementById("temp-chart-container").innerHTML =
      '<div class="attention-detail">Could not load chart data.</div>';
  }
}

async function loadChartPicker() {
  const base = apiBase();
  const oven = selectedOven();
  const sel = document.getElementById("chart-mode");
  try {
    const resp = await fetch(`${base}/api/oven/${oven}/loads?limit=20`).then((r) => r.json());
    const loads = (resp.loads || []).filter((l) => l.actual_start_time);
    const options = ['<option value="live">Live &mdash; last 6h</option>'];
    for (const l of loads) {
      const partLabel = l.part_no ? ` – ${l.part_no}` : "";
      const statusLabel = l.furnace_load_status ? ` (${l.furnace_load_status})` : "";
      options.push(
        `<option value="${l.furnace_load_no}" data-start="${l.actual_start_time}" ` +
        `data-end="${l.actual_end_time || ""}">Load ${l.furnace_load_no}${partLabel}${statusLabel}</option>`);
    }
    sel.innerHTML = options.join("");
  } catch (e) {
    // Leave just "Live" - the picker is a nice-to-have, not required for
    // the chart itself to work.
  }
}

document.getElementById("chart-mode").addEventListener("change", fetchAndRenderChart);

function renderJob(jobs, ovenState) {
  const el = document.getElementById("job-content");
  jobs = jobs || [];
  if (!jobs.length) {
    el.innerHTML = '<div class="attention-detail">No Plex data yet - plex_sync.py has not reported for this oven.</div>';
    return;
  }

  // A load Plex itself has already closed out is not "current" in any
  // useful sense, confirmed or not - showing job details for it would
  // read as if something were still in the oven when nothing is.
  const active = jobs.filter((j) => j.furnace_load_status !== "Completed");
  if (!active.length) {
    const done = jobs.map((j) => j.furnace_load_no || "?").join(", ");
    el.innerHTML = `<div class="attention-detail">No active job - the last known load(s) ` +
      `(Furnace Load ${done}) are already Completed.</div>`;
    return;
  }

  // Almost always one job. The dual-program workaround (see collector/
  // plex.py's get_current_loads()) can leave two loads simultaneously
  // Started for the same oven - both are real and both get shown, each
  // in its own block, rather than picking one.
  const note = active.length > 1
    ? `<div class="banner" style="margin-bottom:0.6rem;">${active.length} loads are ` +
      `simultaneously Started for this oven - likely the dual-program workaround ` +
      `("${active[0].operation_code || "?"}"). All are shown below.</div>`
    : "";
  el.innerHTML = note + active.map((job) => renderOneJob(job, ovenState)).join(
    '<hr style="margin: 1rem 0; border-color: var(--color-border);">'
  );
}

function renderOneJob(job, ovenState) {
  let badge;
  if (job.confirmed) {
    badge = '<span class="flag ok">Plex-confirmed</span>';
  } else if (ovenState !== "RUNNING") {
    badge = '<span class="flag">Unconfirmed - staged for next load</span>';
  } else {
    badge = '<span class="flag">unconfirmed guess</span>';
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

  return `
    <div class="status-row" style="margin-bottom: 0.75rem;">
      ${badge}
      <div style="font-weight: 700; font-size: 16px;">${part}</div>
    </div>
    <div class="tile-grid">
      <div class="tile"><div class="tile-label">Job No</div><div class="tile-value">${job.job_no || "&ndash;"}</div></div>
      <div class="tile"><div class="tile-label">Quantity</div><div class="tile-value">${fmtNum(job.quantity, 0)}</div></div>
      <div class="tile"><div class="tile-label">Furnace Load</div><div class="tile-value">${job.furnace_load_no || "&ndash;"}</div><div class="tile-sub">${job.furnace_load_status || ""}</div></div>
      <div class="tile"><div class="tile-label">Target Temp</div><div class="tile-value">${fmtNum(job.temperature, 0, "°F")}</div></div>
      <div class="tile"><div class="tile-label">Program #</div><div class="tile-value">${job.program_number != null ? job.program_number : "&ndash;"}</div></div>
    </div>
    ${staleNote}
    ${containers.length > 1 ? `
    <div class="overflow-wrap" style="margin-top: 0.9rem;">
      <table>
        <thead><tr><th>Serial No</th><th>Job No</th><th>Part No</th><th>Part Name</th><th>Qty</th></tr></thead>
        <tbody>${containerRows}</tbody>
      </table>
    </div>` : ""}`;
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
    renderTiles(cur);
    renderProbes(cur);
    renderTags(cur);
    renderStates(st);
    renderJob(jobResp.loads, (cur.sample || {}).state);
    fetchAndRenderChart();  // independent of the main render; never blocks it
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
loadChartPicker();  // once - re-populating on every refresh tick would keep
                    // resetting a selected historical load back to "Live"
refresh();
setInterval(refresh, REFRESH_MS);
