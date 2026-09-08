/* Oven Monitor - Alerts page.
 *
 * Same apiBase()/selectedOven() conventions as app.js (duplicated here
 * rather than shared - this dashboard has no module system, and every
 * page here is already self-contained). No login anywhere on this page:
 * this API has no account system at all (only /ingest is gated, by an
 * API key) - unlike granco_monitor's alerts.html, which requires an
 * account because that app already has real per-person accounts for
 * other writes.
 */

const API_DEFAULT = "https://oven-monitor.onrender.com";

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

const OVEN_ID = selectedOven();

const PRESET_TRIGGERS = [
  {
    key: "fault",
    label: "+ Fault Lock",
    trigger: { conditions: [{ field: "fault_lock", type: "bool", equals: true, mode: "becomes" }], sustained_s: 0 },
  },
  {
    key: "combustion-fault",
    label: "+ Combustion Fault",
    trigger: { conditions: [{ field: "combustion_fault_lock", type: "bool", equals: true, mode: "becomes" }], sustained_s: 0 },
  },
];

let TAGS = [];
let COMPARATORS = [];
let BOOL_MODES = [];
let RECIPIENT_EMAIL_DOMAIN = "";
let stagingTriggers = [];
let draftConditions = [emptyCondition()];

function emptyCondition() {
  return { field: "", type: "", comparator: "", threshold: "", equals: true, mode: "becomes" };
}

async function initOvenPicker() {
  try {
    const res = await fetch(`${apiBase()}/api/ovens`).then((r) => r.json());
    const wrap = document.getElementById("oven-picker-wrap");
    if (!res.ovens || res.ovens.length < 2) return;
    wrap.innerHTML = res.ovens.map((o) =>
      `<a href="?oven=${o.id}" class="app-header-subtitle" style="color:#fff; ${
        o.id === OVEN_ID ? "font-weight:700; text-decoration:underline;" : "opacity:0.75;"
      }">${o.name}${o.enabled ? "" : " (off)"}</a>`).join(" &middot; ");
  } catch (e) { /* header nicety only */ }
}

