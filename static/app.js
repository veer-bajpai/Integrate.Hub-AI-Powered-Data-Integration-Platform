const state = {
  token: localStorage.getItem("integratehub_token"),
  theme: document.documentElement.dataset.theme || "light",
  currentWorkspaceId: localStorage.getItem("integratehub_workspace_id"),
  currentWorkspace: null,
  workspaces: [],
  dashboard: null,
  dashboardStats: null,
  records: [],
  savedViews: [],
  selectedRecordId: null,
  selectedSavedViewId: null,
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
  if (state.currentWorkspaceId)
    headers["X-Workspace-ID"] = state.currentWorkspaceId;
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

function applyTheme(theme) {
  state.theme = theme;
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("integratehub_theme", theme);
  const nextTheme = theme === "dark" ? "light" : "dark";
  $$(".theme-toggle").forEach((button) => {
    button.setAttribute("aria-label", `Switch to ${nextTheme} theme`);
    button.setAttribute("title", `Switch to ${nextTheme} theme`);
    button.setAttribute("aria-pressed", String(theme === "dark"));
    button.innerHTML = `<i data-lucide="${theme === "dark" ? "sun" : "moon"}"></i>`;
  });
  $("#theme-color-meta").content = theme === "dark" ? "#13151a" : "#f5f6fa";
  icons();
}

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
  const titles = {
    integrations: "App connections",
    team: "Team & Roles",
    audit: "Audit Log",
    automation: "AI & Automation",
  };
  $("#page-title").textContent =
    titles[view] || view[0].toUpperCase() + view.slice(1);
  $("#sidebar").classList.remove("open");
  if (view === "records") loadRecords();
  if (view === "documents") loadDocuments();
  if (view === "integrations") loadIntegrations();
  if (view === "team") loadTeam();
  if (view === "audit") loadAudit();
  if (view === "automation") loadAISettings();
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
  $("#user-role").textContent =
    state.currentWorkspace?.role?.replace(/^./, (letter) =>
      letter.toUpperCase(),
    ) || "Member";
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
            `<div class="activity-row"><span class="activity-dot ${escapeHtml(log.status || log.type)}"></span><div><strong>${log.actor_name ? `${escapeHtml(log.actor_name)} · ` : ""}${escapeHtml(log.summary || log.message)}</strong><small>${formatDate(log.created_at)}</small></div></div>`,
        )
        .join("")
    : '<div class="empty-state">Workspace activity will appear here.</div>';
}
function emptyDashboardStats() {
  const dateSeries = (days) => {
    const today = new Date();
    const todayUtc = Date.UTC(
      today.getUTCFullYear(),
      today.getUTCMonth(),
      today.getUTCDate(),
    );
    return Array.from({ length: days }, (_, index) => ({
      date: new Date(todayUtc - (days - index - 1) * 86400000)
        .toISOString()
        .slice(0, 10),
      count: 0,
    }));
  };
  return {
    records_by_day: dateSeries(14),
    records_by_source: ["csv", "webhook", "rest"].map((source) => ({
      source,
      count: 0,
    })),
    duplicates_by_day: dateSeries(7),
    sync_volume_by_connector: [
      { name: "No connectors", records: 0, last_7_days: 0, basis: "fallback" },
    ],
  };
}
async function loadDashboardStats() {
  const fallback = emptyDashboardStats();
  try {
    const stats = await api("/api/stats/dashboard");
    return Object.fromEntries(
      Object.keys(fallback).map((key) => [
        key,
        Array.isArray(stats[key]) && stats[key].length
          ? stats[key]
          : fallback[key],
      ]),
    );
  } catch {
    return fallback;
  }
}
async function loadActivityFeed() {
  try {
    renderActivity(await api("/api/activity"));
  } catch (error) {
    toast(error.message, true);
  }
}
async function loadWorkspace() {
  try {
    await loadWorkspaces();
    state.dashboard = await api("/api/dashboard");
    state.dashboardStats = await loadDashboardStats();
    renderMetrics(state.dashboard.metrics, state.dashboard.user);
    renderHealth(state.dashboard.connectors);
    await loadActivityFeed();
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
  renderConnectorDestinations();
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
    populateRecordFilters();
    await loadSavedViews();
    applyRecordFilters(false);
  } catch (error) {
    toast(error.message, true);
  }
}
function renderRecords(records) {
  const rows = records
    .map((record) => {
      const data = JSON.parse(record.data);
      const badgeClass =
        {
          valid: "healthy",
          duplicate: "error",
          invalid: "error",
          failed: "error",
          pending: "warning",
        }[record.status] || "";
      const name =
        data.name ||
        [data.first_name, data.last_name].filter(Boolean).join(" ") ||
        "Unnamed record";
      return `<div class="data-row record-row" data-record-id="${record.id}" role="button" tabindex="0" aria-label="Open ${escapeHtml(name)}"><div><strong>${escapeHtml(name)}</strong><small>${escapeHtml(data.email || "No email")} · ${escapeHtml(data.company || "Unknown company")}</small></div><span class="source-tag">${escapeHtml(record.source)}</span><span class="status-pill ${badgeClass}">${escapeHtml(record.status)}</span><span>${formatDate(record.created_at)}</span></div>`;
    })
    .join("");
  $("#records-summary").textContent =
    `${records.length} shown · ${state.records.length} normalized record${state.records.length === 1 ? "" : "s"}`;
  $("#records-table").innerHTML = rows
    ? `<div class="data-head"><span>RECORD</span><span>SOURCE</span><span>STATUS</span><span>CREATED</span></div>${rows}`
    : '<div class="empty-state">No matching records found.</div>';
  $$(".record-row").forEach((row) => {
    row.addEventListener("click", () =>
      openRecordDetail(Number(row.dataset.recordId)),
    );
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openRecordDetail(Number(row.dataset.recordId));
      }
    });
  });
}
function populateRecordFilters() {
  const sources = [
    ...new Set(state.records.map((record) => record.source)),
  ].sort();
  const statuses = [
    ...new Set(state.records.map((record) => record.status)),
  ].sort();
  const sourceValue = $("#record-source-filter").value;
  const statusValue = $("#record-status-filter").value;
  $("#record-source-filter").innerHTML =
    `<option value="">All sources</option>${sources.map((source) => `<option value="${escapeHtml(source)}">${escapeHtml(source)}</option>`).join("")}`;
  $("#record-status-filter").innerHTML =
    `<option value="">All statuses</option>${statuses.map((status) => `<option value="${escapeHtml(status)}">${escapeHtml(status)}</option>`).join("")}`;
  $("#record-source-filter").value = sourceValue;
  $("#record-status-filter").value = statusValue;
}
function getRecordFilters() {
  return {
    query: $("#record-search").value.trim(),
    source: $("#record-source-filter").value,
    status: $("#record-status-filter").value,
    created_after: $("#record-date-from").value,
    created_before: $("#record-date-to").value,
  };
}
function applyRecordFilters(clearSavedSelection = true) {
  if (clearSavedSelection) {
    state.selectedSavedViewId = null;
    $("#saved-view-select").value = "";
    $("#delete-record-view").disabled = true;
  }
  const filters = getRecordFilters();
  renderRecords(
    state.records.filter((record) => {
      const data = JSON.parse(record.data);
      const matchesQuery =
        !filters.query ||
        [record.source, record.status, ...Object.values(data)].some((value) =>
          String(value).toLowerCase().includes(filters.query.toLowerCase()),
        );
      const recordDate = String(record.created_at || "").slice(0, 10);
      return (
        matchesQuery &&
        (!filters.source || record.source === filters.source) &&
        (!filters.status || record.status === filters.status) &&
        (!filters.created_after || recordDate >= filters.created_after) &&
        (!filters.created_before || recordDate <= filters.created_before)
      );
    }),
  );
}
function renderSavedViews() {
  $("#saved-view-select").innerHTML =
    `<option value="">Saved views</option>${state.savedViews.map((view) => `<option value="${view.id}">${escapeHtml(view.name)}</option>`).join("")}`;
  $("#saved-view-select").value = state.selectedSavedViewId || "";
  $("#delete-record-view").disabled = !state.selectedSavedViewId;
}
async function loadSavedViews() {
  state.savedViews = await api("/api/records/saved-views");
  if (
    !state.savedViews.some(
      (view) => String(view.id) === String(state.selectedSavedViewId),
    )
  )
    state.selectedSavedViewId = null;
  renderSavedViews();
}
function setRecordFilters(filters) {
  $("#record-search").value = filters.query || "";
  $("#record-source-filter").value = filters.source || "";
  $("#record-status-filter").value = filters.status || "";
  $("#record-date-from").value = filters.created_after || "";
  $("#record-date-to").value = filters.created_before || "";
}
async function openRecordDetail(recordId) {
  let record = state.records.find((item) => Number(item.id) === recordId);
  if (!record) {
    try {
      record = await api(`/api/records/${recordId}`);
    } catch (error) {
      toast(error.message, true);
      return;
    }
  }
  const data = JSON.parse(record.data);
  const name =
    data.name ||
    [data.first_name, data.last_name].filter(Boolean).join(" ") ||
    `Record #${record.id}`;
  state.selectedRecordId = recordId;
  $("#record-detail-title").textContent = name;
  $("#record-detail-meta").textContent =
    `${record.source} · ${record.status} · Created ${formatDate(record.created_at)}`;
  $("#record-detail-data").innerHTML =
    Object.entries(data)
      .map(
        ([key, value]) =>
          `<div class="record-field"><strong>${escapeHtml(key.replace(/_/g, " "))}</strong><span>${escapeHtml(value)}</span></div>`,
      )
      .join("") || '<div class="empty-state">No fields available.</div>';
  $("#record-detail").classList.remove("hidden");
  await loadRecordComments(recordId);
  $("#record-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function highlightMentions(value) {
  return escapeHtml(value).replace(
    /@([\p{L}\p{N}._-]+)/gu,
    '<mark class="mention">@$1</mark>',
  );
}
function renderRecordComments(comments) {
  $("#record-comments").innerHTML = comments.length
    ? comments
        .map(
          (comment) =>
            `<article class="record-comment"><div class="comment-heading"><strong>${escapeHtml(comment.author_name)}</strong><time>${formatDate(comment.created_at)}</time></div><p>${highlightMentions(comment.body)}</p></article>`,
        )
        .join("")
    : '<p class="empty-state">No comments yet.</p>';
}
async function loadRecordComments(recordId) {
  try {
    renderRecordComments(await api(`/api/records/${recordId}/comments`));
  } catch (error) {
    toast(error.message, true);
  }
}
let globalSearchTimer;
async function searchWorkspace(query) {
  try {
    const results = await api(`/api/search?q=${encodeURIComponent(query)}`);
    if ($("#global-search").value.trim() !== query) return;
    $("#global-search-results").innerHTML = results.length
      ? results
          .map(
            (result) =>
              `<button class="search-result" type="button" role="option" data-type="${escapeHtml(result.type)}" data-id="${result.id}" data-record-id="${result.record_id || ""}"><i data-lucide="${result.type === "document" ? "file-text" : result.type === "comment" ? "message-square" : "contact"}"></i><span><strong>${escapeHtml(result.title)}</strong><small>${escapeHtml(result.detail)}</small></span></button>`,
          )
          .join("")
      : '<p class="search-empty">No results found.</p>';
    $("#global-search-results").classList.remove("hidden");
    icons();
  } catch (error) {
    toast(error.message, true);
  }
}
async function loadDocuments() {
  try {
    const documents = await api("/api/documents");
    $("#documents-list").innerHTML = documents.length
      ? `<div class="data-head"><span>NAME</span><span>SIZE</span><span>ADDED</span><span></span></div>${documents.map((document) => `<div class="data-row"><strong>${escapeHtml(document.name)}</strong><span>${(document.size / 1024).toFixed(1)} KB</span><span>${formatDate(document.created_at)}</span><span class="document-actions"><a class="text-button" href="/api/documents/${document.id}/download" target="_blank">Download <i data-lucide="arrow-up-right"></i></a><button class="icon-button delete-document" type="button" data-id="${document.id}" data-name="${escapeHtml(document.name)}" title="Delete document" aria-label="Delete ${escapeHtml(document.name)}"><i data-lucide="trash-2"></i></button></span></div>`).join("")}`
      : '<div class="empty-state">No documents yet. Upload a payload example or client brief.</div>';
    $$(".delete-document").forEach((button) =>
      button.addEventListener("click", () => deleteDocument(button)),
    );
    icons();
  } catch (error) {
    toast(error.message, true);
  }
}
async function deleteDocument(button) {
  const name = button.dataset.name;
  if (
    !window.confirm(`Delete "${name}" from the library? This cannot be undone.`)
  )
    return;
  button.disabled = true;
  try {
    await api(`/api/documents/${button.dataset.id}`, { method: "DELETE" });
    toast("Document deleted");
    await loadDocuments();
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
  }
}
const integrationLogos = {
  Slack: "/assets/logos/slack.svg",
  HubSpot: "/assets/logos/hubspot.svg",
  "Google Sheets": "/assets/logos/google-sheets.svg",
  "REST API": "/assets/logos/rest-api.svg",
};
function renderConnectorDestinations() {
  $("#connector-destinations").innerHTML = Object.entries(integrationLogos)
    .map(
      ([name, path]) =>
        `<button class="destination-link" type="button" data-view-target="integrations"><span class="destination-logo"><img src="${path}" alt="" loading="eager" /></span><span>${escapeHtml(name)}</span><i data-lucide="arrow-up-right"></i></button>`,
    )
    .join("");
  $("#connector-destinations")
    .querySelectorAll("[data-view-target]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        navigate(button.dataset.viewTarget),
      ),
    );
  icons();
}
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
async function loadWorkspaces() {
  state.workspaces = await api("/api/workspaces");
  if (!state.workspaces.length)
    throw new Error("No workspace membership found");
  state.currentWorkspace =
    state.workspaces.find(
      (workspace) => String(workspace.id) === String(state.currentWorkspaceId),
    ) || state.workspaces[0];
  state.currentWorkspaceId = String(state.currentWorkspace.id);
  localStorage.setItem("integratehub_workspace_id", state.currentWorkspaceId);
  $("#workspace-switcher").innerHTML = state.workspaces
    .map(
      (workspace) =>
        `<option value="${workspace.id}">${escapeHtml(workspace.name)}</option>`,
    )
    .join("");
  $("#workspace-switcher").value = state.currentWorkspaceId;
  $("#workspace-role").textContent = `${state.currentWorkspace.role} access`;
  $("#workspace-avatar").textContent = initials(state.currentWorkspace.name)[0];
  $("#settings-workspace-name").textContent = state.currentWorkspace.name;
  const isAdmin = ["owner", "admin"].includes(state.currentWorkspace.role);
  $$(".admin-control").forEach((element) =>
    element.classList.toggle("hidden", !isAdmin),
  );
}
async function loadTeam() {
  if (!state.currentWorkspace) return;
  try {
    const members = await api(
      `/api/workspaces/${state.currentWorkspace.id}/members`,
    );
    const isOwner = state.currentWorkspace.role === "owner";
    $("#member-role").innerHTML =
      `<option value="member">Member</option>${isOwner ? '<option value="admin">Admin</option>' : ""}`;
    $("#members-list").innerHTML = members
      .map((member) => {
        const isProtected =
          member.role === "owner" || (member.role === "admin" && !isOwner);
        const roleOptions = isOwner
          ? `<option value="member" ${member.role === "member" ? "selected" : ""}>Member</option><option value="admin" ${member.role === "admin" ? "selected" : ""}>Admin</option>`
          : `<option value="${member.role}" selected>${member.role}</option><option value="member">Member</option>`;
        return `<div class="member-row"><span class="member-avatar">${escapeHtml(initials(member.name))}</span><span class="member-identity"><strong>${escapeHtml(member.name)}</strong><small>${escapeHtml(member.email)}</small></span><span class="member-role">${escapeHtml(member.role)}</span><select class="member-role-select admin-control" data-id="${member.id}" ${isProtected ? "disabled" : ""} aria-label="Role for ${escapeHtml(member.name)}">${roleOptions}</select></div>`;
      })
      .join("");
    $$(".member-role-select").forEach((select) =>
      select.addEventListener("change", () => updateMemberRole(select)),
    );
    $$(".admin-control").forEach((element) =>
      element.classList.toggle(
        "hidden",
        !["owner", "admin"].includes(state.currentWorkspace.role),
      ),
    );
    icons();
  } catch (error) {
    toast(error.message, true);
  }
}
async function updateMemberRole(select) {
  try {
    await api(
      `/api/workspaces/${state.currentWorkspace.id}/members/${select.dataset.id}`,
      { method: "PATCH", body: JSON.stringify({ role: select.value }) },
    );
    toast("Member role updated");
    await loadTeam();
  } catch (error) {
    toast(error.message, true);
    await loadTeam();
  }
}
async function loadAudit() {
  try {
    const entries = await api(
      `/api/workspaces/${state.currentWorkspace.id}/audit-log`,
    );
    $("#audit-list").innerHTML = entries.length
      ? entries
          .map((entry) => {
            const marker =
              entry.already_idempotent === null
                ? ""
                : `<span class="audit-marker ${entry.already_idempotent ? "healthy" : ""}">${entry.already_idempotent ? "Already idempotent" : "Not idempotent"}</span>`;
            return `<article class="audit-row"><span class="audit-icon"><i data-lucide="${entry.action === "role_changed" ? "user-round-cog" : entry.action === "connector_secret_rotated" ? "key-round" : entry.action === "webhook_replay_recorded" ? "rotate-cw" : "activity"}"></i></span><div class="audit-copy"><strong>${escapeHtml(entry.action.replaceAll("_", " "))}</strong><small>${escapeHtml(entry.actor_name)} · ${escapeHtml(entry.target_type)} ${escapeHtml(entry.target_id || "")}</small></div><div class="audit-meta">${marker}<time>${formatDate(entry.created_at)}</time></div></article>`;
          })
          .join("")
      : '<div class="empty-state">No administrative events recorded yet.</div>';
    icons();
  } catch (error) {
    $("#audit-list").innerHTML =
      '<div class="empty-state">The audit log is available to workspace admins.</div>';
  }
}
async function loadAISettings() {
  try {
    const settings = await api(
      `/api/workspaces/${state.currentWorkspace.id}/ai-settings`,
    );
    $("#ai-gemini-enabled").checked = settings.gemini_enabled;
    $("#ai-rollout-percent").value = settings.gemini_rollout_percent;
    $("#ai-rollout-value").textContent = `${settings.gemini_rollout_percent}%`;
    $("#ai-workspace-bucket").textContent = `${settings.workspace_bucket}%`;
    const included =
      settings.gemini_enabled &&
      settings.workspace_bucket < settings.gemini_rollout_percent;
    $("#ai-effective-state").textContent = included
      ? "Gemini enabled"
      : "Local fallback";
    $("#ai-effective-state").classList.toggle("healthy", included);
    const editable = ["owner", "admin"].includes(state.currentWorkspace.role);
    $("#ai-gemini-enabled").disabled = !editable;
    $("#ai-rollout-percent").disabled = !editable;
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

function setAuthFieldError(input, message = "") {
  const label = input.closest("label");
  const error = document.getElementById(`${input.id}-error`);
  label.classList.toggle("invalid", Boolean(message));
  input.setAttribute("aria-invalid", String(Boolean(message)));
  if (error) error.textContent = message;
}

function validateAuthForm(form) {
  let valid = true;
  const inputs = [...form.querySelectorAll("input[required]")];
  inputs.forEach((input) => {
    let message = "";
    if (!input.value.trim()) message = "This field is required.";
    else if (input.type === "email" && input.validity.typeMismatch)
      message = "Enter a valid email address.";
    else if (input.minLength > 0 && input.value.length < input.minLength)
      message = `Use at least ${input.minLength} characters.`;
    setAuthFieldError(input, message);
    if (message) valid = false;
  });
  if (!valid)
    inputs
      .find((input) => input.getAttribute("aria-invalid") === "true")
      ?.focus();
  return valid;
}

$$(".nav-item[data-view], [data-view-target]").forEach((item) =>
  item.addEventListener("click", () =>
    navigate(item.dataset.view || item.dataset.viewTarget),
  ),
);
$("#mobile-menu").addEventListener("click", () =>
  $("#sidebar").classList.toggle("open"),
);
$("#dismiss-upgrade").addEventListener("click", () =>
  $("#upgrade-card").classList.add("hidden"),
);
$$("#login-form input, #register-form input").forEach((input) =>
  input.addEventListener("input", () => setAuthFieldError(input)),
);
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!validateAuthForm(event.currentTarget)) return;
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
    setAuthFieldError($("#password"), error.message);
  }
});
$("#show-register").addEventListener("click", () => {
  $("#login-form").classList.add("hidden");
  $("#register-form").classList.remove("hidden");
  $("#show-register").classList.add("hidden");
  $("#show-login").classList.remove("hidden");
  $("#auth-kicker").textContent = "CREATE YOUR WORKSPACE";
  $("#auth-title").textContent = "Start with a clear view.";
  $("#auth-subtitle").textContent =
    "Set up your workspace and bring your data together.";
});
$("#show-login").addEventListener("click", () => {
  $("#register-form").classList.add("hidden");
  $("#login-form").classList.remove("hidden");
  $("#show-login").classList.add("hidden");
  $("#show-register").classList.remove("hidden");
  $("#auth-kicker").textContent = "SIGN IN";
  $("#auth-title").textContent = "Welcome back.";
  $("#auth-subtitle").textContent = "Sign in to continue to your workspace.";
});
$("#register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!validateAuthForm(event.currentTarget)) return;
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
    const field = /email/i.test(error.message)
      ? $("#register-email")
      : /name/i.test(error.message)
        ? $("#register-name")
        : $("#register-password");
    setAuthFieldError(field, error.message);
  }
});
$("#google-auth").addEventListener("click", () =>
  toast("Google sign-in is not configured yet."),
);
$("#logout").addEventListener("click", () => {
  state.token = null;
  localStorage.removeItem("integratehub_token");
  state.currentWorkspaceId = null;
  state.currentWorkspace = null;
  localStorage.removeItem("integratehub_workspace_id");
  showAuth();
});
$$(".theme-toggle").forEach((button) =>
  button.addEventListener("click", () => {
    applyTheme(state.theme === "light" ? "dark" : "light");
  }),
);
$("#workspace-switcher").addEventListener("change", (event) => {
  state.currentWorkspaceId = event.target.value;
  localStorage.setItem("integratehub_workspace_id", state.currentWorkspaceId);
  loadWorkspace();
});
$("#workspace-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const workspace = await api("/api/workspaces", {
      method: "POST",
      body: JSON.stringify({ name: $("#workspace-name").value.trim() }),
    });
    state.currentWorkspaceId = String(workspace.id);
    localStorage.setItem("integratehub_workspace_id", state.currentWorkspaceId);
    $("#workspace-form").reset();
    toast("Workspace created");
    await loadWorkspace();
    navigate("team");
  } catch (error) {
    toast(error.message, true);
  }
});
$("#member-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api(`/api/workspaces/${state.currentWorkspace.id}/members`, {
      method: "POST",
      body: JSON.stringify({
        email: $("#member-email").value.trim(),
        role: $("#member-role").value,
      }),
    });
    $("#member-form").reset();
    toast("Member added to workspace");
    await loadTeam();
  } catch (error) {
    toast(error.message, true);
  }
});
$("#ai-rollout-percent").addEventListener("input", (event) => {
  $("#ai-rollout-value").textContent = `${event.target.value}%`;
});
$("#ai-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api(`/api/workspaces/${state.currentWorkspace.id}/ai-settings`, {
      method: "PATCH",
      body: JSON.stringify({
        gemini_enabled: $("#ai-gemini-enabled").checked,
        gemini_rollout_percent: Number($("#ai-rollout-percent").value),
      }),
    });
    toast("AI rollout saved");
    await loadAISettings();
  } catch (error) {
    toast(error.message, true);
  }
});
$("#replay-audit-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api(
      `/api/workspaces/${state.currentWorkspace.id}/audit-log/webhook-replay`,
      {
        method: "POST",
        body: JSON.stringify({
          connector_id: Number($("#replay-connector-id").value),
          event_id: $("#replay-event-id").value.trim(),
          already_idempotent: $("#replay-idempotent").checked,
        }),
      },
    );
    $("#replay-audit-form").reset();
    $("#replay-idempotent").checked = true;
    toast("Replay recorded in audit log");
    await loadAudit();
  } catch (error) {
    toast(error.message, true);
  }
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
$("#record-search").addEventListener("input", () => applyRecordFilters());
[
  "#record-source-filter",
  "#record-status-filter",
  "#record-date-from",
  "#record-date-to",
].forEach((selector) =>
  $(selector).addEventListener("change", () => applyRecordFilters()),
);
$("#records-filter").addEventListener("click", () => {
  setRecordFilters({});
  applyRecordFilters();
});
$("#saved-view-select").addEventListener("change", (event) => {
  const view = state.savedViews.find(
    (item) => String(item.id) === event.target.value,
  );
  state.selectedSavedViewId = view?.id || null;
  $("#delete-record-view").disabled = !view;
  if (view) {
    setRecordFilters(view.filters);
    applyRecordFilters(false);
  }
});
$("#save-record-view").addEventListener("click", async () => {
  const name = $("#saved-view-name").value.trim();
  if (!name) return toast("Enter a name for this view", true);
  try {
    const view = await api("/api/records/saved-views", {
      method: "POST",
      body: JSON.stringify({ name, filters: getRecordFilters() }),
    });
    state.savedViews.unshift(view);
    state.selectedSavedViewId = view.id;
    $("#saved-view-name").value = "";
    renderSavedViews();
    toast("View saved");
  } catch (error) {
    toast(error.message, true);
  }
});
$("#delete-record-view").addEventListener("click", async () => {
  if (!state.selectedSavedViewId) return;
  try {
    await api(`/api/records/saved-views/${state.selectedSavedViewId}`, {
      method: "DELETE",
    });
    state.savedViews = state.savedViews.filter(
      (view) => view.id !== state.selectedSavedViewId,
    );
    state.selectedSavedViewId = null;
    renderSavedViews();
    toast("View deleted");
  } catch (error) {
    toast(error.message, true);
  }
});
$("#record-comment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.selectedRecordId) return;
  const body = $("#record-comment-input").value.trim();
  if (!body) return;
  try {
    await api(`/api/records/${state.selectedRecordId}/comments`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });
    $("#record-comment-input").value = "";
    await loadRecordComments(state.selectedRecordId);
    await loadActivityFeed();
  } catch (error) {
    toast(error.message, true);
  }
});
$("#close-record-detail").addEventListener("click", () => {
  state.selectedRecordId = null;
  $("#record-detail").classList.add("hidden");
});
$("#global-search").addEventListener("input", (event) => {
  const query = event.target.value.trim();
  window.clearTimeout(globalSearchTimer);
  if (query.length < 2) {
    $("#global-search-results").classList.add("hidden");
    return;
  }
  globalSearchTimer = window.setTimeout(() => searchWorkspace(query), 220);
});
$("#global-search-results").addEventListener("click", async (event) => {
  const result = event.target.closest(".search-result");
  if (!result) return;
  const type = result.dataset.type;
  const recordId = Number(result.dataset.recordId || result.dataset.id);
  $("#global-search").value = "";
  $("#global-search-results").classList.add("hidden");
  if (type === "document") {
    navigate("documents");
    return;
  }
  navigate("records");
  if (!state.records.some((record) => Number(record.id) === recordId))
    await loadRecords();
  await openRecordDetail(recordId);
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".global-search-wrap"))
    $("#global-search-results").classList.add("hidden");
});
$("#global-search").addEventListener("keydown", (event) => {
  if (event.key === "Escape")
    $("#global-search-results").classList.add("hidden");
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
$("#refresh-audit").addEventListener("click", loadAudit);
$("#prompt-lucky").addEventListener("click", () => navigate("connectors"));
$("#prompt-action").addEventListener("click", () =>
  toast("Ask IntegrateHub from the assistant endpoint in your workspace API."),
);
applyTheme(state.theme);
if (state.token) showApp();
else showAuth();
