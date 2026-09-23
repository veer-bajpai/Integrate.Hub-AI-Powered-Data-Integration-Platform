const state = {
  token: localStorage.getItem("integratehub_token"),
  dashboard: null,
  records: [],
  view: "overview",
};
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const api = async (path, options = {}) => {
  const headers = {
    ...(options.body instanceof FormData
      ? {}
      : { "Content-Type": "application/json" }),
    ...(options.headers || {}),
  };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "Something went wrong");
  return data;
};
const toast = (message, error = false) => {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  window.setTimeout(() => (element.className = "toast"), 3200);
};
const formatDate = (value) =>
  value
    ? new Date(value).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
    : "Never";
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>'"]/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[
        character
      ],
  );
const icons = () =>
  window.lucide?.createIcons({ attrs: { "stroke-width": 1.7 } });

function showApp() {
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#today-label").textContent = new Date()
    .toLocaleDateString([], {
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    })
    .toUpperCase();
  loadWorkspace();
}
function showAuth() {
  $("#auth-view").classList.remove("hidden");
  $("#app-view").classList.add("hidden");
}
function initials(name) {
  return name
    .split(" ")
    .map((part) => part[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
}
function navigate(view) {
  state.view = view;
  $$(".view").forEach((element) =>
    element.classList.toggle("hidden", element.id !== `view-${view}`),
  );
  $$(".nav-item[data-view]").forEach((item) =>
    item.classList.toggle("active", item.dataset.view === view),
  );
  $("#page-title").textContent =
    view === "integrations"
      ? "App connections"
      : view[0].toUpperCase() + view.slice(1);
  $("#sidebar").classList.remove("open");
  if (view === "records") loadRecords();
  if (view === "documents") loadDocuments();
  if (view === "integrations") loadIntegrations();
  icons();
}
function renderMetrics(metrics, user) {
  $("#metric-records").textContent = metrics.records.toLocaleString();
  $("#metric-connectors").textContent = metrics.connectors;
  $("#metric-duplicates").textContent = metrics.duplicates;
  $("#metric-documents").textContent = metrics.documents;
  $("#user-name").textContent = user.name;
  $("#greeting-name").textContent = user.name.split(" ")[0];
  $("#user-avatar").textContent = initials(user.name);
  $("#top-avatar").textContent = initials(user.name);
}
function renderHealth(connectors) {
  $("#connector-count").textContent = connectors.length;
  $("#connector-health").innerHTML = connectors.length
    ? connectors
        .slice(0, 4)
        .map(
          (connector) =>
            `<div class="health-row"><span class="health-symbol">${escapeHtml(connector.kind === "webhook" ? "WH" : connector.kind === "csv" ? "CSV" : "API")}</span><div class="health-main"><strong>${escapeHtml(connector.name)}</strong><small>Last sync ${formatDate(connector.last_sync)}</small></div><span class="health-rate">${Number(connector.success_rate || 0).toFixed(1)}%</span></div>`,
        )
        .join("")
    : '<div class="empty-state">No connectors yet. Start with a source above.</div>';
}
function renderActivity(logs) {
  $("#activity-list").innerHTML = logs.length
    ? logs
        .map(
          (log) =>
            `<div class="activity-row"><span class="activity-dot ${escapeHtml(log.status)}"></span><div><strong>${escapeHtml(log.message)}</strong><small>${formatDate(log.created_at)}</small></div></div>`,
        )
        .join("")
    : '<div class="empty-state">Your ingestion activity will appear here.</div>';
}
async function loadWorkspace() {
  try {
    state.dashboard = await api("/api/dashboard");
    renderMetrics(state.dashboard.metrics, state.dashboard.user);
    renderHealth(state.dashboard.connectors);
    renderActivity(state.dashboard.logs);
    await loadConnectors();
  } catch (error) {
    toast(error.message, true);
    if (error.message.includes("Authentication")) {
      state.token = null;
      localStorage.removeItem("integratehub_token");
      showAuth();
    }
  }
  icons();
}
async function loadConnectors() {
  const connectors = await api("/api/connectors");
  $("#connector-count").textContent = connectors.length;
  $("#connectors-table").innerHTML =
    `<div class="data-head"><span>NAME</span><span>TYPE</span><span>RECORDS</span><span>HEALTH</span><span>LAST SYNC</span></div>${connectors.length ? connectors.map((connector) => `<div class="data-row"><strong>${escapeHtml(connector.name)}</strong><span class="type-tag">${escapeHtml(connector.kind)}</span><span>${Number(connector.records || 0).toLocaleString()}</span><span class="${connector.status === "healthy" ? "status-pill healthy" : "status-pill"}">${Number(connector.success_rate || 0).toFixed(1)}%</span><span>${formatDate(connector.last_sync)} ${connector.kind === "rest" ? `<button class="text-button sync-connector" data-id="${connector.id}">Sync <i data-lucide="arrow-up-right"></i></button>` : ""}</span></div>`).join("") : '<div class="empty-state">No connectors yet. Create your first source to start ingesting.</div>'}</div>`;
  $$(".sync-connector").forEach((button) =>
    button.addEventListener("click", () => syncConnector(button)),
  );
  icons();
}
async function syncConnector(button) {
  button.disabled = true;
  try {
    const result = await api(`/api/connectors/${button.dataset.id}/sync`, {
      method: "POST",
    });
    toast(`Sync complete: ${result.imported} records imported`);
    await loadWorkspace();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}
async function loadRecords() {
  try {
    state.records = await api("/api/records");
    renderRecords(state.records);
  } catch (error) {
    toast(error.message, true);
  }
}
function renderRecords(records) {
  const rows = records
    .map((record) => {
      const data = JSON.parse(record.data);
      return `<div class="data-row"><div><strong>${escapeHtml(data.name || data.first_name || "Unnamed record")}</strong><small>${escapeHtml(data.email || "No email")} · ${escapeHtml(data.company || "Unknown company")}</small></div><span class="source-tag">${escapeHtml(record.source)}</span><span class="status-pill ${record.status === "valid" ? "healthy" : ""}">${escapeHtml(record.status)}</span><span>${formatDate(record.created_at)}</span></div>`;
    })
    .join("");
  $("#records-summary").textContent =
    `${records.length} normalized record${records.length === 1 ? "" : "s"}`;
  $("#records-table").innerHTML = rows
    ? `<div class="data-head"><span>RECORD</span><span>SOURCE</span><span>STATUS</span><span>CREATED</span></div>${rows}`
    : '<div class="empty-state">No matching records found.</div>';
}
async function loadDocuments() {
  try {
    const documents = await api("/api/documents");
    $("#documents-list").innerHTML = documents.length
      ? `<div class="data-head"><span>NAME</span><span>SIZE</span><span>ADDED</span><span></span></div>${documents.map((document) => `<div class="data-row"><strong>${escapeHtml(document.name)}</strong><span>${(document.size / 1024).toFixed(1)} KB</span><span>${formatDate(document.created_at)}</span><a class="text-button" href="/api/documents/${document.id}/download" target="_blank">Download <i data-lucide="arrow-up-right"></i></a></div>`).join("")}`
      : '<div class="empty-state">No documents yet. Upload a payload example or client brief.</div>';
  } catch (error) {
    toast(error.message, true);
  }
}
const integrationLogos = {
  Slack: "/assets/logos/slack.svg",
  HubSpot: "/assets/logos/hubspot.svg",
  "Google Sheets": "/assets/logos/google-sheets.svg",
  "REST API": "/assets/logos/rest-api.svg",
};
async function loadIntegrations() {
  try {
    const integrations = await api("/api/integrations");
    $("#integrations-grid").innerHTML = integrations
      .map((app) => {
        const connected = app.status === "connected";
        const provider =
          app.name === "Slack"
            ? "slack"
            : app.name === "HubSpot"
              ? "hubspot"
              : app.name === "Google Sheets"
                ? "google"
                : null;
        return `<article class="integration-card"><span class="integration-icon"><img src="${integrationLogos[app.name] || integrationLogos["REST API"]}" alt="${escapeHtml(app.name)} logo" /></span><p class="kicker">${escapeHtml(app.category)}</p><h2>${escapeHtml(app.name)}</h2><p>${escapeHtml(app.description)}</p><button class="button ${connected ? "button-secondary" : "button-primary"} integration-action" data-id="${app.id}" data-action="${connected ? "disconnect" : provider ? "oauth" : "connect"}">${connected ? "Disconnect" : provider ? "Connect account" : "Configure endpoint"} <i data-lucide="arrow-up-right"></i></button></article>`;
      })
      .join("");
    $$(".integration-action").forEach((button) =>
      button.addEventListener("click", () => updateIntegration(button)),
    );
    icons();
  } catch (error) {
    toast(error.message, true);
  }
}
async function updateIntegration(button) {
  try {
    if (button.dataset.action === "oauth") {
      const result = await api(
        `/api/integrations/${button.dataset.id}/oauth/start`,
      );
      window.location.assign(result.url);
      return;
    }
    await api(`/api/integrations/${button.dataset.id}`, {
      method: "POST",
      body: JSON.stringify({ action: button.dataset.action }),
    });
    toast(
      button.dataset.action === "connect"
        ? "Connection enabled"
        : "Connection paused",
    );
    loadIntegrations();
  } catch (error) {
    toast(error.message, true);
  }
}
async function createConnector(event) {
  event.preventDefault();
  const kind = $("#connector-kind").value;
  try {
    await api("/api/connectors", {
      method: "POST",
      body: JSON.stringify({
        name: $("#connector-name").value,
        kind,
        endpoint_url:
          kind === "rest" ? $("#connector-endpoint").value || null : null,
        secret: kind !== "csv" ? $("#connector-secret").value || null : null,
      }),
    });
    closeModal();
    toast("Connector configured");
    await loadWorkspace();
  } catch (error) {
    toast(error.message, true);
  }
}
function openModal() {
  $("#connector-modal").classList.remove("hidden");
  $("#connector-name").focus();
}
function closeModal() {
  $("#connector-modal").classList.add("hidden");
  $("#connector-form").reset();
}
async function uploadDocument(file) {
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    await api("/api/documents", { method: "POST", body });
    toast("Document added to your library");
    await loadWorkspace();
    if (state.view === "documents") loadDocuments();
  } catch (error) {
    toast(error.message, true);
  }
}

$$(".nav-item[data-view], [data-view-target]").forEach((item) =>
  item.addEventListener("click", () =>
    navigate(item.dataset.view || item.dataset.viewTarget),
  ),
);
$("#mobile-menu").addEventListener("click", () =>
  $("#sidebar").classList.toggle("open"),
);
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        email: $("#email").value,
        password: $("#password").value,
      }),
    });
    state.token = result.token;
    localStorage.setItem("integratehub_token", state.token);
    showApp();
  } catch (error) {
    toast(error.message, true);
  }
});
$("#show-register").addEventListener("click", () => {
  $("#register-form").classList.remove("hidden");
  $("#show-register").classList.add("hidden");
});
$("#register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await api("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({
        name: $("#register-name").value,
        email: $("#register-email").value,
        password: $("#register-password").value,
      }),
    });
    state.token = result.token;
    localStorage.setItem("integratehub_token", state.token);
    showApp();
  } catch (error) {
    toast(error.message, true);
  }
});
$("#logout").addEventListener("click", () => {
  state.token = null;
  localStorage.removeItem("integratehub_token");
  showAuth();
});
$("#new-connector").addEventListener("click", openModal);
$("#close-modal").addEventListener("click", closeModal);
$("#cancel-modal").addEventListener("click", closeModal);
$("#connector-form").addEventListener("submit", createConnector);
$("#connector-kind").addEventListener("change", () => {
  const csv = $("#connector-kind").value === "csv";
  $("#endpoint-field").classList.toggle("hidden", csv);
  $("#secret-field").classList.toggle("hidden", csv);
});
$("#suggest-mapping").addEventListener("click", async () => {
  const sample = $("#mapping-sample").value.trim();
  if (!sample) return toast("Paste a sample first", true);
  const button = $("#suggest-mapping");
  button.disabled = true;
  try {
    const result = await api("/api/connectors/suggest-mapping", {
      method: "POST",
      body: JSON.stringify({ sample }),
    });
    $("#mapping-provider").textContent = `Powered by ${result.provider}`;
    $("#mapping-result").classList.remove("hidden");
    $("#mapping-result").innerHTML = Object.entries(result.mapping)
      .map(
        ([field, value]) =>
          `<div><strong>${escapeHtml(field)}</strong><span>${escapeHtml(value.source || "No confident match")}</span><em>${Math.round((value.confidence || 0) * 100)}%</em></div>`,
      )
      .join("");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});
$("#record-search").addEventListener("input", (event) => {
  const query = event.target.value.toLowerCase();
  renderRecords(
    state.records.filter((record) => {
      const data = JSON.parse(record.data);
      return [record.source, record.status, ...Object.values(data)].some(
        (value) => String(value).toLowerCase().includes(query),
      );
    }),
  );
});
const documentInput = $("#document-input");
$("#upload-document").addEventListener("click", () => documentInput.click());
$("#browse-documents").addEventListener("click", () => documentInput.click());
$("#quick-upload").addEventListener("click", () => {
  navigate("documents");
  documentInput.click();
});
documentInput.addEventListener("change", () =>
  uploadDocument(documentInput.files[0]),
);
$("#refresh-dashboard").addEventListener("click", loadWorkspace);
$("#prompt-lucky").addEventListener("click", () => navigate("connectors"));
$("#prompt-action").addEventListener("click", () =>
  toast("Ask IntegrateHub from the assistant endpoint in your workspace API."),
);
icons();
if (state.token) showApp();
else showAuth();