function renderConditionRows() {
  const container = document.getElementById("condition-rows");
  container.innerHTML = "";
  draftConditions.forEach((condition, index) => {
    const wrap = document.createElement("div");
    wrap.className = "condition-row-wrap";

    if (index > 0) {
      const and = document.createElement("span");
      and.className = "and-label";
      and.textContent = "AND";
      wrap.appendChild(and);
    }

    const row = document.createElement("div");
    row.className = "condition-row";

    const fieldSelect = document.createElement("select");
    fieldSelect.innerHTML = '<option value="">Choose a tag…</option>';
    for (const groupType of ["bool", "numeric", "string"]) {
      const groupTags = TAGS.filter((t) => t.type === groupType);
      if (!groupTags.length) continue;
      const label = groupType === "bool" ? "Boolean tags" : groupType === "numeric" ? "Numeric tags" : "Text tags";
      const optgroup = document.createElement("optgroup");
      optgroup.label = label;
      for (const t of groupTags) {
        const opt = document.createElement("option");
        opt.value = t.field;
        opt.textContent = t.field;
        if (t.field === condition.field) opt.selected = true;
        optgroup.appendChild(opt);
      }
      fieldSelect.appendChild(optgroup);
    }
    fieldSelect.addEventListener("change", () => {
      const tag = TAGS.find((t) => t.field === fieldSelect.value);
      if (!tag) {
        draftConditions[index] = emptyCondition();
      } else if (tag.type === "bool") {
        draftConditions[index] = { field: tag.field, type: "bool", equals: true, mode: "becomes" };
      } else if (tag.type === "string") {
        draftConditions[index] = { field: tag.field, type: "string", equals: "" };
      } else {
        draftConditions[index] = { field: tag.field, type: "numeric", comparator: (COMPARATORS[0] || {}).value || "<", threshold: "" };
      }
      renderConditionRows();
    });
    row.appendChild(fieldSelect);

    if (condition.type === "bool") {
      const modeSelect = document.createElement("select");
      for (const m of BOOL_MODES) {
        const opt = document.createElement("option");
        opt.value = m.value;
        opt.textContent = m.label;
        if (m.value === condition.mode) opt.selected = true;
        modeSelect.appendChild(opt);
      }
      modeSelect.addEventListener("change", () => { draftConditions[index].mode = modeSelect.value; });
      row.appendChild(modeSelect);

      const boolSelect = document.createElement("select");
      boolSelect.innerHTML = '<option value="1">True</option><option value="0">False</option>';
      boolSelect.value = condition.equals ? "1" : "0";
      boolSelect.addEventListener("change", () => { draftConditions[index].equals = boolSelect.value === "1"; });
      row.appendChild(boolSelect);
    } else if (condition.type === "string") {
      const textInput = document.createElement("input");
      textInput.type = "text";
      textInput.placeholder = "exact value";
      textInput.value = condition.equals || "";
      textInput.addEventListener("input", () => { draftConditions[index].equals = textInput.value; });
      row.appendChild(textInput);
    } else if (condition.type === "numeric") {
      const comparatorSelect = document.createElement("select");
      for (const c of COMPARATORS) {
        const opt = document.createElement("option");
        opt.value = c.value;
        opt.textContent = `${c.value} (${c.label})`;
        if (c.value === condition.comparator) opt.selected = true;
        comparatorSelect.appendChild(opt);
      }
      comparatorSelect.addEventListener("change", () => { draftConditions[index].comparator = comparatorSelect.value; });
      row.appendChild(comparatorSelect);

      const numberInput = document.createElement("input");
      numberInput.type = "number";
      numberInput.placeholder = "value";
      numberInput.value = condition.threshold;
      numberInput.addEventListener("input", () => { draftConditions[index].threshold = numberInput.value; });
      row.appendChild(numberInput);
    }

    wrap.appendChild(row);

    if (draftConditions.length > 1) {
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "btn btn-link condition-remove";
      removeBtn.title = "Remove this condition";
      removeBtn.textContent = "×";
      removeBtn.addEventListener("click", () => {
        draftConditions.splice(index, 1);
        renderConditionRows();
      });
      wrap.appendChild(removeBtn);
    }

    if (index === draftConditions.length - 1) {
      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "btn btn-secondary add-condition-button";
      addBtn.title = "Add another AND condition (compound alert)";
      addBtn.textContent = "+";
      addBtn.addEventListener("click", () => {
        draftConditions.push(emptyCondition());
        renderConditionRows();
      });
      wrap.appendChild(addBtn);
    }

    container.appendChild(wrap);
  });
}

function conditionIsComplete(c) {
  if (!c.field) return false;
  if (c.type === "bool") return true;
  if (c.type === "string") return Boolean(c.equals);
  if (c.type === "numeric") return c.threshold !== "" && !Number.isNaN(Number(c.threshold));
  return false;
}

function buildDraftConditions() {
  return draftConditions.map((c) =>
    c.type === "bool"
      ? { field: c.field, type: "bool", equals: c.equals, mode: c.mode || "becomes" }
      : c.type === "string"
        ? { field: c.field, type: "string", equals: c.equals }
        : { field: c.field, type: "numeric", comparator: c.comparator, threshold: Number(c.threshold) }
  );
}

document.getElementById("add-trigger-btn").addEventListener("click", async () => {
  if (!draftConditions.every(conditionIsComplete)) return;
  const durationValue = document.getElementById("duration-value").value;
  const durationUnit = Number(document.getElementById("duration-unit").value);
  const sustained_s = durationValue === "" ? 0 : Math.max(0, Number(durationValue) * durationUnit);
  await addStagingTrigger({ conditions: buildDraftConditions(), sustained_s });
});

async function addStagingTrigger(trigger) {
  let description = null;
  try {
    const res = await fetch(`${apiBase()}/api/oven/${OVEN_ID}/alerts/describe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trigger }),
    });
    const data = await res.json();
    if (!res.ok) {
      window.alert(data.error || "Could not describe that trigger.");
      return;
    }
    description = data.description;
  } catch (err) {
    window.alert(`Network error: ${err.message}`);
    return;
  }

  stagingTriggers.push({ ...trigger, description, _key: `${Date.now()}-${Math.random()}` });
  draftConditions = [emptyCondition()];
  document.getElementById("duration-value").value = "";
  renderConditionRows();
  renderStagingList();
}

function renderStagingList() {
  const list = document.getElementById("staging-list");
  list.innerHTML = "";
  for (const t of stagingTriggers) {
    const li = document.createElement("li");
    li.className = "trigger-list-item";
    const span = document.createElement("span");
    span.textContent = t.description;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "btn btn-link";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () => {
      stagingTriggers = stagingTriggers.filter((x) => x._key !== t._key);
      renderStagingList();
      updateCreateHint();
    });
    li.appendChild(span);
    li.appendChild(removeBtn);
    list.appendChild(li);
  }
  updateCreateHint();
}

function isValidRecipient(email) {
  if (!email || !RECIPIENT_EMAIL_DOMAIN) return false;
  return email.toLowerCase().endsWith(`@${RECIPIENT_EMAIL_DOMAIN.toLowerCase()}`);
}

function updateCreateHint() {
  const hint = document.getElementById("create-hint");
  const recipientEmail = document.getElementById("recipient-email").value.trim();
  if (stagingTriggers.length === 0) {
    hint.textContent = "Add at least one trigger above (a preset, or build one and click + Add Trigger) to enable this.";
  } else if (!isValidRecipient(recipientEmail)) {
    hint.textContent = `Enter a recipient email ending in @${RECIPIENT_EMAIL_DOMAIN} to enable this.`;
  } else {
    hint.textContent = "";
  }
}

document.getElementById("recipient-email").addEventListener("input", () => {
  document.getElementById("test-result").textContent = "";
  updateCreateHint();
});

document.getElementById("send-test-btn").addEventListener("click", async () => {
  const recipientEmail = document.getElementById("recipient-email").value.trim();
  const result = document.getElementById("test-result");
  if (!isValidRecipient(recipientEmail)) return;
  result.className = "test-result";
  result.textContent = "Sending…";
  try {
    const res = await fetch(`${apiBase()}/api/oven/${OVEN_ID}/alerts/test-webhook`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recipient_email: recipientEmail }),
    });
    const data = await res.json();
    if (!res.ok) {
      result.className = "test-result test-error";
      result.textContent = `✗ ${data.error || "Could not send."}`;
      return;
    }
    result.className = "test-result test-ok";
    result.textContent = "✓ Sent - check the Teams channel";
  } catch (err) {
    result.className = "test-result test-error";
    result.textContent = `✗ Network error: ${err.message}`;
  }
});

document.getElementById("create-alert-btn").addEventListener("click", async () => {
  const recipientEmail = document.getElementById("recipient-email").value.trim();
  if (stagingTriggers.length === 0 || !isValidRecipient(recipientEmail)) return;

  const btn = document.getElementById("create-alert-btn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Creating…';
  try {
    const res = await fetch(`${apiBase()}/api/oven/${OVEN_ID}/alerts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        label: document.getElementById("alert-label").value.trim() || undefined,
        repeat: document.getElementById("repeat-select").value,
        recipient_email: recipientEmail,
        triggers: stagingTriggers.map(({ _key, description, ...t }) => t),
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      window.alert(data.error || "Could not create alert.");
      return;
    }
    stagingTriggers = [];
    renderStagingList();
    document.getElementById("alert-label").value = "";
    document.getElementById("recipient-email").value = "";
    document.getElementById("repeat-select").value = "recurring";
    await loadExistingAlerts();
  } catch (err) {
    window.alert(`Network error: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Create Alert";
  }
});

function renderExistingAlerts(alertList) {
  const container = document.getElementById("existing-alerts");
  container.innerHTML = "";
  if (alertList.length === 0) {
    const p = document.createElement("p");
    p.className = "footnote";
    p.textContent = "No alerts created yet for this oven.";
    container.appendChild(p);
    return;
  }

  for (const rule of alertList) {
    const card = document.createElement("div");
    card.className = "alert-card";

    const header = document.createElement("div");
    header.className = "alert-card-header";

    const left = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = rule.label || "Untitled alert";
    left.appendChild(strong);
    if (rule.recipient_email) {
      const span = document.createElement("span");
      span.className = "footnote";
      span.textContent = ` · to ${rule.recipient_email}`;
      left.appendChild(span);
    }
    if (rule.repeat === "one_time") {
      const span = document.createElement("span");
      span.className = "footnote";
      span.textContent = " · one-time";
      left.appendChild(span);
    }
    header.appendChild(left);

    const right = document.createElement("div");
    const toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "btn btn-secondary";
    toggleBtn.textContent = rule.active ? "Disable" : "Enable";
    toggleBtn.addEventListener("click", () => toggleActive(rule));
    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "btn btn-secondary";
    deleteBtn.textContent = "Delete";
    deleteBtn.addEventListener("click", () => deleteRule(rule));
    right.appendChild(toggleBtn);
    right.appendChild(deleteBtn);
    header.appendChild(right);

    card.appendChild(header);

    if (!rule.active) {
      const p = document.createElement("p");
      p.className = "footnote";
      p.textContent = "Disabled — won't post to Teams.";
      card.appendChild(p);
    }

    const list = document.createElement("ul");
    list.className = "trigger-list";
    for (const t of rule.triggers) {
      const li = document.createElement("li");
      li.className = "trigger-list-item";
      const span = document.createElement("span");
      span.textContent = t.description;
      li.appendChild(span);
      list.appendChild(li);
    }
    card.appendChild(list);

    container.appendChild(card);
  }
}

async function toggleActive(rule) {
  try {
    const res = await fetch(`${apiBase()}/api/oven/${OVEN_ID}/alerts/${rule._id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: !rule.active }),
    });
    if (!res.ok) {
      window.alert("Could not update alert.");
      return;
    }
    await loadExistingAlerts();
  } catch (err) {
    window.alert(`Network error: ${err.message}`);
  }
}

async function deleteRule(rule) {
  if (!window.confirm(`Delete "${rule.label || "this alert"}"? Nothing will post to Teams for it anymore.`)) return;
  try {
    const res = await fetch(`${apiBase()}/api/oven/${OVEN_ID}/alerts/${rule._id}`, { method: "DELETE" });
    if (!res.ok) {
      window.alert("Could not delete alert.");
      return;
    }
    await loadExistingAlerts();
  } catch (err) {
    window.alert(`Network error: ${err.message}`);
  }
}

function renderPresetButtons() {
  const container = document.getElementById("preset-buttons");
  container.innerHTML = "";
  for (const preset of PRESET_TRIGGERS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-secondary";
    btn.textContent = preset.label;
    btn.addEventListener("click", () => addStagingTrigger(preset.trigger));
    container.appendChild(btn);
  }
}

async function loadExistingAlerts() {
  const res = await fetch(`${apiBase()}/api/oven/${OVEN_ID}/alerts`);
  const data = await res.json();
  renderExistingAlerts(data.alerts || []);
}

async function init() {
  try { localStorage.setItem("ovenId", OVEN_ID); } catch (e) { /* private mode */ }
  initOvenPicker();

  try {
    const res = await fetch(`${apiBase()}/api/oven/${OVEN_ID}/alerts/tags`);
    const data = await res.json();
    if (!res.ok) {
      document.getElementById("trigger-builder").outerHTML = `<p class="footnote">${data.error || "Could not load tags."}</p>`;
      return;
    }
    TAGS = data.tags || [];
    COMPARATORS = data.comparators || [];
    BOOL_MODES = data.bool_modes || [];
    RECIPIENT_EMAIL_DOMAIN = data.recipient_email_domain || "";
    document.getElementById("recipient-email").placeholder = RECIPIENT_EMAIL_DOMAIN
      ? `someone@${RECIPIENT_EMAIL_DOMAIN}`
      : "someone@company.com";
  } catch {
    // leave TAGS/COMPARATORS empty - the condition builder will just show no options
  }
  renderPresetButtons();
  renderConditionRows();
  renderStagingList();
  await loadExistingAlerts();
}

init();
