import { createClient } from "/vendor/supabase.js";

const API_PREFIX = "/api/v1";
const GROQ_STORAGE_PREFIX = "autoapply.groq_api_key.v2";
const HUNTER_STORAGE_PREFIX = "autoapply.hunter_api_key.v1";
const DISCOVERY_RUN_STORAGE_PREFIX = "autoapply.discovery_run.v1";
const GMAIL_REVOCATION_WARNING_PREFIX = "autoapply.gmail_revocation_warning.v1";
const UI_STORAGE_KEY = "autoapply.ui_preferences.v1";
const DEFAULT_RESUME_LIMIT = 6_291_456;
const TURNSTILE_SCRIPT_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
const TURNSTILE_FLEXIBLE_MIN_WIDTH = 300;
const DISCOVERY_POLL_INTERVAL_MS = 2_000;
const DISCOVERY_MONITOR_TIMEOUT_MS = 300_000;
const DISCOVERY_TERMINAL_STATUSES = new Set(["succeeded", "failed", "cancelled", "needs_attention"]);
const FORM_WORKFLOW_POLL_INTERVAL_MS = 2_000;
const FORM_WORKFLOW_MONITOR_TIMEOUT_MS = 300_000;
const BOOT_STEP_ORDER = ["service", "session", "workspace"];
const WORKSPACE_OPEN_TIMEOUT_MS = 30_000;
const PROVIDER_CREDENTIAL_NAMES = ["groq", "hunter", "browserbase"];

const viewCopy = {
  overview: {
    kicker: "Your workspace",
    title: "Overview",
    description: "A focused view of your application pipeline.",
  },
  profile: {
    kicker: "Applicant foundation",
    title: "Profile",
    description: "The factual source used to ground your drafts and application answers.",
  },
  discovery: {
    kicker: "Step 2 · Find opportunities",
    title: "Find jobs",
    description: "Use your résumé to search LinkedIn, Telegram, and RSS together.",
  },
  form_pilot: {
    kicker: "Step 3 · Prepare forms",
    title: "Form Pilot",
    description: "Prepare Google Forms and exact provider applications for review.",
  },
  outreach: {
    kicker: "Step 4 · Reach the right people",
    title: "Mass Cold Email",
    description: "Add your Hunter key, find recruiter emails, draft with Groq, and send only messages you approve through Gmail.",
  },
  jobs: {
    kicker: "Opportunity workspace",
    title: "Jobs",
    description: "Capture job descriptions, contacts, and application links.",
  },
  applications: {
    kicker: "Step 4 · Review and send",
    title: "Mass Cold Email",
    description: "Review and approve the exact cold-email messages before sending through Gmail.",
  },
  connections: {
    kicker: "External services",
    title: "Connections",
    description: "See what each provider supports and connect through approved flows.",
  },
  automation: {
    kicker: "Durable work",
    title: "Activity",
    description: "Monitor queued, running, completed, and attention-required work.",
  },
  account: {
    kicker: "Workspace controls",
    title: "Settings",
    description: "Set review safeguards and account-level preferences.",
  },
};

const state = {
  config: null,
  health: null,
  supabase: null,
  session: null,
  profile: {},
  settings: {},
  resumes: [],
  jobs: [],
  jobsHasMore: false,
  fitSummary: {},
  applications: [],
  formApplications: [],
  connections: [],
  googleOauthClient: {},
  googleOauthMode: null,
  googleOauthEditing: false,
  providerCredentials: { groq: {}, hunter: {}, browserbase: {} },
  providerCredentialEditing: new Set(),
  providerCredentialMigrationUserId: null,
  publicProviders: [],
  automationJobs: [],
  discoverySources: [],
  googleForms: [],
  googleFormsTotal: 0,
  ycPreferences: { query: "", remote_only: false, limit: 10 },
  resumeDiscoveryPlan: null,
  discoveryRun: null,
  discoveryMonitorPromise: null,
  workflowDockDismissedRunId: null,
  hunterValidation: null,
  outreachSelectedJobIds: new Set(),
  outreachContacts: {},
  formRevisions: {},
  selectedFormRevisionId: null,
  selectedFormApplicationId: null,
  formSuggestionAttempts: new Set(),
  formSuggestionCache: new Map(),
  formSubmissionJobs: new Map(),
  formWorkflowMonitors: new Map(),
  formRecoveryScanApplicationIds: new Set(),
  pendingJobImportFile: null,
  resumeSuggestions: null,
  pendingResumeFile: null,
  selectedApplicationId: null,
  applicationEditorDirty: false,
  currentView: "overview",
  recoveryMode: false,
  identityUserId: null,
  identityGeneration: 0,
  workspaceLoadId: 0,
  workspaceOpeningPromise: null,
  workspaceOpeningUserId: null,
  automationTimer: null,
  captchaToken: null,
  captchaWidgetId: null,
  captchaWidgetSize: null,
  captchaResizeObserver: null,
  captchaLoadPromise: null,
  captchaLoadError: null,
  accountDeletionInProgress: false,
  gmailRevocationWarning: false,
};

class AppError extends Error {
  constructor(message, code = "client_error", status = 0, requestId = null) {
    super(message);
    this.name = "AppError";
    this.code = code;
    this.status = status;
    this.requestId = requestId;
  }
}

class IdentityChangedError extends AppError {
  constructor() {
    super("The signed-in account changed while this request was running.", "identity_changed");
    this.name = "IdentityChangedError";
  }
}

const byId = (id) => document.getElementById(id);
const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));

function setText(target, value) {
  const node = typeof target === "string" ? byId(target) : target;
  const next = value == null ? "" : String(value);
  if (node && node.textContent !== next) node.textContent = next;
}

function setAriaBusy(node, busy) {
  if (!node) return;
  const next = busy ? "true" : "false";
  if (node.getAttribute("aria-busy") !== next) node.setAttribute("aria-busy", next);
}

function clearNode(node) {
  if (node) {
    node.replaceChildren();
    node.removeAttribute("aria-busy");
  }
}

function createElement(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = String(options.text);
  if (options.type) node.type = options.type;
  if (options.title) node.title = options.title;
  if (options.hidden) node.hidden = true;
  if (options.attrs) {
    for (const [name, value] of Object.entries(options.attrs)) {
      if (value !== undefined && value !== null) node.setAttribute(name, String(value));
    }
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child) node.append(child);
  }
  return node;
}

function emptyState(title, message, symbol = "◇") {
  const box = createElement("div", { className: "empty-state" });
  box.append(
    createElement("span", { className: "empty-state-icon", text: symbol, attrs: { "aria-hidden": "true" } }),
    createElement("strong", { text: title }),
    createElement("p", { text: message }),
  );
  return box;
}

function showLoading(container, count = 3) {
  if (!container) return;
  clearNode(container);
  container.setAttribute("aria-busy", "true");
  const list = createElement("div", {
    className: "loading-list",
    attrs: { role: "status", "aria-live": "polite", "aria-label": "Loading saved workspace items" },
  });
  for (let index = 0; index < count; index += 1) {
    list.append(createElement("div", { className: "skeleton", attrs: { "aria-hidden": "true" } }));
  }
  container.append(list);
}

function setBootCheckpoint(step, title, detail, activeStatus) {
  const boot = byId("boot-screen");
  if (!boot) return;
  const currentIndex = Math.max(0, BOOT_STEP_ORDER.indexOf(step));
  boot.hidden = false;
  boot.classList.remove("has-error");
  byId("boot-recovery").hidden = true;
  setAriaBusy(boot, true);
  setText("boot-title", title);
  setText("boot-detail", detail);
  all("[data-boot-step]", boot).forEach((item) => {
    const itemIndex = BOOT_STEP_ORDER.indexOf(item.dataset.bootStep);
    const complete = itemIndex < currentIndex;
    const active = itemIndex === currentIndex;
    item.classList.toggle("is-complete", complete);
    item.classList.toggle("is-active", active);
    if (active) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    const status = item.querySelector("small");
    if (status && complete) status.textContent = "Checked and ready";
    else if (status && active) status.textContent = activeStatus;
  });
}

function finishBoot() {
  const boot = byId("boot-screen");
  if (!boot) return;
  setAriaBusy(boot, false);
  boot.hidden = true;
}

function showBootFailure(error) {
  const boot = byId("boot-screen");
  if (!boot) return;
  boot.hidden = false;
  boot.classList.add("has-error");
  setAriaBusy(boot, false);
  setText("boot-title", "Your workspace stopped before opening");
  setText("boot-detail", errorMessage(error, "The saved workspace data could not be loaded."));
  byId("boot-recovery").hidden = false;
}

async function waitForWorkspaceLoad(identity) {
  let timeout;
  try {
    await Promise.race([
      loadWorkspace(identity),
      new Promise((resolve, reject) => {
        timeout = window.setTimeout(
          () => reject(new AppError("The workspace took too long to open. Check the service, then retry.", "workspace_load_timeout")),
          WORKSPACE_OPEN_TIMEOUT_MS,
        );
      }),
    ]);
  } finally {
    if (timeout) window.clearTimeout(timeout);
  }
}

function announce(message) {
  setText("global-status", message);
}

function toast(message, type = "info", title = null, timeout = 5_000) {
  if (message == null) return;
  const region = byId("toast-region");
  if (!region) return;
  const item = createElement("div", {
    className: `toast${type === "error" ? " is-error" : type === "success" ? " is-success" : ""}`,
    attrs: { role: type === "error" ? "alert" : "status" },
  });
  const symbol = type === "error" ? "!" : type === "success" ? "✓" : "i";
  const copy = createElement("div", { className: "toast-copy" }, [
    createElement("strong", { text: title || (type === "error" ? "Could not complete that" : type === "success" ? "Done" : "AutoApply") }),
    createElement("span", { text: message }),
  ]);
  const close = createElement("button", { className: "toast-close", text: "×", type: "button", attrs: { "aria-label": "Dismiss message" } });
  close.addEventListener("click", () => item.remove());
  item.append(createElement("span", { className: "toast-symbol", text: symbol, attrs: { "aria-hidden": "true" } }), copy, close);
  region.append(item);
  announce(message);
  if (timeout > 0) window.setTimeout(() => item.remove(), timeout);
}

let actionDialogResolve = null;
let actionDialogTrigger = null;

function settleActionDialog(confirmed) {
  const resolve = actionDialogResolve;
  const trigger = actionDialogTrigger;
  actionDialogResolve = null;
  actionDialogTrigger = null;
  document.body.classList.remove("modal-open");
  if (resolve) resolve(Boolean(confirmed));
  if (trigger?.isConnected && !trigger.disabled) {
    requestAnimationFrame(() => trigger.focus({ preventScroll: true }));
  }
}

function bindActionDialog() {
  const dialog = byId("action-dialog");
  if (!dialog) return;
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    dialog.close("cancel");
  });
  dialog.addEventListener("close", () => settleActionDialog(dialog.returnValue === "confirm"));
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close("cancel");
  });
}

function confirmAction({
  eyebrow = "Before you continue",
  title = "Review this action",
  message = "Check the details before continuing.",
  confirmLabel = "Continue",
  cancelLabel = "Keep current state",
  tone = "default",
  ticketLabel = "Review",
  symbol = "→",
} = {}) {
  const dialog = byId("action-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    toast("This browser cannot open the review dialog. The action was cancelled.", "error");
    return Promise.resolve(false);
  }
  if (dialog.open || actionDialogResolve) return Promise.resolve(false);

  setText("action-dialog-eyebrow", eyebrow);
  setText("action-dialog-title", title);
  setText("action-dialog-message", message);
  setText("action-dialog-confirm", confirmLabel);
  setText("action-dialog-cancel", cancelLabel);
  setText("action-dialog-ticket-label", ticketLabel);
  setText("action-dialog-symbol", symbol);
  dialog.dataset.tone = ["danger", "caution"].includes(tone) ? tone : "default";
  const confirmButton = byId("action-dialog-confirm");
  confirmButton.className = `button ${tone === "danger" ? "button-danger" : tone === "caution" ? "button-accent" : "button-primary"}`;
  dialog.returnValue = "cancel";
  actionDialogTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;

  return new Promise((resolve) => {
    actionDialogResolve = resolve;
    document.body.classList.add("modal-open");
    try {
      dialog.showModal();
      requestAnimationFrame(() => byId("action-dialog-cancel")?.focus());
    } catch {
      settleActionDialog(false);
    }
  });
}

function errorMessage(error, fallback = "Something went wrong. Please try again.") {
  if (isIdentityChanged(error)) return null;
  if (error instanceof AppError && error.message) return error.message;
  if (error && typeof error.message === "string" && error.message.length < 300) return error.message;
  return fallback;
}

function isIdentityChanged(error) {
  return error instanceof IdentityChangedError || error?.code === "identity_changed";
}

function clearPrivateState() {
  state.profile = {};
  state.settings = {};
  state.resumes = [];
  state.jobs = [];
  state.jobsHasMore = false;
  state.fitSummary = {};
  state.applications = [];
  state.formApplications = [];
  state.connections = [];
  state.googleOauthClient = {};
  state.googleOauthMode = null;
  state.googleOauthEditing = false;
  state.providerCredentials = { groq: {}, hunter: {}, browserbase: {} };
  state.providerCredentialEditing = new Set();
  state.providerCredentialMigrationUserId = null;
  // A typed-but-unsaved secret lives in the DOM, not application state. Clear
  // every provider form at the same identity boundary so it cannot appear in
  // the next account's workspace after sign-out or an account switch.
  all("[data-provider-credential]").forEach((form) => form.reset());
  state.automationJobs = [];
  state.discoverySources = [];
  state.googleForms = [];
  state.googleFormsTotal = 0;
  state.resumeDiscoveryPlan = null;
  state.discoveryRun = null;
  state.discoveryMonitorPromise = null;
  state.workflowDockDismissedRunId = null;
  state.hunterValidation = null;
  state.outreachSelectedJobIds = new Set();
  state.outreachContacts = {};
  state.formRevisions = {};
  state.selectedFormRevisionId = null;
  state.selectedFormApplicationId = null;
  state.formSuggestionAttempts = new Set();
  state.formSuggestionCache = new Map();
  state.formSubmissionJobs = new Map();
  state.formWorkflowMonitors = new Map();
  state.formRecoveryScanApplicationIds = new Set();
  state.pendingJobImportFile = null;
  state.resumeSuggestions = null;
  state.pendingResumeFile = null;
  state.selectedApplicationId = null;
  state.applicationEditorDirty = false;
  const dialog = byId("action-dialog");
  if (dialog?.open) dialog.close("cancel");
}

function setSession(session) {
  const nextSession = session?.user?.id ? session : null;
  const nextUserId = nextSession?.user?.id || null;
  if (nextUserId !== state.identityUserId) {
    state.identityUserId = nextUserId;
    state.identityGeneration += 1;
    state.workspaceLoadId += 1;
    state.accountDeletionInProgress = false;
    state.gmailRevocationWarning = nextUserId ? hasGmailRevocationWarning(nextUserId) : false;
    clearPrivateState();
    stopAutomationPolling();
  }
  state.session = nextSession;
  return nextSession;
}

function identitySnapshot() {
  const userId = state.identityUserId || state.session?.user?.id || null;
  if (!userId) throw new AppError("Your session has ended. Please sign in again.", "not_authenticated", 401);
  return { userId, generation: state.identityGeneration };
}

function isCurrentIdentity(snapshot) {
  return Boolean(
    snapshot
      && snapshot.generation === state.identityGeneration
      && snapshot.userId === state.identityUserId
      && snapshot.userId === state.session?.user?.id,
  );
}

function assertCurrentIdentity(snapshot) {
  if (!isCurrentIdentity(snapshot)) throw new IdentityChangedError();
}

function setFormMessage(id, message = "", type = "") {
  if (message == null) return;
  const node = byId(id);
  if (!node) return;
  node.textContent = message;
  node.classList.toggle("is-error", type === "error");
  node.classList.toggle("is-success", type === "success");
}

async function withBusy(button, busyLabel, action) {
  if (!button) return action();
  if (button.disabled) return undefined;
  const originalNodes = Array.from(button.childNodes);
  const hadAriaLabel = button.hasAttribute("aria-label");
  const originalAriaLabel = button.getAttribute("aria-label");
  const originalDisabled = button.disabled;
  button.dataset.busy = "true";
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  setBusyLabel(button, busyLabel);
  try {
    return await action();
  } finally {
    delete button.dataset.busy;
    button.removeAttribute("aria-busy");
    button.replaceChildren(...originalNodes);
    if (hadAriaLabel) button.setAttribute("aria-label", originalAriaLabel || "");
    else button.removeAttribute("aria-label");
    if (isCaptchaProtectedAuthButton(button)) updateCaptchaControls();
    else button.disabled = originalDisabled;
  }
}

function setBusyLabel(button, busyLabel) {
  if (!button) return;
  const label = String(busyLabel || "Working…") === "…" ? "Refreshing…" : String(busyLabel || "Working…");
  const compact = button.classList.contains("icon-button");
  button.replaceChildren(
    createElement("span", { className: "button-pending-spinner", attrs: { "aria-hidden": "true" } }),
    createElement("span", { className: compact ? "sr-only" : "button-pending-label", text: label }),
  );
  if (compact) button.setAttribute("aria-label", label.replace(/…/g, ""));
}

function isCaptchaProtectedAuthButton(button) {
  return Boolean(button?.matches?.(
    "#signin-form button[type='submit'], #signup-form button[type='submit'], #reset-form button[type='submit']",
  ));
}

function safeHttpUrl(value) {
  if (!value || typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

function safeBrowserbaseLiveViewUrl(value) {
  const candidate = safeHttpUrl(value);
  if (!candidate) return null;
  try {
    const url = new URL(candidate);
    const host = url.hostname.toLowerCase();
    return url.protocol === "https:" && (host === "browserbase.com" || host.endsWith(".browserbase.com"))
      ? url.href
      : null;
  } catch {
    return null;
  }
}

function safeGoogleAuthorizationUrl(value) {
  const candidate = safeHttpUrl(value);
  if (!candidate) return null;
  try {
    const url = new URL(candidate);
    return url.protocol === "https:" && url.hostname.toLowerCase() === "accounts.google.com"
      ? url.href
      : null;
  } catch {
    return null;
  }
}

function openExternal(value) {
  const url = safeHttpUrl(value);
  if (!url) {
    toast("That external URL is missing or invalid.", "error");
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
}

function formatDate(value, includeTime = false) {
  if (!value) return "Not yet";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown date";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    ...(includeTime ? { timeStyle: "short" } : {}),
  }).format(date);
}

function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "Unknown size";
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`;
  return `${(value / 1_048_576).toFixed(1)} MB`;
}

function humanize(value) {
  if (!value) return "Unknown";
  return String(value)
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function statusClass(status) {
  const value = String(status || "").toLowerCase();
  if (["succeeded", "sent", "connected", "active", "approved", "applied", "ready", "parsed", "valid"].includes(value)) return "status-success";
  if (["failed", "error", "disconnected", "cancelled", "rejected", "invalid"].includes(value)) return "status-danger";
  if (["needs_attention", "partner_required", "manual_only", "expired", "unavailable"].includes(value)) return "status-warning";
  if (["queued", "running", "pending", "draft_pending", "drafted", "drafting", "uploaded", "parsing", "saved"].includes(value)) return "status-info";
  return "status-neutral";
}

function makeStatus(status, label = null) {
  return createElement("span", { className: `status-pill ${statusClass(status)}`, text: label || humanize(status) });
}

function unwrapData(payload) {
  return payload && typeof payload === "object" && !Array.isArray(payload) && Object.hasOwn(payload, "data")
    ? payload.data
    : payload;
}

function unwrapItems(payload, fallbackKeys = []) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== "object") return [];
  if (Array.isArray(payload.items)) return payload.items;
  for (const key of fallbackKeys) {
    if (Array.isArray(payload[key])) return payload[key];
  }
  const data = unwrapData(payload);
  if (Array.isArray(data)) return data;
  if (data && typeof data === "object" && Array.isArray(data.items)) return data.items;
  return [];
}

function getUiPreferences() {
  try {
    const parsed = JSON.parse(localStorage.getItem(UI_STORAGE_KEY) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function saveUiPreferences(update) {
  try {
    const next = { ...getUiPreferences(), ...update };
    localStorage.setItem(UI_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // The workspace remains usable when browser storage is blocked.
  }
}

function gmailRevocationWarningStorageKey(userId = state.identityUserId) {
  return typeof userId === "string" && userId
    ? `${GMAIL_REVOCATION_WARNING_PREFIX}.${userId}`
    : null;
}

function hasGmailRevocationWarning(userId = state.identityUserId) {
  try {
    const storageKey = gmailRevocationWarningStorageKey(userId);
    return Boolean(storageKey && localStorage.getItem(storageKey) === "required");
  } catch {
    return state.gmailRevocationWarning;
  }
}

function setGmailRevocationWarning(enabled, userId = state.identityUserId) {
  state.gmailRevocationWarning = Boolean(enabled);
  try {
    const storageKey = gmailRevocationWarningStorageKey(userId);
    if (!storageKey) return;
    if (enabled) localStorage.setItem(storageKey, "required");
    else localStorage.removeItem(storageKey);
  } catch {
    // Keep the warning for this page lifetime if browser storage is unavailable.
  }
}

function groqStorageKey(userId = state.identityUserId) {
  return typeof userId === "string" && userId ? `${GROQ_STORAGE_PREFIX}.${userId}` : null;
}

function getLegacyGroqKey(userId = state.identityUserId) {
  try {
    const storageKey = groqStorageKey(userId);
    return storageKey ? localStorage.getItem(storageKey) || "" : "";
  } catch {
    return "";
  }
}

function deleteLegacyGroqKey(userId = state.identityUserId) {
  try {
    const storageKey = groqStorageKey(userId);
    if (storageKey) localStorage.removeItem(storageKey);
  } catch {
    throw new AppError("This browser blocked access to local storage.", "storage_unavailable");
  }
}

function hunterStorageKey(userId = state.identityUserId) {
  return typeof userId === "string" && userId ? `${HUNTER_STORAGE_PREFIX}.${userId}` : null;
}

function getLegacyHunterKey(userId = state.identityUserId) {
  try {
    const storageKey = hunterStorageKey(userId);
    return storageKey ? localStorage.getItem(storageKey) || "" : "";
  } catch {
    return "";
  }
}

function deleteLegacyHunterKey(userId = state.identityUserId) {
  try {
    const storageKey = hunterStorageKey(userId);
    if (storageKey) localStorage.removeItem(storageKey);
  } catch {
    throw new AppError("This browser blocked access to local storage.", "storage_unavailable");
  }
}

function discoveryRunStorageKey(userId = state.identityUserId) {
  return typeof userId === "string" && userId ? `${DISCOVERY_RUN_STORAGE_PREFIX}.${userId}` : null;
}

function saveDiscoveryRun(run, userId = state.identityUserId) {
  const storageKey = discoveryRunStorageKey(userId);
  if (!storageKey) return;
  try {
    sessionStorage.setItem(storageKey, JSON.stringify({
      job_ids: run.jobIds,
      started_at: run.startedAt,
    }));
  } catch {
    // The live page can still monitor the run if session storage is unavailable.
  }
}

function loadDiscoveryRun(userId = state.identityUserId) {
  const storageKey = discoveryRunStorageKey(userId);
  if (!storageKey) return null;
  try {
    const parsed = JSON.parse(sessionStorage.getItem(storageKey) || "null");
    const jobIds = Array.isArray(parsed?.job_ids)
      ? parsed.job_ids.filter((value) => typeof value === "string" && /^[0-9a-f-]{36}$/i.test(value)).slice(0, 2)
      : [];
    const startedAt = Number(parsed?.started_at);
    if (!jobIds.length || !Number.isFinite(startedAt) || startedAt <= 0) return null;
    return { jobIds, startedAt, jobs: [], monitoring: false };
  } catch {
    return null;
  }
}

function clearDiscoveryRun(userId = state.identityUserId) {
  const storageKey = discoveryRunStorageKey(userId);
  if (!storageKey) return;
  try {
    sessionStorage.removeItem(storageKey);
  } catch {
    // The in-memory run is still cleared.
  }
}

function providerCredential(provider) {
  return state.providerCredentials?.[provider] || {};
}

function credentialSaved(provider) {
  return providerCredential(provider).configured === true;
}

function credentialConfigured(provider) {
  const credential = providerCredential(provider);
  return credential.configured === true
    && credential.requires_reconfiguration !== true
    && credential.verification_status !== "invalid"
    && (provider !== "browserbase" || credential.verification_status === "verified");
}

function credentialHint(provider) {
  const value = String(providerCredential(provider).key_hint || "").trim();
  return value || "Saved secret";
}

async function publicRequest(path) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await fetch(`${API_PREFIX}${path}`, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: controller.signal,
    });
    const payload = await readResponse(response);
    if (!response.ok) throw apiErrorFrom(response, payload);
    return payload;
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new AppError("The workspace service took too long to respond. Try again.", "request_timeout");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function readResponse(response) {
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  await response.text();
  return { message: "The service returned an unexpected response." };
}

function apiErrorFrom(response, payload) {
  const details = payload && typeof payload === "object" ? payload.error : null;
  const message = details?.message || payload?.message || `Request failed with status ${response.status}.`;
  return new AppError(message, details?.code || "request_failed", response.status, details?.request_id || null);
}

async function storageClientForIdentity(identity = identitySnapshot()) {
  assertCurrentIdentity(identity);
  const sessionResult = await state.supabase.auth.getSession();
  assertCurrentIdentity(identity);
  const session = sessionResult.data?.session;
  if (!session?.access_token || session.user?.id !== identity.userId) throw new IdentityChangedError();
  return createClient(state.config.supabase_url, state.config.supabase_publishable_key, {
    accessToken: async () => session.access_token,
    auth: {
      persistSession: false,
      autoRefreshToken: false,
      detectSessionInUrl: false,
    },
    global: { headers: { "X-Client-Info": "autoapply-cloud-web-upload/2.0" } },
  });
}

async function apiRequest(path, options = {}) {
  if (!state.supabase) throw new AppError("Account services are not configured.", "auth_unavailable");
  const identity = options.identity || identitySnapshot();
  assertCurrentIdentity(identity);
  const method = options.method || "GET";
  const sessionResult = await state.supabase.auth.getSession();
  assertCurrentIdentity(identity);
  let session = sessionResult.data?.session || state.session;
  if (!session?.access_token) throw new AppError("Your session has ended. Please sign in again.", "not_authenticated", 401);
  if (session.user?.id !== identity.userId) throw new IdentityChangedError();

  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  headers.set("Authorization", `Bearer ${session.access_token}`);

  const init = {
    method,
    headers,
    cache: "no-store",
    credentials: "same-origin",
    signal: options.signal,
  };
  if (options.body !== undefined) {
    if (options.body instanceof FormData) {
      init.body = options.body;
    } else {
      headers.set("Content-Type", "application/json");
      init.body = JSON.stringify(options.body);
    }
  }

  let response = await fetch(`${API_PREFIX}${path}`, init);
  assertCurrentIdentity(identity);
  if (response.status === 401 && options.retry !== false) {
    const refreshed = await state.supabase.auth.refreshSession();
    assertCurrentIdentity(identity);
    session = refreshed.data?.session;
    if (session?.user?.id && session.user.id !== identity.userId) throw new IdentityChangedError();
    if (session?.access_token) {
      setSession(session);
      assertCurrentIdentity(identity);
      headers.set("Authorization", `Bearer ${session.access_token}`);
      response = await fetch(`${API_PREFIX}${path}`, init);
      assertCurrentIdentity(identity);
    }
  }

  const payload = await readResponse(response);
  assertCurrentIdentity(identity);
  if (!response.ok) {
    const responseError = apiErrorFrom(response, payload);
    if (response.status === 401) {
      window.setTimeout(() => {
        if (!isCurrentIdentity(identity)) return;
        setSession(null);
        showPublicSite();
        state.supabase?.auth.signOut();
      }, 0);
    }
    if (responseError.code === "account_deletion_in_progress" && isCurrentIdentity(identity)) {
      showAccountDeletionScreen(responseError.message);
    }
    throw responseError;
  }
  return payload;
}

function currentUser() {
  return state.session?.user || null;
}

function initialFor(value) {
  const text = String(value || "U").trim();
  return (text[0] || "U").toUpperCase();
}

function setAccountDeletionPath(active) {
  const url = new URL(window.location.href);
  if (active) {
    url.searchParams.set("account", "deleting");
    url.searchParams.delete("view");
    url.searchParams.delete("oauth");
    url.searchParams.delete("connection");
    url.searchParams.delete("oauth_error");
  } else {
    url.searchParams.delete("account");
  }
  if (url.href !== window.location.href) window.history.replaceState(null, "", url);
}

function showAccountDeletionScreen(message = "Your workspace is locked while deletion finishes.") {
  if (!currentUser()) {
    showPublicSite();
    return;
  }
  const wasVisible = state.accountDeletionInProgress;
  state.accountDeletionInProgress = true;
  state.workspaceLoadId += 1;
  clearPrivateState();
  stopAutomationPolling();
  byId("skip-link").setAttribute("href", "#account-deletion-main");
  finishBoot();
  byId("public-site").hidden = true;
  byId("workspace").hidden = true;
  byId("account-deletion-screen").hidden = false;
  document.body.classList.remove("workspace-open");
  setText("account-deletion-email", currentUser()?.email || "your signed-in account");
  if (!wasVisible) {
    byId("account-deletion-retry-form").reset();
    setFormMessage("account-deletion-message", message);
  }
  updateAccountDeletionRetryButton();
  setAccountDeletionPath(true);
}

function showPublicSite() {
  state.accountDeletionInProgress = false;
  byId("skip-link").setAttribute("href", "#public-main");
  finishBoot();
  byId("account-deletion-screen").hidden = true;
  byId("workspace").hidden = true;
  byId("public-site").hidden = false;
  document.body.classList.remove("workspace-open");
  stopAutomationPolling();
  setAccountDeletionPath(false);
}

async function showWorkspace(session) {
  if (!session?.user) {
    showPublicSite();
    return;
  }
  if (state.identityUserId !== session.user.id || state.session?.access_token !== session.access_token) return;
  if (state.workspaceOpeningPromise && state.workspaceOpeningUserId === session.user.id) {
    return state.workspaceOpeningPromise;
  }
  const opening = openWorkspace(session);
  state.workspaceOpeningPromise = opening;
  state.workspaceOpeningUserId = session.user.id;
  try {
    return await opening;
  } catch (error) {
    if (state.identityUserId === session.user.id) showBootFailure(error);
    throw error;
  } finally {
    if (state.workspaceOpeningPromise === opening) {
      state.workspaceOpeningPromise = null;
      state.workspaceOpeningUserId = null;
    }
  }
}

async function openWorkspace(session) {
  if (state.accountDeletionInProgress) {
    showAccountDeletionScreen();
    return;
  }
  setSession(session);
  state.accountDeletionInProgress = false;
  const identity = identitySnapshot();
  setBootCheckpoint(
    "workspace",
    "Opening your application desk",
    "Loading your profile, résumé, jobs, connections, and active work.",
    "Collecting your saved data",
  );
  byId("skip-link").setAttribute("href", "#main-content");
  byId("account-deletion-screen").hidden = true;
  byId("public-site").hidden = true;
  byId("workspace").hidden = true;
  document.body.classList.remove("workspace-open");
  setAccountDeletionPath(false);

  const email = session.user.email || "Signed-in account";
  const display = state.profile.full_name || session.user.user_metadata?.full_name || email.split("@")[0] || "Your account";
  setText("sidebar-user-email", email);
  setText("sidebar-user-name", display);
  setText("account-email", email);
  setText("account-name", display);
  setText("sidebar-avatar", initialFor(display));
  setText("account-avatar", initialFor(display));
  renderGmailRevocationWarning();

  switchView(viewFromUrl(), false);
  await waitForWorkspaceLoad(identity);
  if (!isCurrentIdentity(identity) || state.accountDeletionInProgress) return;
  byId("workspace").hidden = false;
  document.body.classList.add("workspace-open");
  finishBoot();
  showOAuthResult();
}

function captchaEnabled() {
  return state.config?.captcha?.enabled === true
    && state.config?.captcha?.provider === "turnstile"
    && typeof state.config?.captcha?.site_key === "string"
    && state.config.captcha.site_key.length > 0;
}

function updateCaptchaControls() {
  const enabled = captchaEnabled();
  for (const button of all("#signin-form button[type='submit'], #signup-form button[type='submit'], #reset-form button[type='submit']")) {
    button.disabled = button.dataset.busy === "true"
      || (enabled && (Boolean(state.captchaLoadError) || !state.captchaToken));
  }
}

function resetCaptcha() {
  state.captchaToken = null;
  byId("auth-captcha").classList.remove("is-complete");
  if (captchaEnabled() && !state.captchaLoadError) {
    setFormMessage("auth-captcha-status", "Complete a fresh challenge to continue.");
  }
  updateCaptchaControls();
  if (state.captchaWidgetId !== null && window.turnstile?.reset) {
    try {
      window.turnstile.reset(state.captchaWidgetId);
    } catch {
      // A navigation or provider-side teardown can remove the widget first.
    }
  }
}

function requireCaptchaToken() {
  if (!captchaEnabled()) return undefined;
  if (state.captchaLoadError) {
    throw new AppError(
      "Bot protection is unavailable. Reload the page before trying to sign in.",
      "captcha_unavailable",
    );
  }
  if (!state.captchaToken) {
    throw new AppError("Complete the bot-protection challenge before continuing.", "captcha_required");
  }
  return state.captchaToken;
}

function handleCaptchaLoadFailure(error) {
  state.captchaLoadError = error instanceof Error
    ? error
    : new AppError("Bot protection could not be loaded.", "captcha_unavailable");
  state.captchaToken = null;
  byId("auth-captcha-section").hidden = false;
  setFormMessage(
    "auth-captcha-status",
    "Bot protection could not load. Authentication is disabled until you reload and complete the challenge.",
    "error",
  );
  updateCaptchaControls();
}

function authProviderError(error) {
  return error?.code === "captcha_failed"
    ? "The challenge expired or failed. Complete the new challenge and try again."
    : error?.message || "Authentication could not be completed.";
}

function googleAuthProviderError(error) {
  const detail = `${error?.code || ""} ${error?.message || ""}`.toLowerCase();
  if (detail.includes("provider") && (detail.includes("disabled") || detail.includes("enabled") || detail.includes("unsupported"))) {
    return "Google sign-in is not enabled on this deployment yet.";
  }
  return authProviderError(error);
}

function consumeAuthCallbackError() {
  const url = new URL(window.location.href);
  const keys = ["error", "error_code", "error_description"];
  const hashParams = new URLSearchParams(url.hash.startsWith("#") ? url.hash.slice(1) : url.hash);
  const hashHasError = keys.some((key) => hashParams.has(key));
  const queryHasError = keys.some((key) => url.searchParams.has(key));
  if (!hashHasError && !queryHasError) return null;

  const values = keys.map((key) => url.searchParams.get(key) || hashParams.get(key) || "");
  for (const key of keys) url.searchParams.delete(key);
  if (hashHasError) {
    for (const key of keys) hashParams.delete(key);
    url.hash = hashParams.size ? `#${hashParams.toString()}` : "";
  }
  window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);

  const detail = values.join(" ").toLowerCase();
  if (detail.includes("access_denied")) {
    return "Google sign-in was cancelled. You can try again whenever you are ready.";
  }
  if (detail.includes("provider") && (detail.includes("disabled") || detail.includes("enabled") || detail.includes("unsupported"))) {
    return "Google sign-in is not enabled on this deployment yet.";
  }
  return "Google sign-in could not be completed. Try again or use email and password.";
}

function captchaSizeForWidth(width) {
  if (!Number.isFinite(width) || width <= 0) return state.captchaWidgetSize || "flexible";
  return width < TURNSTILE_FLEXIBLE_MIN_WIDTH ? "compact" : "flexible";
}

function captchaSizeForContainer() {
  return captchaSizeForWidth(byId("auth-captcha").getBoundingClientRect().width);
}

function renderCaptchaWidget(size = captchaSizeForContainer()) {
  const container = byId("auth-captcha");
  state.captchaWidgetSize = size;
  container.classList.toggle("is-compact", size === "compact");
  container.classList.remove("is-complete");
  state.captchaWidgetId = window.turnstile.render(container, {
    sitekey: state.config.captcha.site_key,
    size,
    theme: "auto",
    language: "auto",
    callback: (token) => {
      state.captchaToken = typeof token === "string" && token ? token : null;
      container.classList.toggle("is-complete", Boolean(state.captchaToken));
      setFormMessage("auth-captcha-status", state.captchaToken ? "Security check complete." : "Complete the challenge to continue.", state.captchaToken ? "success" : "");
      updateCaptchaControls();
    },
    "expired-callback": () => {
      state.captchaToken = null;
      container.classList.remove("is-complete");
      setFormMessage("auth-captcha-status", "The challenge expired. Complete the new challenge to continue.", "error");
      updateCaptchaControls();
    },
    "error-callback": () => {
      state.captchaToken = null;
      container.classList.remove("is-complete");
      setFormMessage("auth-captcha-status", "The challenge could not be completed. Try again or reload the page.", "error");
      updateCaptchaControls();
    },
  });
}

function observeCaptchaSize() {
  if (state.captchaResizeObserver || !("ResizeObserver" in window)) return;
  state.captchaResizeObserver = new ResizeObserver((entries) => {
    const width = entries[0]?.contentRect?.width;
    const nextSize = captchaSizeForWidth(width);
    if (
      state.captchaWidgetId === null
      || nextSize === state.captchaWidgetSize
      || !window.turnstile?.render
      || !window.turnstile?.remove
    ) return;

    state.captchaToken = null;
    updateCaptchaControls();
    try {
      window.turnstile.remove(state.captchaWidgetId);
    } catch {
      // The provider can remove its iframe before this resize callback runs.
    }
    state.captchaWidgetId = null;
    clearNode(byId("auth-captcha"));
    renderCaptchaWidget(nextSize);
    setFormMessage("auth-captcha-status", "Complete the challenge to continue.");
  });
  state.captchaResizeObserver.observe(byId("auth-captcha"));
}

async function initialiseCaptcha() {
  const section = byId("auth-captcha-section");
  if (!captchaEnabled()) {
    state.captchaLoadError = null;
    section.hidden = true;
    setFormMessage("auth-captcha-status");
    updateCaptchaControls();
    return;
  }
  state.captchaLoadError = null;
  section.hidden = false;
  setFormMessage("auth-captcha-status", "Loading bot protection…");
  updateCaptchaControls();
  if (!state.captchaLoadPromise) {
    state.captchaLoadPromise = new Promise((resolve, reject) => {
      if (window.turnstile?.render) {
        resolve();
        return;
      }
      const script = document.createElement("script");
      script.src = TURNSTILE_SCRIPT_URL;
      script.async = true;
      script.defer = true;
      const timeoutId = window.setTimeout(() => {
        reject(new AppError("Bot protection timed out while loading.", "captcha_unavailable"));
      }, 12_000);
      script.addEventListener("load", () => {
        window.clearTimeout(timeoutId);
        resolve();
      }, { once: true });
      script.addEventListener("error", () => {
        window.clearTimeout(timeoutId);
        reject(new AppError("Bot protection could not be loaded.", "captcha_unavailable"));
      }, { once: true });
      document.head.append(script);
    });
  }
  await state.captchaLoadPromise;
  if (!window.turnstile?.render) throw new AppError("Bot protection did not initialize.", "captcha_unavailable");
  if (state.captchaWidgetId === null) {
    renderCaptchaWidget();
    observeCaptchaSize();
  }
  setFormMessage("auth-captcha-status", "Complete the challenge to continue.");
  updateCaptchaControls();
}

function setAuthMode(mode) {
  const signin = mode === "signin";
  byId("signin-form").hidden = !signin;
  byId("signup-form").hidden = signin;
  byId("reset-form").hidden = true;
  byId("auth-social").hidden = false;
  byId("auth-tab-signin").closest(".segmented-control").hidden = false;
  byId("auth-tab-signin").classList.toggle("is-active", signin);
  byId("auth-tab-signup").classList.toggle("is-active", !signin);
  byId("auth-tab-signin").setAttribute("aria-selected", String(signin));
  byId("auth-tab-signup").setAttribute("aria-selected", String(!signin));
  setText("auth-heading", signin ? "Sign in to continue" : "Create your workspace");
  setText("auth-subtitle", signin ? "Your workspace is isolated to your account." : "Start with a private profile and résumé space.");
  setFormMessage("auth-message");
  resetCaptcha();
}

function showResetForm(show) {
  byId("signin-form").hidden = show;
  byId("signup-form").hidden = true;
  byId("reset-form").hidden = !show;
  byId("auth-social").hidden = show;
  byId("auth-tab-signin").closest(".segmented-control").hidden = show;
  setText("auth-heading", show ? "Reset your password" : "Sign in to continue");
  setText("auth-subtitle", show ? "We will email a one-time recovery link." : "Your workspace is isolated to your account.");
  setFormMessage("auth-message");
  resetCaptcha();
  if (!show) setAuthMode("signin");
}

function showRecoveryForm(show) {
  state.recoveryMode = show;
  byId("auth-standard").hidden = show;
  byId("recovery-form").hidden = !show;
  if (show) {
    showPublicSite();
    byId("recovery-password").focus();
  }
}

async function loadWorkspace(identity = identitySnapshot()) {
  const loadId = ++state.workspaceLoadId;
  const results = await Promise.allSettled([
    loadProfile(true, identity),
    loadSettings(true, identity),
    loadResumes(true, identity),
    loadJobs(true, identity),
    loadApplications(true, identity),
    loadFormApplications(true, identity),
    loadProviderCredentials(true, identity),
    loadConnections(true, identity),
    loadAutomationJobs(true, identity),
    loadDiscoverySources(true, identity),
    loadGoogleForms(true, identity),
    loadYcPreferences(true, identity),
  ]);
  if (loadId !== state.workspaceLoadId || !isCurrentIdentity(identity)) return;
  if (state.profile?.account_status === "deleting") {
    showAccountDeletionScreen("Account deletion has started. Retry deletion to finish cleanup.");
    return;
  }
  const failed = results.filter((result) => result.status === "rejected" && !isIdentityChanged(result.reason));
  if (failed.length) toast(`${failed.length} workspace section${failed.length === 1 ? "" : "s"} could not be refreshed.`, "error");
  updateUserIdentity();
  renderOverview();
  renderGroqState();
  renderResumeDiscoveryPlan();
  renderOutreach();
  resumeDiscoveryMonitoring(identity);
}

function updateUserIdentity() {
  const user = currentUser();
  if (!user) return;
  const display = state.profile.full_name || user.user_metadata?.full_name || user.email?.split("@")[0] || "there";
  setText("sidebar-user-name", display);
  setText("account-name", display);
  setText("welcome-name", display.split(/\s+/)[0]);
  setText("sidebar-avatar", initialFor(display));
  setText("account-avatar", initialFor(display));
  setText("account-delete-email-copy", user.email || "your signed-in email");
  updateAccountDeleteButton();
}

function viewFromUrl() {
  const url = new URL(window.location.href);
  const view = url.searchParams.get("view") || "overview";
  if (view === "outreach" && url.searchParams.get("tab") === "review") return "applications";
  return view === "assets" ? "profile" : view;
}

function switchView(view, push = true) {
  if (view === "assets") view = "profile";
  if (!Object.hasOwn(viewCopy, view)) view = "overview";
  const massEmailView = view === "outreach" || view === "applications";
  state.currentView = view;
  all("[data-view-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.viewPanel !== view;
  });
  all("[data-view]").forEach((button) => {
    const active = button.dataset.view === (massEmailView ? "outreach" : view);
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  all("[data-mass-email-view]").forEach((button) => {
    const active = button.dataset.massEmailView === view;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  const copy = viewCopy[view];
  setText("view-kicker", copy.kicker);
  setText("view-title", copy.title);
  setText("view-description", copy.description);
  closeMobileMenu();
  if (push) {
    const url = new URL(window.location.href);
    url.searchParams.set("view", massEmailView ? "outreach" : view);
    if (view === "applications") url.searchParams.set("tab", "review");
    else url.searchParams.delete("tab");
    url.searchParams.delete("oauth");
    url.searchParams.delete("connection");
    url.searchParams.delete("oauth_error");
    window.history.pushState({ view }, "", url);
  }
  if (view === "automation") startAutomationPolling();
  else stopAutomationPolling();
  if (view === "discovery") {
    renderResumeDiscoveryPlan();
  }
  if (view === "form_pilot") renderGoogleFormQueue();
  if (view === "outreach") renderOutreach();
  if (view === "applications") renderApplications();
  const mainContent = byId("main-content");
  if (mainContent) {
    mainContent.scrollTop = 0;
    requestAnimationFrame(() => { mainContent.scrollTop = 0; });
  }
}

function openMobileMenu() {
  byId("workspace-sidebar").classList.add("is-open");
  byId("mobile-backdrop").hidden = false;
  byId("menu-toggle").setAttribute("aria-expanded", "true");
}

function closeMobileMenu() {
  byId("workspace-sidebar").classList.remove("is-open");
  byId("mobile-backdrop").hidden = true;
  byId("menu-toggle").setAttribute("aria-expanded", "false");
}

async function checkHealth() {
  const badge = byId("api-health-badge");
  try {
    const payload = await publicRequest("/health");
    state.health = payload;
    const healthStatus = String(payload?.status || "").toLowerCase();
    const ready = payload?.ready === true || (payload?.ready !== false && ["ready", "ok", "healthy"].includes(healthStatus));
    badge.className = `status-pill ${ready ? "status-success" : "status-warning"}`;
    badge.textContent = ready ? "Service ready" : "Setup required";
  } catch {
    badge.className = "status-pill status-danger";
    badge.textContent = "Service unavailable";
  }
}

async function loadPublicProviders() {
  try {
    const payload = await publicRequest("/providers");
    state.publicProviders = unwrapItems(payload, ["providers"]);
  } catch {
    state.publicProviders = [];
  }
}

async function loadProfile(quiet = false, identity = identitySnapshot()) {
  if (!quiet) byId("profile-save-state").textContent = "Loading";
  const payload = await apiRequest("/profile", { identity });
  state.profile = unwrapData(payload) || {};
  populateProfileForm();
  updateUserIdentity();
  if (!quiet) toast("Profile refreshed.", "success");
  return state.profile;
}

function educationToLines(value) {
  if (!Array.isArray(value)) return "";
  return value
    .map((item) => {
      if (typeof item === "string") return item;
      if (!item || typeof item !== "object") return "";
      return item.label || [item.degree, item.field, item.institution].filter(Boolean).join(" — ");
    })
    .filter(Boolean)
    .join("\n");
}

function educationFromLines(lines, existing) {
  const current = Array.isArray(existing) ? existing : [];
  const byLabel = new Map(
    current
      .filter((item) => item && typeof item === "object" && typeof item.label === "string")
      .map((item) => [item.label.trim().toLowerCase(), item]),
  );
  return lines.map((label) => {
    const preserved = byLabel.get(label.toLowerCase());
    return preserved ? { ...preserved, label } : { label };
  });
}

function csvValues(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function populateProfileForm() {
  const profile = state.profile || {};
  const preferences = profile.preferences && typeof profile.preferences === "object" ? profile.preferences : {};
  const values = {
    "profile-full-name": profile.full_name,
    "profile-email": profile.email || currentUser()?.email,
    "profile-phone": profile.phone,
    "profile-location": profile.location,
    "profile-headline": profile.headline,
    "profile-summary": profile.summary,
    "profile-years": profile.years_experience,
    "profile-authorization": profile.work_authorization,
    "profile-notice": profile.notice_period,
    "profile-college": profile.college,
    "profile-degree": profile.degree,
    "profile-graduation-year": profile.graduation_year,
    "profile-skills": Array.isArray(profile.skills) ? profile.skills.join(", ") : "",
    "profile-education": educationToLines(profile.education),
    "profile-target-roles": Array.isArray(preferences.target_roles) ? preferences.target_roles.join(", ") : "",
    "profile-preferred-locations": Array.isArray(preferences.preferred_locations) ? preferences.preferred_locations.join(", ") : "",
    "profile-linkedin": profile.linkedin_url,
    "profile-github": profile.github_url,
    "profile-portfolio": profile.portfolio_url,
    "profile-resume-url": profile.resume_url,
  };
  for (const [id, value] of Object.entries(values)) {
    const input = byId(id);
    if (input) input.value = value == null ? "" : String(value);
  }
  byId("profile-remote").checked = Boolean(preferences.remote);
  byId("profile-complete").checked = Boolean(profile.onboarding_completed);
  const pill = byId("profile-save-state");
  pill.className = `status-pill ${profile.onboarding_completed ? "status-success" : "status-warning"}`;
  pill.textContent = profile.onboarding_completed ? "Onboarding complete" : "In progress";
  renderProfileCompleteness();
  renderJobIntelligence();
}

function profilePayloadFromForm() {
  const lines = byId("profile-education").value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const nullable = (id) => byId(id).value.trim() || null;
  const yearsText = byId("profile-years").value.trim();
  const graduationText = byId("profile-graduation-year").value.trim();
  return {
    full_name: nullable("profile-full-name"),
    email: nullable("profile-email"),
    phone: nullable("profile-phone"),
    location: nullable("profile-location"),
    headline: nullable("profile-headline"),
    summary: nullable("profile-summary"),
    years_experience: yearsText ? Number(yearsText) : null,
    work_authorization: nullable("profile-authorization"),
    notice_period: nullable("profile-notice"),
    college: nullable("profile-college"),
    degree: nullable("profile-degree"),
    graduation_year: graduationText ? Number(graduationText) : null,
    linkedin_url: nullable("profile-linkedin"),
    github_url: nullable("profile-github"),
    portfolio_url: nullable("profile-portfolio"),
    resume_url: nullable("profile-resume-url"),
    education: educationFromLines(lines, state.profile.education),
    skills: csvValues(byId("profile-skills").value),
    preferences: {
      ...(state.profile.preferences && typeof state.profile.preferences === "object" ? state.profile.preferences : {}),
      target_roles: csvValues(byId("profile-target-roles").value),
      preferred_locations: csvValues(byId("profile-preferred-locations").value),
      remote: byId("profile-remote").checked,
    },
    onboarding_completed: byId("profile-complete").checked,
  };
}

function renderProfileCompleteness() {
  const fields = [
    ["Full name", byId("profile-full-name").value],
    ["Application email", byId("profile-email").value],
    ["Location", byId("profile-location").value],
    ["Professional headline", byId("profile-headline").value],
    ["Background summary", byId("profile-summary").value],
    ["Skills", byId("profile-skills").value],
    ["Work authorization", byId("profile-authorization").value],
    ["Target roles", byId("profile-target-roles").value],
    ["Passout year", byId("profile-graduation-year").value],
    ["LinkedIn profile", byId("profile-linkedin").value],
    ["GitHub profile", byId("profile-github").value],
    ["Public résumé link", byId("profile-resume-url").value],
  ];
  const complete = fields.filter(([, value]) => String(value || "").trim()).length;
  const percentage = Math.round((complete / fields.length) * 100);
  setText("profile-progress-label", `${percentage}%`);
  byId("profile-progress-bar").style.width = `${percentage}%`;
  const list = byId("profile-missing-list");
  clearNode(list);
  const missing = fields.filter(([, value]) => !String(value || "").trim()).map(([label]) => label);
  if (!missing.length) list.append(createElement("li", { text: "Your core profile fields are complete." }));
  else missing.slice(0, 5).forEach((label) => list.append(createElement("li", { text: `Add ${label.toLowerCase()}` })));
}

async function saveProfile(event) {
  event.preventDefault();
  const button = event.submitter || event.currentTarget.querySelector("button[type=submit]");
  await withBusy(button, "Saving…", async () => {
    try {
      const payload = await apiRequest("/profile", { method: "PATCH", body: profilePayloadFromForm() });
      state.profile = unwrapData(payload) || {};
      populateProfileForm();
      await Promise.all([loadJobs(true), loadGoogleForms(true)]);
      updateUserIdentity();
      renderOverview();
      toast("Your applicant profile was saved.", "success");
    } catch (error) {
      toast(errorMessage(error, "The profile could not be saved."), "error");
    }
  });
}

async function loadSettings(quiet = false, identity = identitySnapshot()) {
  const payload = await apiRequest("/settings", { identity });
  state.settings = unwrapData(payload) || {};
  populateSettingsForm();
  if (!quiet) toast("Settings refreshed.", "success");
  return state.settings;
}

function populateSettingsForm() {
  byId("settings-daily-cap").value = String(state.settings.daily_send_cap ?? 10);
  byId("settings-duplicate-days").value = String(state.settings.duplicate_window_days ?? 7);
  byId("settings-timezone").value = state.settings.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  byId("settings-review").checked = state.settings.require_review !== false;
}

async function saveSettings(event) {
  event.preventDefault();
  const button = event.submitter;
  await withBusy(button, "Saving…", async () => {
    try {
      const payload = await apiRequest("/settings", {
        method: "PATCH",
        body: {
          daily_send_cap: Number(byId("settings-daily-cap").value),
          duplicate_window_days: Number(byId("settings-duplicate-days").value),
          require_review: true,
          timezone: byId("settings-timezone").value.trim(),
        },
      });
      state.settings = unwrapData(payload) || {};
      populateSettingsForm();
      toast("Workspace settings were saved.", "success");
    } catch (error) {
      toast(errorMessage(error, "Settings could not be saved."), "error");
    }
  });
}

function accountDeleteInputsMatch() {
  const signedInEmail = String(currentUser()?.email || "").trim().toLowerCase();
  const enteredEmail = String(byId("account-delete-email")?.value || "").trim().toLowerCase();
  const confirmation = String(byId("account-delete-confirmation")?.value || "").trim();
  return Boolean(signedInEmail && enteredEmail === signedInEmail && confirmation === "DELETE");
}

function updateAccountDeleteButton() {
  const button = byId("account-delete-submit");
  if (button) button.disabled = !accountDeleteInputsMatch();
}

function accountDeletionRetryMatches() {
  return String(byId("account-deletion-confirmation")?.value || "").trim() === "DELETE";
}

function updateAccountDeletionRetryButton() {
  const button = byId("account-deletion-retry-submit");
  if (button && button.dataset.busy !== "true") button.disabled = !accountDeletionRetryMatches();
}

async function finishDeletedAccount(identity, form = null) {
  let localKeyRemoved = true;
  try {
    deleteLegacyGroqKey(identity.userId);
  } catch {
    localKeyRemoved = false;
  }
  try {
    deleteLegacyHunterKey(identity.userId);
  } catch {
    localKeyRemoved = false;
  }
  setGmailRevocationWarning(false, identity.userId);

  try {
    await state.supabase?.auth.signOut({ scope: "local" });
  } catch {
    // The server-side account is already gone; clear in-memory state even if the
    // auth client cannot acknowledge its local sign-out.
  }

  if (state.identityUserId && state.identityUserId !== identity.userId) return;
  setSession(null);
  form?.reset();
  showPublicSite();
  setAuthMode("signin");

  if (localKeyRemoved) {
    toast("Your account, workspace, and encrypted provider credentials were permanently deleted.", "success", "Account deleted", 0);
  } else {
    toast("Your account was deleted, but this browser blocked removal of a local API key. Clear site data and rotate the key with its provider.", "error", "Account deleted with a local cleanup warning", 0);
  }
}

async function retryAccountDeletion(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter || byId("account-deletion-retry-submit");
  setFormMessage("account-deletion-message");
  if (!accountDeletionRetryMatches()) {
    setFormMessage("account-deletion-message", "Type DELETE exactly to retry permanent deletion.", "error");
    updateAccountDeletionRetryButton();
    return;
  }

  const identity = identitySnapshot();
  await withBusy(button, "Retrying deletion…", async () => {
    try {
      await apiRequest("/account", {
        method: "DELETE",
        body: { confirmation: "DELETE" },
        retry: false,
        identity,
      });
      await finishDeletedAccount(identity, form);
    } catch (error) {
      setFormMessage(
        "account-deletion-message",
        errorMessage(error, "Deletion is still incomplete. Retry in a moment or contact the deployment operator."),
        "error",
      );
    }
  });
  updateAccountDeletionRetryButton();
}

async function deleteAccount(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter || byId("account-delete-submit");
  setFormMessage("account-delete-message");

  if (!accountDeleteInputsMatch()) {
    setFormMessage("account-delete-message", "Enter your signed-in email and DELETE exactly to continue.", "error");
    updateAccountDeleteButton();
    return;
  }

  const identity = identitySnapshot();
  const email = currentUser()?.email || "this account";
  if (!await confirmAction({
    eyebrow: "Final account check",
    title: "Permanently delete this account?",
    message: `${email} and all AutoApply workspace data will be deleted. This cannot be undone.`,
    confirmLabel: "Delete account permanently",
    cancelLabel: "Keep my account",
    tone: "danger",
    ticketLabel: "Permanent action",
    symbol: "×",
  })) return;

  await withBusy(button, "Deleting account…", async () => {
    try {
      await apiRequest("/account", { method: "DELETE", body: { confirmation: "DELETE" }, retry: false, identity });
      await finishDeletedAccount(identity, form);
    } catch (error) {
      const message = errorMessage(error, "The account could not be deleted. No local credentials were removed.");
      if ([
        "account_deletion_in_progress",
        "account_remote_cleanup_unavailable",
        "account_remote_cleanup_failed",
        "account_storage_cleanup_failed",
      ].includes(error?.code)) {
        showAccountDeletionScreen(message);
      } else {
        setFormMessage("account-delete-message", message, "error");
      }
    }
  });
  updateAccountDeleteButton();
}

async function loadResumes(quiet = false, identity = identitySnapshot()) {
  const container = byId("resume-list");
  if (!quiet) showLoading(container, 2);
  const payload = await apiRequest("/resumes", { identity });
  state.resumes = unwrapItems(payload, ["resumes"]);
  renderResumes();
  renderOverview();
  renderJobIntelligence();
  renderResumeDiscoveryPlan();
  renderOutreach();
  return state.resumes;
}

function renderResumes() {
  const container = byId("resume-list");
  clearNode(container);
  if (!state.resumes.length) {
    container.append(emptyState("No résumé uploaded", "Choose one PDF to start your private application kit.", "▤"));
    return;
  }
  for (const resume of state.resumes) {
    const card = createElement("article", { className: "document-card" });
    const copy = createElement("div", { className: "document-copy" }, [
      createElement("strong", { text: resume.original_name || "Résumé.pdf" }),
      createElement("small", { text: `${formatBytes(resume.size_bytes)} · Uploaded ${formatDate(resume.created_at)}` }),
    ]);
    const parseStatus = makeStatus(resume.parse_status || (resume.parsed_text ? "parsed" : "uploaded"));
    const actions = createElement("div", { className: "card-actions" });
    if (resume.parse_status !== "parsed" && !resume.parsed_text) {
      const parse = createElement("button", { className: "button button-ghost button-small", text: "Parse", type: "button" });
      parse.addEventListener("click", () => parseResume(resume.id, parse));
      actions.append(parse);
    }
    const remove = createElement("button", { className: "button button-danger-quiet button-small", text: "Delete", type: "button" });
    remove.addEventListener("click", () => removeResume(resume, remove));
    actions.append(remove);
    card.append(createElement("span", { className: "document-icon", text: "PDF", attrs: { "aria-hidden": "true" } }), copy, parseStatus, actions);
    container.append(card);
  }
}

async function sha256Hex(file) {
  if (!crypto.subtle) return null;
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function validateResumeFile(file) {
  if (!file) throw new AppError("Choose a PDF résumé first.", "resume_missing");
  const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
  if (!isPdf) throw new AppError("Only PDF résumés are accepted.", "resume_type_invalid");
  const limit = Number(state.config?.feature_flags?.max_resume_bytes || state.config?.max_resume_bytes || DEFAULT_RESUME_LIMIT);
  if (file.size <= 0 || file.size > limit) throw new AppError(`The PDF must be smaller than ${formatBytes(limit)}.`, "resume_too_large");
}

async function uploadResume(event) {
  event.preventDefault();
  const identity = identitySnapshot();
  const button = event.submitter;
  const file = state.pendingResumeFile || byId("resume-file").files?.[0];
  try {
    validateResumeFile(file);
  } catch (error) {
    toast(errorMessage(error), "error");
    return;
  }
  await withBusy(button, "Uploading…", async () => {
    const progress = byId("resume-upload-progress");
    progress.hidden = false;
    const bucket = state.config?.feature_flags?.resume_bucket || "resumes";
    let storagePath = null;
    let uploaded = false;
    let registrationComplete = false;
    let storageClient = null;
    try {
      const hash = await sha256Hex(file);
      assertCurrentIdentity(identity);
      storageClient = await storageClientForIdentity(identity);
      const listing = await storageClient.storage.from(bucket).list(identity.userId, {
        limit: 10,
        sortBy: { column: "name", order: "asc" },
      });
      if (listing.error) throw new AppError(listing.error.message || "Storage could not check résumé capacity.", "storage_list_failed");
      const occupied = new Set((listing.data || []).map((entry) => String(entry?.name || "")));
      const slot = [1, 2, 3, 4, 5].find((candidate) => !occupied.has(`resume-${candidate}.pdf`));
      if (!slot) throw new AppError("Delete an existing résumé before uploading another (maximum 5).", "resume_quota_reached");
      storagePath = `${identity.userId}/resume-${slot}.pdf`;
      const result = await storageClient.storage.from(bucket).upload(storagePath, file, {
        contentType: "application/pdf",
        upsert: false,
        cacheControl: "3600",
      });
      if (result.error) throw new AppError(result.error.message || "Storage rejected the upload.", "storage_upload_failed");
      uploaded = true;
      assertCurrentIdentity(identity);

      const registeredPayload = await apiRequest("/resumes/register", {
        method: "POST",
        identity,
        body: {
          storage_path: storagePath,
          original_name: file.name,
          mime_type: "application/pdf",
          size_bytes: file.size,
          ...(hash ? { sha256: hash } : {}),
        },
      });
      registrationComplete = true;
      const resume = unwrapData(registeredPayload);
      state.pendingResumeFile = null;
      byId("resume-file").value = "";
      setText("resume-file-label", "or drop it here");
      await loadResumes(true);
      toast("Résumé uploaded to your private storage folder.", "success");
      if (resume?.id) await parseResume(resume.id, null, true);
    } catch (error) {
      if (uploaded && !registrationComplete && storagePath) {
        try {
          await storageClient?.storage.from(bucket).remove([storagePath]);
        } catch {
          // The server/operator can clean an orphan if rollback is unavailable.
        }
      }
      toast(errorMessage(error, "The résumé could not be uploaded."), "error");
    } finally {
      progress.hidden = true;
    }
  });
}

async function parseResume(resumeId, button = null, quiet = false) {
  const execute = async () => {
    try {
      const payload = await apiRequest(`/resumes/${encodeURIComponent(resumeId)}/parse`, { method: "POST" });
      state.resumeSuggestions = payload?.suggestions && typeof payload.suggestions === "object"
        ? payload.suggestions
        : null;
      renderResumeSuggestions();
      await loadResumes(true);
      if (credentialConfigured("groq")) {
        await analyzeResume(resumeId, { quiet, autoFill: true });
      } else {
        const filled = applyResumeSuggestions({ navigate: false, notify: false });
        toast(
          filled
            ? `Résumé parsed and ${filled} blank profile field${filled === 1 ? " was" : "s were"} filled. Add a Groq key for full extraction.`
            : "Résumé parsed. Add a Groq key for full profile extraction and role recommendations.",
          "success",
        );
      }
    } catch (error) {
      toast(errorMessage(error, "The PDF could not be parsed."), "error");
    }
  };
  if (button) await withBusy(button, "Parsing…", execute);
  else await execute();
}

async function analyzeResume(resumeId, { button = null, quiet = false, autoFill = true } = {}) {
  const execute = async () => {
    try {
      const payload = await apiRequest(`/resumes/${encodeURIComponent(resumeId)}/analyze`, {
        method: "POST",
        groq: true,
      });
      const data = unwrapData(payload) || {};
      const suggestions = data.suggestions && typeof data.suggestions === "object"
        ? data.suggestions
        : {};
      state.resumeSuggestions = { ...(state.resumeSuggestions || {}), ...suggestions };
      renderResumeSuggestions();
      const filled = autoFill
        ? applyResumeSuggestions({ navigate: false, notify: false })
        : 0;
      renderJobIntelligence();
      if (!quiet || filled) {
        toast(
          filled
            ? `Résumé analysis filled ${filled} blank profile field${filled === 1 ? "" : "s"}. Review and save your profile.`
            : "Résumé analysis is ready for review.",
          "success",
        );
      }
      return suggestions;
    } catch (error) {
      toast(errorMessage(error, "The résumé could not be analyzed with Groq."), "error");
      return null;
    }
  };
  return button ? withBusy(button, "Analyzing…", execute) : execute();
}

function renderResumeSuggestions() {
  const card = byId("resume-suggestions");
  const list = byId("suggestions-list");
  clearNode(list);
  const suggestions = state.resumeSuggestions;
  if (!suggestions || typeof suggestions !== "object" || !Object.keys(suggestions).length) {
    card.hidden = true;
    return;
  }
  const labels = {
    full_name: "Full name",
    email: "Email",
    phone: "Phone",
    location: "Location",
    headline: "Headline",
    summary: "Summary",
    skills: "Skills",
    education: "Education",
    years_experience: "Years of experience",
    work_authorization: "Work authorization",
    notice_period: "Notice period",
    college: "College or university",
    degree: "Degree",
    graduation_year: "Passout year",
    linkedin_url: "LinkedIn profile",
    github_url: "GitHub profile",
    portfolio_url: "Portfolio",
    target_roles: "Recommended target roles",
  };
  for (const [key, value] of Object.entries(suggestions)) {
    if (!Object.hasOwn(labels, key) || value == null || value === "") continue;
    const display = Array.isArray(value)
      ? value.map((item) => typeof item === "string" ? item : item?.label || "").filter(Boolean).join(", ")
      : typeof value === "object" ? JSON.stringify(value) : String(value);
    list.append(createElement("div", { className: "suggestion-row" }, [
      createElement("div", { className: "suggestion-copy" }, [
        createElement("strong", { text: labels[key] }),
        createElement("small", { text: display.slice(0, 500) }),
      ]),
    ]));
  }
  card.hidden = list.childElementCount === 0;
}

function isPlaceholderProfileUrl(value) {
  try {
    const placeholders = new Set([
      "changeme", "insertlink", "inserturl", "replaceme",
      "yourhandle", "yourname", "yourprofile", "yourusername",
    ]);
    return new URL(value).pathname
      .split("/")
      .filter(Boolean)
      .map((segment) => decodeURIComponent(segment).toLowerCase().replace(/[^a-z0-9]/g, ""))
      .some((segment) => placeholders.has(segment));
  } catch (_error) {
    return false;
  }
}

function applyResumeSuggestions({ navigate = true, notify = true } = {}) {
  const map = {
    full_name: "profile-full-name",
    email: "profile-email",
    phone: "profile-phone",
    location: "profile-location",
    headline: "profile-headline",
    summary: "profile-summary",
    years_experience: "profile-years",
    work_authorization: "profile-authorization",
    notice_period: "profile-notice",
    college: "profile-college",
    degree: "profile-degree",
    graduation_year: "profile-graduation-year",
    linkedin_url: "profile-linkedin",
    github_url: "profile-github",
    portfolio_url: "profile-portfolio",
  };
  let count = 0;
  for (const [key, id] of Object.entries(map)) {
    const value = state.resumeSuggestions?.[key];
    const input = byId(id);
    const mayReplacePlaceholder = ["linkedin_url", "github_url", "portfolio_url"].includes(key)
      && isPlaceholderProfileUrl(input?.value || "");
    if (input && (!input.value.trim() || mayReplacePlaceholder) && value != null && value !== "") {
      input.value = String(value);
      count += 1;
    }
  }
  if (!byId("profile-skills").value.trim() && Array.isArray(state.resumeSuggestions?.skills)) {
    byId("profile-skills").value = state.resumeSuggestions.skills.map((item) => typeof item === "string" ? item : item?.label || "").filter(Boolean).join(", ");
    count += 1;
  }
  if (!byId("profile-education").value.trim() && Array.isArray(state.resumeSuggestions?.education)) {
    byId("profile-education").value = educationToLines(state.resumeSuggestions.education);
    count += 1;
  }
  if (!byId("profile-target-roles").value.trim() && Array.isArray(state.resumeSuggestions?.target_roles)) {
    byId("profile-target-roles").value = state.resumeSuggestions.target_roles
      .filter((item) => typeof item === "string" && item.trim())
      .join(", ");
    count += 1;
  }
  renderProfileCompleteness();
  renderJobIntelligence();
  if (notify) {
    toast(count ? `${count} blank profile field${count === 1 ? " was" : "s were"} filled. Review and save the profile.` : "No blank supported fields needed filling.", count ? "success" : "info");
  }
  if (navigate) switchView("profile");
  return count;
}

async function fillProfileFromResume(button) {
  const active = state.resumes.find((resume) => resume.is_active !== false) || state.resumes[0];
  if (!active) {
    switchView("profile");
    requestAnimationFrame(() => byId("resume-file")?.focus());
    toast("Upload a PDF résumé first.", "info");
    return;
  }
  if (active.parse_status !== "parsed") {
    await parseResume(active.id, button);
    switchView("profile");
    return;
  }
  if (!credentialConfigured("groq")) {
    switchView("profile");
    requestAnimationFrame(() => byId("groq-key")?.focus());
    toast("Add a Groq key to extract the complete profile from your résumé.", "info");
    return;
  }
  await analyzeResume(active.id, { button, autoFill: true });
  switchView("profile");
}

async function removeResume(resume, button) {
  if (!await confirmAction({
    eyebrow: "Résumé library",
    title: "Delete this résumé?",
    message: `${resume.original_name || "This résumé"} and its private stored PDF will be removed.`,
    confirmLabel: "Delete résumé",
    cancelLabel: "Keep résumé",
    tone: "danger",
    ticketLabel: "Private file",
    symbol: "CV",
  })) return;
  await withBusy(button, "Deleting…", async () => {
    try {
      await apiRequest(`/resumes/${encodeURIComponent(resume.id)}`, { method: "DELETE" });
      await loadResumes(true);
      toast("Résumé deleted.", "success");
    } catch (error) {
      toast(errorMessage(error, "The résumé could not be deleted."), "error");
    }
  });
}

function renderGroqState() {
  const credential = providerCredential("groq");
  const saved = credential.configured === true;
  const ready = credentialConfigured("groq");
  const summary = byId("groq-saved-summary");
  const pill = byId("groq-status-pill");
  if (!summary || !pill) return;
  summary.hidden = !saved;
  setText("groq-masked-key", saved ? credentialHint("groq") : "");
  pill.className = `status-pill ${ready ? credential.verified_at ? "status-success" : "status-info" : saved ? "status-warning" : "status-neutral"}`;
  pill.textContent = ready ? credential.verified_at ? "Groq ready" : "Saved · check pending" : saved ? "Replace required" : "Not connected";
  byId("delete-groq").disabled = !saved;
  renderOverview();
  renderJobIntelligence();
  renderResumeDiscoveryPlan();
  renderOutreach();
}

function discoveryRunKey(prefix) {
  return `${prefix}-${crypto.randomUUID()}`;
}

function showDiscoveryResult(message, tone = "info") {
  const banner = byId("discovery-result-banner");
  if (!banner) return;
  banner.hidden = !message;
  banner.className = `notice${tone === "success" ? " notice-success" : tone === "error" ? " notice-danger" : tone === "warning" ? " notice-warning" : ""}`;
  banner.textContent = message || "";
}

async function loadDiscoverySources(quiet = false, identity = identitySnapshot()) {
  try {
    const payload = await apiRequest("/discovery/sources", { identity });
    state.discoverySources = unwrapItems(payload, ["sources"]);
    return state.discoverySources;
  } catch (error) {
    if (!quiet) throw error;
    state.discoverySources = [];
    return [];
  }
}

function discoveredJobs() {
  return state.jobs
    .filter((job) => String(job.source || "manual").toLowerCase() !== "manual" || job.metadata?.discovered === true)
    .sort((a, b) => new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0))
    .slice(0, 12);
}

function renderDiscoveryJobs() {
  const container = byId("discovery-job-list");
  if (!container) return;
  clearNode(container);
  const jobs = discoveredJobs();
  if (!jobs.length) {
    container.append(emptyState("The incoming tray is empty", "Run a public search, paste a digest, or import a spreadsheet. Saved results will appear here.", "⌕"));
    return;
  }
  for (const job of jobs) {
    const source = String(job.source || job.metadata?.provider || "import");
    const clipping = createElement("article", { className: "discovery-clipping" });
    const sourceLine = createElement("div", { className: "clipping-source" }, [
      createElement("span", { text: humanize(source) }),
      createElement("span", { text: formatDate(job.created_at) }),
    ]);
    const actions = createElement("div", { className: "card-actions" });
    const review = createElement("button", { className: "button button-ghost button-small", text: "Review job", type: "button" });
    review.addEventListener("click", () => {
      switchView("jobs");
      byId("job-search").value = job.company || job.title || "";
      renderJobs();
    });
    actions.append(review);
    if (safeHttpUrl(job.apply_url)) {
      const open = createElement("button", { className: "link-button", text: "Open source ↗", type: "button" });
      open.addEventListener("click", () => openExternal(job.apply_url));
      actions.append(open);
    }
    clipping.append(
      sourceLine,
      createElement("h3", { text: job.title || "Untitled role" }),
      createElement("p", { text: `${job.company || "Unknown company"}${job.location ? ` · ${job.location}` : ""}` }),
      actions,
    );
    container.append(clipping);
  }
}

function activeParsedResume() {
  return state.resumes.find((resume) => resume.is_active !== false && resume.parse_status === "parsed") || null;
}

function inferredRoleDirections() {
  const preferences = state.profile?.preferences && typeof state.profile.preferences === "object"
    ? state.profile.preferences
    : {};
  const candidates = [
    preferences.target_roles,
    state.resumeDiscoveryPlan?.roles,
    state.fitSummary?.recommended_roles,
    state.resumeSuggestions?.target_roles,
  ];
  for (const value of candidates) {
    if (Array.isArray(value)) {
      const roles = value.filter((item) => typeof item === "string" && item.trim()).map((item) => item.trim()).slice(0, 5);
      if (roles.length) return roles;
    }
  }
  return [];
}

function discoveryJobStatus(job) {
  return String(job?.status || "queued").toLowerCase();
}

function discoveryJobIsTerminal(job) {
  return DISCOVERY_TERMINAL_STATUSES.has(discoveryJobStatus(job));
}

function discoveryRunIsActive(run = state.discoveryRun) {
  if (!run || run.finished) return false;
  if (!Array.isArray(run.jobs) || !run.jobs.length) return Array.isArray(run.jobIds) && run.jobIds.length > 0;
  return run.jobs.some((job) => !discoveryJobIsTerminal(job));
}

function recoverDiscoveryRunFromAutomationJobs() {
  const groups = new Map();
  for (const job of state.automationJobs) {
    if (!["discover_linkedin_guest", "discover_public_feeds"].includes(job?.kind)) continue;
    if (discoveryJobIsTerminal(job)) continue;
    const match = String(job.idempotency_key || "").match(/^(resume-search-.+):(linkedin|feeds)$/);
    if (!match || typeof job.id !== "string") continue;
    const key = match[1];
    const group = groups.get(key) || { key, jobs: [], startedAt: 0 };
    group.jobs.push(job);
    const createdAt = Date.parse(job.created_at || job.updated_at || "");
    group.startedAt = Math.max(group.startedAt, Number.isFinite(createdAt) ? createdAt : 0);
    groups.set(key, group);
  }
  const latest = Array.from(groups.values()).sort((a, b) => b.startedAt - a.startedAt)[0];
  if (!latest?.jobs.length) return null;
  return {
    jobIds: latest.jobs.map((job) => job.id).slice(0, 2),
    jobs: latest.jobs.slice(0, 2),
    startedAt: latest.startedAt || Date.now(),
    monitoring: false,
  };
}

function discoveryCheckpoint(job) {
  const status = discoveryJobStatus(job);
  const source = job?.kind === "discover_linkedin_guest" ? "LinkedIn" : "Telegram + RSS";
  const copy = {
    queued: ["Filed", "The worker will collect this source next"],
    running: ["Collecting", "The worker is reading this public source"],
    succeeded: ["Saved", "Matches from this source are in your workspace"],
    failed: ["Stopped", "Open Activity to review the safe error"],
    cancelled: ["Cancelled", "This source was not collected"],
    needs_attention: ["Needs review", "Open Activity to continue safely"],
  }[status] || [humanize(status), "Open Activity for the latest worker status"];
  return { source, status, label: copy[0], detail: copy[1] };
}

function renderDiscoveryCheckpoints(container, jobs, compact = false) {
  if (!container) return;
  const signature = jobs.map((job) => `${job?.id || job?.kind || "source"}:${discoveryJobStatus(job)}`).join("|");
  if (container.dataset.statusSignature === signature) return;
  container.dataset.statusSignature = signature;
  clearNode(container);
  for (const job of jobs) {
    const checkpoint = discoveryCheckpoint(job);
    const item = createElement("span", {
      className: `discovery-checkpoint status-${checkpoint.status}`,
      attrs: { title: checkpoint.detail },
    }, [
      createElement("span", { className: "discovery-checkpoint-mark", attrs: { "aria-hidden": "true" } }),
      createElement("span", {}, [
        createElement("strong", { text: checkpoint.source }),
        createElement("small", { text: checkpoint.label }),
      ]),
    ]);
    if (!compact) item.append(createElement("span", { className: "sr-only", text: `. ${checkpoint.detail}` }));
    container.append(item);
  }
}

function renderWorkflowDock(run, jobs, { running, failed } = {}) {
  const dock = byId("workflow-dock");
  if (!dock) return;
  if (!run) {
    dock.hidden = true;
    setAriaBusy(dock, false);
    delete dock.dataset.statusSignature;
    return;
  }
  const runId = (run.jobIds || []).join(":") || String(run.startedAt || "current-run");
  if (state.workflowDockDismissedRunId === runId) {
    dock.hidden = true;
    setAriaBusy(dock, false);
    return;
  }
  dock.hidden = false;
  const active = !run.finished && !run.timedOut;
  byId("workflow-dock-dismiss").hidden = active;
  setAriaBusy(dock, active);
  dock.classList.toggle("is-complete", Boolean(run.finished && !failed));
  dock.classList.toggle("needs-attention", Boolean(failed || run.timedOut));
  if (run.finished) {
    setText("workflow-dock-title", failed ? "Search closed with a source warning" : "Search complete");
    setText("workflow-dock-detail", failed ? "Successful matches were kept. Open Activity for the source that needs review." : "Fresh jobs and forms are ready to review.");
  } else if (run.timedOut) {
    setText("workflow-dock-title", "Search continues in the background");
    setText("workflow-dock-detail", "Open Activity for the latest durable worker status.");
  } else if (running) {
    setText("workflow-dock-title", "Collecting public job matches");
    setText("workflow-dock-detail", "You can keep using AutoApply while each source moves through its checkpoint.");
  } else {
    setText("workflow-dock-title", "Search filed with the worker");
    setText("workflow-dock-detail", "The first collector will begin when the worker claims the run.");
  }
  renderDiscoveryCheckpoints(byId("workflow-dock-checkpoints"), jobs, true);
}

function renderResumeDiscoveryProgress(run = state.discoveryRun) {
  const panel = byId("resume-discovery-progress");
  if (!panel) return;
  if (!run) {
    panel.hidden = true;
    setAriaBusy(panel, false);
    renderWorkflowDock(null, []);
    return;
  }
  panel.hidden = false;
  const jobs = Array.isArray(run.jobs) ? run.jobs : [];
  const total = Math.max(run.jobIds?.length || 0, jobs.length);
  const finished = jobs.filter(discoveryJobIsTerminal).length;
  const running = jobs.filter((job) => discoveryJobStatus(job) === "running").length;
  const failed = jobs.filter((job) => ["failed", "cancelled", "needs_attention"].includes(discoveryJobStatus(job))).length;
  const allQueued = jobs.length > 0 && jobs.every((job) => discoveryJobStatus(job) === "queued");
  const elapsed = Date.now() - Number(run.startedAt || Date.now());
  setText("resume-discovery-progress-count", total ? `${Math.min(finished, total)} of ${total} complete` : "Checking status");
  setAriaBusy(panel, !run.finished && !run.timedOut);
  if (run.finished) {
    setText("resume-discovery-progress-title", failed ? "Search finished with a source warning" : "Search complete");
  } else if (running) {
    setText("resume-discovery-progress-title", "Searching LinkedIn, Telegram, and RSS…");
  } else {
    setText("resume-discovery-progress-title", "Starting the public-source search…");
  }
  let detail = jobs.length
    ? jobs.map((job) => `${job.kind === "discover_linkedin_guest" ? "LinkedIn" : "Telegram + RSS"}: ${humanize(discoveryJobStatus(job))}`).join(" · ")
    : "Connecting to the background worker…";
  if (!run.finished && allQueued && elapsed >= 15_000) {
    detail = "Your search is safely queued. You can keep using AutoApply and open Activity for the latest worker status.";
  } else if (run.timedOut) {
    detail = "The search is still running in the background. Keep this page open or use Activity for diagnostics.";
  } else if (run.finished) {
    detail = "Fresh jobs and the Form Pilot inbox were refreshed automatically.";
  }
  renderDiscoveryCheckpoints(byId("resume-discovery-checkpoints"), jobs);
  renderWorkflowDock(run, jobs, { running, failed });
  setText("resume-discovery-progress-detail", detail);
}

function discoveryCompletionSummary(jobs) {
  let saved = 0;
  let sourceWarnings = 0;
  let hardFailures = 0;
  for (const job of jobs) {
    const result = job?.result && typeof job.result === "object" ? job.result : {};
    const savedCount = Number(result.saved_count);
    if (Number.isFinite(savedCount) && savedCount > 0) saved += savedCount;
    if (Array.isArray(result.source_errors)) sourceWarnings += result.source_errors.length;
    if (["failed", "cancelled", "needs_attention"].includes(discoveryJobStatus(job))) hardFailures += 1;
  }
  return { saved, sourceWarnings, hardFailures };
}

function waitForDiscoveryPoll() {
  return new Promise((resolve) => window.setTimeout(resolve, DISCOVERY_POLL_INTERVAL_MS));
}

async function monitorResumeDiscoveryRun(run, identity = identitySnapshot()) {
  if (state.discoveryMonitorPromise) return state.discoveryMonitorPromise;
  state.discoveryRun = run;
  run.monitoring = true;
  saveDiscoveryRun(run, identity.userId);
  renderResumeDiscoveryProgress(run);
  renderResumeDiscoveryPlan();

  const monitor = (async () => {
    let consecutiveErrors = 0;
    let firstPoll = true;
    while (firstPoll || Date.now() - run.startedAt < DISCOVERY_MONITOR_TIMEOUT_MS) {
      firstPoll = false;
      assertCurrentIdentity(identity);
      try {
        const jobs = await Promise.all(run.jobIds.map(async (jobId) => {
          const payload = await apiRequest(`/automation-jobs/${encodeURIComponent(jobId)}`, { identity });
          return unwrapData(payload) || {};
        }));
        consecutiveErrors = 0;
        run.jobs = jobs;
        renderResumeDiscoveryProgress(run);
        if (jobs.length && jobs.every(discoveryJobIsTerminal)) {
          run.finished = true;
          run.monitoring = false;
          clearDiscoveryRun(identity.userId);
          await Promise.all([
            loadJobs(true, identity),
            loadGoogleForms(true, identity),
            loadAutomationJobs(true, identity),
          ]);
          const summary = discoveryCompletionSummary(jobs);
          if (summary.saved > 0) {
            const warningCopy = summary.hardFailures || summary.sourceWarnings
              ? " One source needs attention, but its successful matches were kept."
              : "";
            showDiscoveryResult(`${summary.saved} matching job${summary.saved === 1 ? " was" : "s were"} saved. Results and Form Pilot refreshed automatically.${warningCopy}`, warningCopy ? "warning" : "success");
          } else if (summary.hardFailures) {
            showDiscoveryResult("The search finished, but a public source could not complete. Review Activity for the safe error details, then try again.", "error");
          } else {
            showDiscoveryResult("Search complete. No new matching jobs were found in this bounded run.", "info");
          }
          renderResumeDiscoveryProgress(run);
          renderResumeDiscoveryPlan();
          return jobs;
        }
      } catch (error) {
        if (isIdentityChanged(error)) throw error;
        consecutiveErrors += 1;
        if (consecutiveErrors >= 3) throw error;
      }
      await waitForDiscoveryPoll();
    }
    run.timedOut = true;
    run.monitoring = false;
    renderResumeDiscoveryProgress(run);
    showDiscoveryResult("The search is taking longer than usual and is still running in the background. Results will remain safe; Activity is available for diagnostics.", "warning");
    return run.jobs;
  })();

  state.discoveryMonitorPromise = monitor;
  try {
    return await monitor;
  } finally {
    if (state.discoveryMonitorPromise === monitor) state.discoveryMonitorPromise = null;
    run.monitoring = false;
    if (isCurrentIdentity(identity)) {
      renderResumeDiscoveryProgress(run);
      renderResumeDiscoveryPlan();
    }
  }
}

function resumeDiscoveryMonitoring(identity = identitySnapshot()) {
  if (state.discoveryMonitorPromise) return;
  const stored = loadDiscoveryRun(identity.userId);
  const recovered = stored || recoverDiscoveryRunFromAutomationJobs();
  if (!recovered) return;
  state.discoveryRun = recovered;
  saveDiscoveryRun(recovered, identity.userId);
  monitorResumeDiscoveryRun(recovered, identity).catch((error) => {
    if (!isIdentityChanged(error)) {
      showDiscoveryResult(errorMessage(error, "The live search status could not be refreshed."), "error");
    }
  });
}

function renderResumeDiscoveryPlan(plan = state.resumeDiscoveryPlan) {
  const container = byId("resume-discovery-role-list");
  const status = byId("resume-discovery-status");
  const run = byId("resume-discovery-run");
  if (!container || !status || !run) return;
  clearNode(container);
  const roles = Array.isArray(plan?.roles) ? plan.roles : inferredRoleDirections();
  if (roles.length) {
    roles.forEach((role) => container.append(createElement("span", { className: "chip status-neutral", text: role })));
  } else {
    container.append(createElement("span", { className: "muted", text: "Your target roles will appear after résumé analysis." }));
  }
  const hasResume = Boolean(activeParsedResume());
  const hasGroq = credentialConfigured("groq");
  const activeRun = discoveryRunIsActive();
  run.disabled = !hasResume || !hasGroq || activeRun || run.dataset.busy === "true";
  if (!hasResume || !hasGroq) {
    status.textContent = `${!hasResume ? "Upload and parse a résumé" : "Résumé ready"}; ${!hasGroq ? "connect a Groq key in Profile or Connections" : "Groq key ready"}.`;
    status.className = "form-message is-error";
  } else if (activeRun) {
    status.textContent = "Search in progress. Fresh jobs and Google Forms will appear automatically when the public collectors finish.";
    status.className = "form-message is-success";
  } else if (plan) {
    const sourceCopy = "LinkedIn, Telegram, and RSS";
    status.textContent = `Ready to search ${sourceCopy} for ${roles.join(", ")}${plan.location ? ` around ${plan.location}` : ""}.`;
    status.className = "form-message is-success";
  } else {
    status.textContent = "Your résumé and Groq key are ready. One search will dispatch all three public collectors.";
    status.className = "form-message";
  }
  const planNode = byId("resume-discovery-plan");
  if (planNode) planNode.hidden = false;
  renderResumeDiscoveryProgress();
}

async function submitResumeGuidedDiscovery(event) {
  event.preventDefault();
  const button = event.submitter || byId("resume-discovery-run");
  if (!activeParsedResume() || !credentialConfigured("groq")) {
    renderResumeDiscoveryPlan();
    toast("Complete the résumé and Groq setup in Profile first.", "error");
    return;
  }
  if (discoveryRunIsActive() || state.discoveryMonitorPromise) {
    showDiscoveryResult("Your current search is already running. Results will refresh here automatically.", "info");
    return;
  }
  const identity = identitySnapshot();
  await withBusy(button, "Finding jobs…", async () => {
    try {
      const payload = await apiRequest("/discovery/resume-guided", {
        method: "POST",
        groq: true,
        body: {
          location: byId("discovery-location")?.value.trim() || null,
          remote_only: Boolean(byId("discovery-remote-only")?.checked),
          linkedin_limit: 20,
          feed_limit: 60,
          idempotency_key: discoveryRunKey("resume-search"),
        },
      });
      const data = unwrapData(payload) || {};
      state.resumeDiscoveryPlan = data.plan && typeof data.plan === "object" ? data.plan : null;
      const jobs = Array.isArray(data.automation_jobs) ? data.automation_jobs.filter((job) => typeof job?.id === "string") : [];
      if (!jobs.length) throw new AppError("The search was accepted but returned no trackable work.", "discovery_jobs_missing");
      const run = {
        jobIds: jobs.map((job) => job.id).slice(0, 2),
        jobs: jobs.slice(0, 2),
        startedAt: Date.now(),
        monitoring: false,
      };
      state.discoveryRun = run;
      showDiscoveryResult("Search started. Keep this page open—matching jobs and Google Forms will refresh here automatically.", "success");
      renderResumeDiscoveryPlan();
      await monitorResumeDiscoveryRun(run, identity);
    } catch (error) {
      if (isIdentityChanged(error)) return;
      setFormMessage("resume-discovery-status", errorMessage(error, "The résumé-guided search could not be queued."), "error");
      showDiscoveryResult(errorMessage(error, "The résumé-guided search could not be queued."), "error");
    }
  });
  renderResumeDiscoveryPlan();
}

async function loadGoogleForms(quiet = false, identity = identitySnapshot()) {
  const container = byId("google-form-queue");
  if (!quiet && container) showLoading(container, 2);
  try {
    const payload = await apiRequest("/discovery/google-forms?limit=100&offset=0", { identity });
    state.googleForms = unwrapItems(payload, ["forms"]);
    state.googleFormsTotal = Number.isInteger(payload?.total) ? payload.total : state.googleForms.length;
    renderGoogleFormQueue();
    return state.googleForms;
  } catch (error) {
    state.googleForms = [];
    state.googleFormsTotal = 0;
    renderGoogleFormQueue();
    if (!quiet) throw error;
    return [];
  }
}

function googleFormApplication(entry) {
  const current = state.formApplications.find(
    (application) => application.job_id === entry?.job_id && application.channel === "ats",
  );
  if (current) return current;
  return entry?.application && typeof entry.application === "object" && entry.application.channel === "ats"
    ? entry.application
    : null;
}

async function continueGoogleFormReview(entry) {
  const application = googleFormApplication(entry);
  if (!application?.id) return;
  if (!state.formApplications.some((item) => item.id === application.id)) state.formApplications.unshift(application);
  await openFormApplicationReview(application.id);
}

async function saveAndScanGoogleForm(entry, button) {
  await withBusy(button, "Saving form…", async () => {
    try {
      const payload = await apiRequest("/discovery/ats", {
        method: "POST",
        body: { urls: [entry.apply_url] },
      });
      const saved = unwrapItems(payload, ["jobs", "items"])[0];
      if (!saved?.id) throw new AppError("The Google Form was saved but could not be opened for review.", "google_form_save_incomplete");
      await Promise.all([loadJobs(true), loadGoogleForms(true)]);
      await scanJobApplication(saved, "google_forms", null);
    } catch (error) {
      setFormMessage("google-form-queue-status", errorMessage(error, "The Google Form could not be saved."), "error");
    }
  });
}

function renderGoogleFormQueue() {
  const container = byId("google-form-queue");
  if (!container) return;
  clearNode(container);
  setText("google-form-count", `${state.googleFormsTotal} form${state.googleFormsTotal === 1 ? "" : "s"}`);
  const badge = byId("form-count-badge");
  if (badge) {
    badge.hidden = state.googleFormsTotal <= 0;
    badge.textContent = state.googleFormsTotal > 99 ? "99+" : String(state.googleFormsTotal);
    badge.setAttribute("aria-label", `${state.googleFormsTotal} Google Form${state.googleFormsTotal === 1 ? "" : "s"} ready in Form Pilot`);
  }
  if (!state.googleForms.length) {
    container.append(emptyState("No Google Forms waiting", "Parse a referral alert, add a single form, or run Find jobs. Detected forms will appear here automatically.", "GF"));
    setFormMessage("google-form-queue-status", "Your Form Pilot inbox is clear.");
    return;
  }
  for (const entry of state.googleForms) {
    const application = googleFormApplication(entry);
    const item = createElement("article", { className: "google-form-queue-item" });
    const copy = createElement("div", { className: "google-form-queue-copy" }, [
      createElement("strong", { text: `${entry.title || "Application form"}${entry.company ? ` — ${entry.company}` : ""}` }),
      createElement("small", { text: `${humanize(entry.source || "discovery")} · ${application ? humanize(application.status || "captured") : entry.saved ? "Ready to scan" : "Found inside a lead"}` }),
    ]);
    const actions = createElement("div", { className: "google-form-queue-actions" });
    if (application?.id) {
      const review = createElement("button", { className: "button button-primary button-small", text: "Review prepared form", type: "button" });
      review.addEventListener("click", () => continueGoogleFormReview(entry));
      actions.append(review);
    } else if (entry.saved && entry.job_id) {
      const scan = createElement("button", { className: "button button-primary button-small", text: "Prepare form", type: "button" });
      scan.addEventListener("click", () => {
        const savedJob = state.jobs.find((job) => job.id === entry.job_id) || { ...entry, id: entry.job_id };
        scanJobApplication(savedJob, "google_forms", scan);
      });
      actions.append(scan);
    } else {
      const save = createElement("button", { className: "button button-primary button-small", text: "Prepare form", type: "button" });
      save.addEventListener("click", () => saveAndScanGoogleForm(entry, save));
      actions.append(save);
    }
    if (safeHttpUrl(entry.apply_url)) {
      const open = createElement("button", { className: "link-button", text: "Open form ↗", type: "button" });
      open.addEventListener("click", () => openExternal(entry.apply_url));
      actions.append(open);
    }
    item.append(copy, actions);
    container.append(item);
  }
  setFormMessage("google-form-queue-status", `${state.googleFormsTotal} Google Form${state.googleFormsTotal === 1 ? " is" : "s are"} ready for preparation and required review.`);
}

function importedCount(payload) {
  const data = unwrapData(payload) || {};
  for (const value of [data.imported, data.saved, data.count, payload?.count]) {
    if (Number.isInteger(value) && value >= 0) return value;
  }
  const items = unwrapItems(payload, ["jobs", "items"]);
  return items.length;
}

function setFormIntakeMode(mode, focus = true) {
  const single = mode === "single";
  all("[data-form-intake-mode]").forEach((button) => {
    const active = button.dataset.formIntakeMode === (single ? "single" : "digest");
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  byId("referral-ingest-form").hidden = single;
  byId("google-form-intake-form").hidden = !single;
  setText("referral-ingest-heading", single ? "Paste Google Form link" : "Paste referral alert");
  setText(
    "form-pilot-intake-copy",
    single
      ? "Add one public Google Form directly, then start preparing its questions and résumé-grounded answers."
      : "Paste the complete Telegram, WhatsApp, or email digest. AutoApply separates each numbered role and keeps the useful application route.",
  );
  const summary = byId("referral-route-summary");
  summary.hidden = single || summary.dataset.ready !== "true";
  if (focus) requestAnimationFrame(() => byId(single ? "google-form-url" : "referral-digest")?.focus());
}

function renderReferralRouteSummary(summary = {}) {
  const parsed = Number.isInteger(summary.parsed) ? summary.parsed : 0;
  const saved = Number.isInteger(summary.saved) ? summary.saved : parsed;
  const googleForms = Number.isInteger(summary.google_forms) ? summary.google_forms : 0;
  const emailApply = Number.isInteger(summary.email_apply) ? summary.email_apply : 0;
  const ignored = Number.isInteger(summary.ignored_promotional) ? summary.ignored_promotional : 0;
  const other = Math.max(0, parsed - googleForms - emailApply);
  const panel = byId("referral-route-summary");
  panel.dataset.ready = "true";
  panel.hidden = false;
  setText("referral-route-total", `${saved} saved`);
  setText("referral-route-forms", googleForms);
  setText("referral-route-emails", emailApply);
  const notes = [];
  if (ignored) notes.push(`${ignored} promotional link${ignored === 1 ? " was" : "s were"} ignored`);
  if (other) notes.push(`${other} direct application link${other === 1 ? " was" : "s were"} saved with the jobs`);
  notes.push("No form was submitted and no email was sent");
  setText("referral-route-ignored", `${notes.join(" · ")}.`);
}

async function ingestReferralDigest(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter || form.querySelector('button[type="submit"]');
  const text = byId("referral-digest").value.trim();
  setFormMessage("referral-ingest-status");
  if (text.length < 20) {
    setFormMessage("referral-ingest-status", "Paste the full referral message, including at least one application link or email address.", "error");
    byId("referral-digest").focus();
    return;
  }
  await withBusy(button, "Parsing opportunities…", async () => {
    try {
      const payload = await apiRequest("/discovery/referrals", {
        method: "POST",
        body: { text },
      });
      const summary = payload?.summary && typeof payload.summary === "object"
        ? payload.summary
        : { parsed: importedCount(payload), saved: importedCount(payload) };
      if (!summary.parsed) {
        byId("referral-route-summary").hidden = true;
        setFormMessage("referral-ingest-status", "No application route was found. Include each job's Google Form link or application email.", "error");
        return;
      }
      form.reset();
      await Promise.all([loadJobs(true), loadGoogleForms(true)]);
      renderReferralRouteSummary(summary);
      const forms = Number(summary.google_forms || 0);
      const emails = Number(summary.email_apply || 0);
      setFormMessage(
        "referral-ingest-status",
        `${summary.saved ?? summary.parsed} opportunities saved · ${forms} form${forms === 1 ? "" : "s"} ready here · ${emails} email application${emails === 1 ? "" : "s"} ready for Mass Cold Email.`,
        "success",
      );
    } catch (error) {
      setFormMessage("referral-ingest-status", errorMessage(error, "The referral message could not be parsed."), "error");
    }
  });
}

async function importJobFile(event) {
  event.preventDefault();
  const button = event.submitter;
  const file = state.pendingJobImportFile || byId("job-import-file").files?.[0];
  if (!file) {
    showDiscoveryResult("Choose a CSV or XLSX file first.", "error");
    return;
  }
  if (file.size <= 0 || file.size > 4 * 1024 * 1024) {
    showDiscoveryResult("The import file must be 4 MB or smaller.", "error");
    return;
  }
  await withBusy(button, "Importing…", async () => {
    try {
      const body = new FormData();
      body.append("file", file, file.name);
      const payload = await apiRequest("/discovery/import", { method: "POST", body });
      const count = importedCount(payload);
      byId("job-import-form").reset();
      state.pendingJobImportFile = null;
      setText("job-import-file-label", "Title and company are required; common header names are accepted.");
      await Promise.all([loadJobs(true), loadGoogleForms(true)]);
      showDiscoveryResult(`${count} spreadsheet row${count === 1 ? "" : "s"} saved after normalization.`, "success");
    } catch (error) {
      showDiscoveryResult(errorMessage(error, "The spreadsheet could not be imported."), "error");
    }
  });
}

async function ingestAtsLinks(event) {
  event.preventDefault();
  const button = event.submitter;
  const urls = byId("ats-links").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  await withBusy(button, "Detecting…", async () => {
    try {
      const payload = await apiRequest("/discovery/ats", { method: "POST", body: { urls } });
      const count = importedCount(payload);
      byId("ats-link-form").reset();
      await Promise.all([loadJobs(true), loadGoogleForms(true)]);
      showDiscoveryResult(`${count} supported public application link${count === 1 ? " was" : "s were"} saved.`, "success");
    } catch (error) {
      showDiscoveryResult(errorMessage(error, "Those ATS links could not be saved."), "error");
    }
  });
}

async function addGoogleFormToPilot(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter || form.querySelector('button[type="submit"]');
  const input = byId("google-form-url");
  const rawUrl = input?.value.trim() || "";
  const url = safeHttpUrl(rawUrl);
  setFormMessage("google-form-intake-status");
  if (!url || providerForJob({ apply_url: url }) !== "google_forms") {
    setFormMessage("google-form-intake-status", "Paste a valid forms.gle or docs.google.com/forms link.", "error");
    input?.focus();
    return;
  }

  let savedJob = null;
  await withBusy(button, "Adding form…", async () => {
    try {
      const payload = await apiRequest("/discovery/ats", {
        method: "POST",
        body: { urls: [url] },
      });
      savedJob = unwrapItems(payload, ["jobs", "items"])[0] || null;
      if (!savedJob?.id) throw new AppError("The form was accepted but could not be opened for preparation.", "google_form_save_incomplete");
      form.reset();
      await Promise.all([loadJobs(true), loadGoogleForms(true)]);
      setFormMessage("google-form-intake-status", "Form added. Starting preparation…", "success");
    } catch (error) {
      setFormMessage("google-form-intake-status", errorMessage(error, "The Google Form could not be added."), "error");
    }
  });

  if (!savedJob) return;
  const preparationStarted = await scanJobApplication(savedJob, "google_forms", button);
  if (preparationStarted) {
    setFormMessage("google-form-intake-status", "Form saved and preparation queued. Review opens next.", "success");
  } else {
    setFormMessage("google-form-intake-status", "The form is saved in Form Pilot. Complete the required connection, then choose Prepare form.", "error");
  }
}

async function queueAtsBoardDiscovery(event) {
  event.preventDefault();
  const button = event.submitter;
  const urls = byId("ats-board-links").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  await withBusy(button, "Queueing…", async () => {
    try {
      await apiRequest("/discovery/ats/boards", {
        method: "POST",
        body: {
          urls,
          limit: 100,
          idempotency_key: discoveryRunKey("public-ats"),
        },
      });
      byId("ats-board-form").reset();
      showDiscoveryResult("Public ATS board scan queued. A running background worker is required; follow the run in Activity.", "success");
      await loadAutomationJobs(true);
    } catch (error) {
      showDiscoveryResult(errorMessage(error, "Those public ATS boards could not be queued."), "error");
    }
  });
}

async function loadJobs(quiet = false, identity = identitySnapshot(), append = false) {
  const container = byId("job-list");
  if (!quiet && !append) showLoading(container);
  const offset = append ? state.jobs.length : 0;
  const payload = await apiRequest(`/jobs?limit=50&offset=${offset}`, { identity });
  const page = unwrapItems(payload, ["jobs"]);
  if (append) {
    const known = new Set(state.jobs.map((job) => job.id));
    state.jobs.push(...page.filter((job) => !known.has(job.id)));
  } else {
    state.jobs = page;
    state.fitSummary = payload?.fit_summary && typeof payload.fit_summary === "object"
      ? payload.fit_summary
      : {};
  }
  state.jobsHasMore = page.length === 50;
  byId("jobs-load-more").hidden = !state.jobsHasMore;
  renderJobs();
  renderDiscoveryJobs();
  renderOutreach();
  renderOverview();
  renderJobIntelligence();
  renderYcDesk();
  return state.jobs;
}

function jobPayloadFromForm() {
  const nullable = (id) => byId(id).value.trim() || null;
  const description = byId("job-description").value.trim();
  if (description.length < 20) throw new AppError("The job description must contain at least 20 characters.", "description_too_short");
  return {
    source: "manual",
    apply_url: nullable("job-url"),
    title: byId("job-title").value.trim(),
    company: byId("job-company").value.trim(),
    location: nullable("job-location"),
    description,
    contact_email: nullable("job-contact"),
    metadata: {},
  };
}

async function saveJob(event) {
  event.preventDefault();
  const button = event.submitter;
  await withBusy(button, byId("job-edit-id").value ? "Updating…" : "Saving…", async () => {
    try {
      const full = jobPayloadFromForm();
      const editing = byId("job-edit-id").value;
      const body = editing
        ? {
            apply_url: full.apply_url,
            title: full.title,
            company: full.company,
            location: full.location,
            description: full.description,
            contact_email: full.contact_email,
          }
        : full;
      await apiRequest(editing ? `/jobs/${encodeURIComponent(editing)}` : "/jobs", {
        method: editing ? "PATCH" : "POST",
        body,
      });
      resetJobForm();
      await loadJobs(true);
      toast(editing ? "Job updated." : "Job added to your workspace.", "success");
    } catch (error) {
      toast(errorMessage(error, "The job could not be saved."), "error");
    }
  });
}

function resetJobForm() {
  byId("job-form").reset();
  byId("job-edit-id").value = "";
  setText("job-form-title", "Add a job");
  setText("job-submit", "Save job");
  byId("job-cancel-edit").hidden = true;
}

function editJob(job) {
  byId("job-edit-id").value = job.id;
  byId("job-title").value = job.title || "";
  byId("job-company").value = job.company || "";
  byId("job-location").value = job.location || "";
  byId("job-contact").value = job.contact_email || "";
  byId("job-url").value = job.apply_url || "";
  byId("job-description").value = job.description || "";
  setText("job-form-title", "Edit job");
  setText("job-submit", "Update job");
  byId("job-cancel-edit").hidden = false;
  byId("job-form").scrollIntoView({ behavior: "smooth", block: "start" });
  byId("job-title").focus();
}

function filteredJobs() {
  const query = byId("job-search").value.trim().toLowerCase();
  const status = byId("job-status-filter").value;
  const filtered = state.jobs.filter((job) => {
    const matchesText = !query || `${job.title || ""} ${job.company || ""} ${job.location || ""}`.toLowerCase().includes(query);
    const matchesStatus = !status || job.status === status || (status === "archived" && Boolean(job.archived_at));
    return matchesText && matchesStatus;
  });
  if (byId("job-sort")?.value === "fit") {
    return filtered.sort((left, right) => {
      const leftScore = left.fit?.evaluated && Number.isFinite(left.fit?.score) ? left.fit.score : -1;
      const rightScore = right.fit?.evaluated && Number.isFinite(right.fit?.score) ? right.fit.score : -1;
      if (rightScore !== leftScore) return rightScore - leftScore;
      return new Date(right.created_at || 0) - new Date(left.created_at || 0);
    });
  }
  return filtered.sort((left, right) => new Date(right.created_at || 0) - new Date(left.created_at || 0));
}

function exactYcJobUrl(value) {
  const normalized = safeHttpUrl(value);
  if (!normalized) return null;
  const url = new URL(normalized);
  const host = url.hostname.toLowerCase();
  const parts = url.pathname.split("/").filter(Boolean);
  const companySlug = (part) => /^[a-z0-9](?:[a-z0-9-]{0,98}[a-z0-9])?$/i.test(part || "");
  const currentJobSlug = (part) => /^[a-z0-9]{5,64}(?:-[a-z0-9]+)*$/i.test(part || "");
  const current = ["ycombinator.com", "www.ycombinator.com"].includes(host)
    && parts.length === 4
    && parts[0] === "companies"
    && parts[2] === "jobs"
    && companySlug(parts[1])
    && currentJobSlug(parts[3]);
  if (!current || url.protocol !== "https:" || url.port || url.username || url.password) return null;
  url.protocol = "https:";
  url.hostname = "www.ycombinator.com";
  url.pathname = `/${parts.join("/")}`;
  url.search = "";
  url.hash = "";
  return url.href;
}

function titleCaseSlug(value) {
  return decodeURIComponent(String(value || ""))
    .replace(/^[A-Za-z0-9]{5,64}-/, "")
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase())
    .trim();
}

function inferYcJobIdentity(rawUrl) {
  const exact = exactYcJobUrl(rawUrl);
  if (!exact) return { url: null, company: "", title: "", externalId: null };
  const parts = new URL(exact).pathname.split("/").filter(Boolean);
  const companySlug = parts[0] === "companies" ? parts[1] : "";
  const jobSlug = parts.at(-1) || "";
  return {
    url: exact,
    company: titleCaseSlug(companySlug),
    title: titleCaseSlug(jobSlug),
    externalId: jobSlug.slice(0, 255) || null,
  };
}

function ycProvider() {
  return mergedProviders().find(
    (provider) => String(provider.id || provider.provider || "").toLowerCase() === "yc",
  ) || null;
}

function ycConnectionReady() {
  const provider = ycProvider();
  return Boolean(provider && ["connected", "active"].includes(connectionStatus(provider)));
}

function ycJobForExactUrl(targetUrl, jobs = state.jobs) {
  if (!targetUrl) return null;
  return jobs.find((job) => exactYcJobUrl(job?.apply_url) === targetUrl) || null;
}

async function findSavedYcJob(targetUrl, identity = identitySnapshot()) {
  const loaded = ycJobForExactUrl(targetUrl);
  if (loaded) return loaded;
  let offset = 0;
  while (offset <= 10_000) {
    const payload = await apiRequest(`/jobs?limit=50&offset=${offset}`, { identity });
    const page = unwrapItems(payload, ["jobs"]);
    const found = ycJobForExactUrl(targetUrl, page);
    if (found) {
      if (!state.jobs.some((job) => job.id === found.id)) state.jobs.push(found);
      return found;
    }
    if (page.length < 50) break;
    offset += page.length;
  }
  return null;
}

function applyYcIntakeDefaults(force = false) {
  const form = byId("yc-job-form");
  if (!form || (!force && form.contains(document.activeElement))) return;
  const preferences = state.ycPreferences && typeof state.ycPreferences === "object"
    ? state.ycPreferences
    : { query: "", remote_only: false, limit: 10 };
  const defaults = [
    [byId("yc-job-title"), String(preferences.query || "").trim()],
    [byId("yc-job-location"), preferences.remote_only === true ? "Remote" : ""],
  ];
  for (const [field, nextDefault] of defaults) {
    if (!field) continue;
    const previousDefault = field.dataset.ycIntakeDefault || "";
    const currentValue = field.value.trim();
    if (currentValue && currentValue !== previousDefault) {
      delete field.dataset.ycIntakeDefault;
      continue;
    }
    if (nextDefault) {
      field.value = nextDefault;
      field.dataset.ycIntakeDefault = nextDefault;
    } else if (!currentValue || currentValue === previousDefault) {
      field.value = "";
      delete field.dataset.ycIntakeDefault;
    }
  }
}

function setYcRouteStep(id, { complete = false, current = false, attention = false } = {}) {
  const step = byId(id);
  if (!step) return;
  step.classList.toggle("is-ready", complete);
  step.classList.toggle("is-current", current);
  step.classList.toggle("needs-attention", attention);
  if (current) step.setAttribute("aria-current", "step");
  else step.removeAttribute("aria-current");
  const heading = step.querySelector("strong")?.textContent || "YC workflow step";
  step.setAttribute("aria-label", `${heading}: ${complete ? "complete" : attention ? "needs attention" : current ? "current" : "not started"}`);
}

function renderYcRouteProgress({ connected = ycConnectionReady(), connectionNeedsAttention = false } = {}) {
  const ycJobs = state.jobs.filter((job) => Boolean(exactYcJobUrl(job?.apply_url)));
  const jobIds = new Set(ycJobs.map((job) => job.id));
  const applications = state.formApplications.filter((application) => jobIds.has(application.job_id));
  const applicationIds = new Set(applications.map((application) => application.id));
  const revisions = applications.flatMap((application) => state.formRevisions[application.id] || []);
  const submissionJobs = state.automationJobs.filter(
    (job) => job.kind === "application_submit" && applicationIds.has(job.application_id),
  );
  const saved = ycJobs.length > 0;
  const captured = applications.length > 0 || revisions.length > 0;
  const reviewed = revisions.some(
    (revision) => Boolean(revision.approved_at) || ["approved", "submitted"].includes(String(revision.status || "").toLowerCase()),
  ) || applications.some((application) => ["approved", "queued", "sent", "applied"].includes(String(application.status || "").toLowerCase()));
  const submitted = revisions.some(formRevisionSubmissionIsVerified) || submissionJobs.some(formSubmissionIsVerified);
  const submissionNeedsAttention = submissionJobs.some(
    (job) => ["failed", "needs_attention"].includes(String(job.status || "").toLowerCase()),
  );

  setYcRouteStep("yc-route-connect", {
    complete: connected,
    current: !connected,
    attention: !connected && connectionNeedsAttention,
  });
  setYcRouteStep("yc-route-save", {
    complete: saved,
    current: connected && !saved,
  });
  setYcRouteStep("yc-route-review", {
    complete: reviewed || submitted,
    current: connected && saved && !reviewed,
  });
  setYcRouteStep("yc-route-submit", {
    complete: submitted,
    current: connected && saved && captured && reviewed && !submitted,
    attention: submissionNeedsAttention && !submitted,
  });
}

function renderYcDesk() {
  if (!byId("yc-application-desk")) return;
  const provider = ycProvider();
  const status = provider ? connectionStatus(provider) : "unavailable";
  const connected = ["connected", "active"].includes(status);
  const pending = status === "pending";
  const attention = status === "needs_attention";
  const browserbaseReady = credentialConfigured("browserbase");
  const capabilityReady = provider?.available !== false && provider?.can_connect !== false;
  const pill = byId("yc-connection-status");
  const connect = byId("yc-connect");
  const complete = byId("yc-complete-login");
  renderYcRouteProgress({ connected, connectionNeedsAttention: attention });
  pill.className = `status-pill ${connected ? "status-success" : pending ? "status-info" : attention ? "status-warning" : "status-neutral"}`;
  pill.textContent = connected ? "YC connected" : pending ? "Login waiting" : attention ? "YC needs attention" : provider ? "YC not connected" : "YC unavailable";
  connect.hidden = connected || pending;
  connect.disabled = !capabilityReady || !browserbaseReady;
  complete.hidden = !pending;
  setText(
    "yc-connection-detail",
    connected
      ? "Your isolated YC browser context is ready. Saving this exact job can start its review-gated preparation."
      : pending
        ? "Finish YC sign-in and MFA in the opened Live View, then return here and mark login complete."
        : !provider
          ? "This deployment has not published YC application support yet."
          : !browserbaseReady
            ? "Add your Browserbase key and Project ID in Connections before opening YC login."
            : attention
              ? "The saved YC browser context needs a fresh login before another application can be prepared."
              : "Open YC in your isolated browser. Your YC password is entered only on YC's own page.",
  );

  const preferences = state.ycPreferences && typeof state.ycPreferences === "object"
    ? state.ycPreferences
    : { query: "", remote_only: false, limit: 10 };
  if (!byId("yc-preferences-form")?.contains(document.activeElement)) {
    byId("yc-preference-query").value = preferences.query || "";
    byId("yc-preference-remote").checked = preferences.remote_only === true;
  }
  applyYcIntakeDefaults();
}

async function loadYcPreferences(quiet = false, identity = identitySnapshot()) {
  try {
    const payload = await apiRequest("/providers/yc/preferences", { identity });
    const preferences = unwrapData(payload);
    state.ycPreferences = preferences && typeof preferences === "object"
      ? preferences
      : { query: "", remote_only: false, limit: 10 };
    renderYcDesk();
    return state.ycPreferences;
  } catch (error) {
    state.ycPreferences = { query: "", remote_only: false, limit: 10 };
    renderYcDesk();
    if (!quiet) throw error;
    return state.ycPreferences;
  }
}

async function saveYcPreferences(event) {
  event.preventDefault();
  const button = event.submitter;
  const query = byId("yc-preference-query").value.trim();
  const remoteOnly = byId("yc-preference-remote").checked;
  setFormMessage("yc-preferences-status");
  await withBusy(button, "Saving…", async () => {
    try {
      const payload = await apiRequest("/providers/yc/preferences", {
        method: "PATCH",
        body: { query: query || null, remote_only: remoteOnly, limit: 10 },
      });
      state.ycPreferences = unwrapData(payload) || { query, remote_only: remoteOnly, limit: 10 };
      renderYcDesk();
      applyYcIntakeDefaults(true);
      setFormMessage("yc-preferences-status", "YC intake defaults saved. Blank role and location fields were updated; no YC pages were fetched.", "success");
    } catch (error) {
      setFormMessage("yc-preferences-status", errorMessage(error, "YC intake defaults could not be saved."), "error");
    }
  });
}

function inferYcFieldsFromUrl() {
  const identity = inferYcJobIdentity(byId("yc-job-url").value.trim());
  if (!identity.url) return;
  if (!byId("yc-job-company").value.trim() && identity.company) byId("yc-job-company").value = identity.company;
  const title = byId("yc-job-title");
  if ((!title.value.trim() || title.value.trim() === title.dataset.ycIntakeDefault) && identity.title) {
    title.value = identity.title;
    delete title.dataset.ycIntakeDefault;
  }
}

async function saveAndPrepareYcJob(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter || byId("yc-save-and-prepare");
  const jobIdentity = inferYcJobIdentity(byId("yc-job-url").value.trim());
  const title = byId("yc-job-title").value.trim();
  const company = byId("yc-job-company").value.trim();
  const location = byId("yc-job-location").value.trim();
  const description = byId("yc-job-description").value.trim();
  setFormMessage("yc-job-status");
  if (!jobIdentity.url) {
    setFormMessage("yc-job-status", "Paste one exact YC job page—not a YC search, company, account, or application URL.", "error");
    byId("yc-job-url").focus();
    return;
  }

  let savedJob = null;
  let reusedSavedJob = false;
  const requestIdentity = identitySnapshot();
  await withBusy(button, "Checking saved YC jobs…", async () => {
    try {
      savedJob = await findSavedYcJob(jobIdentity.url, requestIdentity);
      if (savedJob?.id) {
        reusedSavedJob = true;
        setBusyLabel(button, "Reasserting exact YC target…");
        const reboundPayload = await apiRequest(`/jobs/${encodeURIComponent(savedJob.id)}`, {
          method: "PATCH",
          body: { apply_url: jobIdentity.url },
        });
        savedJob = unwrapData(reboundPayload) || savedJob;
        form.reset();
        applyYcIntakeDefaults(true);
        await loadFormApplications(true, requestIdentity);
        renderJobs();
        renderYcDesk();
        setFormMessage("yc-job-status", "This exact YC job is already saved. Its strict target was revalidated and its existing preparation is reopening…", "success");
        return;
      }
      if (!title || !company || description.length < 20) {
        setFormMessage("yc-job-status", "For a new YC job, add the role, startup, and at least 20 characters of the real job description.", "error");
        return;
      }
      setBusyLabel(button, "Saving YC job…");
      const payload = await apiRequest("/jobs", {
        method: "POST",
        body: {
          source: "yc_exact",
          external_id: jobIdentity.externalId,
          apply_url: jobIdentity.url,
          title,
          company,
          location: location || null,
          description,
          contact_email: null,
          metadata: { application_provider: "yc", intake: "exact_user_saved" },
        },
      });
      savedJob = unwrapData(payload);
      if (!savedJob?.id) throw new AppError("The YC job was saved without a reviewable identity.", "yc_job_save_incomplete");
      form.reset();
      applyYcIntakeDefaults(true);
      await loadJobs(true, requestIdentity);
      setFormMessage(
        "yc-job-status",
        ycConnectionReady()
          ? "Exact YC job saved. Opening its application preparation now…"
          : "Exact YC job saved. Connect YC here, then choose Scan application on the saved job.",
        "success",
      );
    } catch (error) {
      setFormMessage("yc-job-status", errorMessage(error, "The exact YC job could not be saved."), "error");
    }
  });
  if (!savedJob?.id) return;
  const existingFormApplication = state.formApplications.find(
    (application) => application.job_id === savedJob.id && application.channel === "ats",
  );
  if (existingFormApplication?.id) {
    setFormMessage(
      "yc-job-status",
      reusedSavedJob
        ? "Existing YC job and prepared application found. Opening the same review desk—no duplicate was created."
        : "A prepared application already exists for this YC job. Opening its review desk.",
      "success",
    );
    await openFormApplicationReview(existingFormApplication.id);
    return;
  }
  if (ycConnectionReady() && capabilityForProvider("yc")?.can_scan === true) {
    await scanJobApplication(savedJob, "yc", button);
  } else {
    renderYcDesk();
    byId("yc-connect")?.focus();
  }
}

function openYcConnections() {
  switchView("connections");
  window.setTimeout(() => {
    const card = document.querySelector('[data-provider-card="yc"]');
    card?.scrollIntoView({ behavior: "smooth", block: "center" });
    card?.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
  }, 250);
}

function providerForJob(job) {
  const supplied = String(job?.metadata?.application_provider || job?.metadata?.provider || job?.metadata?.ats_provider || job?.metadata?.discovery?.provider || "").toLowerCase();
  const supported = new Set(["company_form", "google_forms", "greenhouse", "lever", "ashby", "yc", "wellfound", "cutshort", "instahyre"]);
  if (supported.has(supplied)) return supplied;
  const url = safeHttpUrl(job?.apply_url);
  if (!url) return null;
  const host = new URL(url).hostname.toLowerCase();
  if (host === "forms.gle" || (host === "docs.google.com" && new URL(url).pathname.startsWith("/forms/"))) return "google_forms";
  if (host === "greenhouse.io" || host.endsWith(".greenhouse.io")) return "greenhouse";
  if (host === "lever.co" || host.endsWith(".lever.co")) return "lever";
  if (host === "ashbyhq.com" || host.endsWith(".ashbyhq.com")) return "ashby";
  if (exactYcJobUrl(url)) return "yc";
  if (host === "wellfound.com" || host.endsWith(".wellfound.com")) return "wellfound";
  if (host === "cutshort.io" || host.endsWith(".cutshort.io")) return "cutshort";
  if (host === "instahyre.com" || host.endsWith(".instahyre.com")) return "instahyre";
  return null;
}

function capabilityForProvider(providerId) {
  const wanted = String(providerId || "").toLowerCase();
  return mergedProviders().find(
    (provider) => String(provider.id || provider.provider || "").toLowerCase() === wanted,
  ) || null;
}

function jobFitBlock(job) {
  const fit = job?.fit && typeof job.fit === "object" ? job.fit : {};
  const score = fit.evaluated && Number.isFinite(fit.score) ? Math.max(0, Math.min(100, fit.score)) : null;
  const heading = createElement("div", { className: "job-fit-heading" }, [
    createElement("strong", { text: score == null ? "Résumé alignment" : fit.label || "Résumé alignment" }),
    createElement("span", { text: score == null ? "Not scored" : `${score}%` }),
  ]);
  const children = [heading];
  if (score != null) {
    children.push(createElement("progress", {
      className: "job-fit-meter",
      attrs: { max: "100", value: String(score), "aria-label": `Résumé alignment ${score}%` },
    }));
  }
  children.push(createElement("small", { text: fit.basis || "Parse a résumé to compare its evidence with this description." }));
  const skills = Array.isArray(fit.matched_skills) ? fit.matched_skills.slice(0, 6) : [];
  if (skills.length) {
    children.push(createElement("div", { className: "job-skill-signals", attrs: { "aria-label": "Matching skills" } },
      skills.map((skill) => createElement("span", { className: "job-skill-signal", text: skill })),
    ));
  }
  return createElement("section", { className: "job-fit-block", attrs: { "aria-label": "Résumé alignment estimate" } }, children);
}

function renderJobs() {
  const container = byId("job-list");
  clearNode(container);
  const jobs = filteredJobs();
  if (!jobs.length) {
    container.append(emptyState(state.jobs.length ? "No jobs match this filter" : "No jobs yet", state.jobs.length ? "Try a different search or status." : "Use the form to add your first opportunity.", "◇"));
    return;
  }
  for (const job of jobs) {
    const card = createElement("article", { className: "job-card" });
    const heading = createElement("div", { className: "job-card-header" }, [
      createElement("div", {}, [createElement("h3", { text: job.title || "Untitled role" }), createElement("p", { text: job.company || "Unknown company" })]),
      makeStatus(job.status || "saved"),
    ]);
    const meta = createElement("div", { className: "meta-row" });
    if (job.location) meta.append(createElement("span", { text: `⌖ ${job.location}` }));
    if (job.contact_email) meta.append(createElement("span", { text: `@ ${job.contact_email}` }));
    meta.append(createElement("span", { text: `Added ${formatDate(job.created_at)}` }));
    const preview = createElement("p", { className: "job-description-preview", text: job.description || "No description" });
    const actions = createElement("div", { className: "card-actions" });
    const aiDraft = createElement("button", { className: "button button-primary button-small", text: "Draft with Groq", type: "button" });
    aiDraft.addEventListener("click", () => draftJob(job, aiDraft));
    const blankDraft = createElement("button", { className: "button button-ghost button-small", text: "Blank draft", type: "button" });
    blankDraft.addEventListener("click", () => createBlankApplication(job, blankDraft));
    const edit = createElement("button", { className: "link-button", text: "Edit", type: "button" });
    edit.addEventListener("click", () => editJob(job));
    actions.append(aiDraft, blankDraft);
    const applicationProvider = providerForJob(job);
    if (applicationProvider) {
      const capability = capabilityForProvider(applicationProvider);
      const canScan = capability?.can_scan === true;
      const existingFormApplication = state.formApplications.find(
        (application) => application.job_id === job.id && application.channel === "ats",
      ) || null;
      const scan = createElement("button", {
        className: `button ${existingFormApplication || canScan ? "button-accent" : "button-ghost"} button-small`,
        text: existingFormApplication ? "Review application" : canScan ? "Scan application" : "Scan not enabled",
        type: "button",
      });
      scan.disabled = !existingFormApplication && !canScan;
      scan.title = existingFormApplication
        ? `Open the captured ${humanize(applicationProvider)} application and its latest review revision`
        : canScan
          ? `Capture the current ${humanize(applicationProvider)} form before reviewing answers`
          : capability?.reason || "Hosted form scanning is not enabled for this provider.";
      if (existingFormApplication) {
        scan.addEventListener("click", () => openFormApplicationReview(existingFormApplication.id));
      } else if (canScan) {
        scan.addEventListener("click", () => scanJobApplication(job, applicationProvider, scan));
      }
      actions.append(scan);
    }
    actions.append(edit);
    if (safeHttpUrl(job.apply_url)) {
      const open = createElement("button", { className: "link-button", text: "Open job ↗", type: "button" });
      open.addEventListener("click", () => openExternal(job.apply_url));
      actions.append(open);
    }
    const archived = job.status === "archived" || Boolean(job.archived_at);
    const archive = createElement("button", { className: "link-button", text: archived ? "Restore" : "Archive", type: "button" });
    archive.addEventListener("click", () => setJobArchived(job, !archived, archive));
    actions.append(archive);
    card.append(heading, meta, jobFitBlock(job), preview, actions);
    container.append(card);
  }
}

async function setJobArchived(job, archive, button) {
  await withBusy(button, archive ? "Archiving…" : "Restoring…", async () => {
    try {
      await apiRequest(`/jobs/${encodeURIComponent(job.id)}`, { method: "PATCH", body: { status: archive ? "archived" : "saved" } });
      await loadJobs(true);
      toast(archive ? "Job archived." : "Job restored.", "success");
    } catch (error) {
      toast(errorMessage(error, "The job status could not be changed."), "error");
    }
  });
}

async function draftJob(job, button) {
  await withBusy(button, "Drafting…", async () => {
    try {
      const payload = await apiRequest(`/jobs/${encodeURIComponent(job.id)}/draft`, { method: "POST", groq: true });
      const application = unwrapData(payload);
      await Promise.all([loadJobs(true), loadApplications(true)]);
      if (application?.id) await openApplicationReview(application.id);
      toast("A grounded draft is ready for your review.", "success");
    } catch (error) {
      toast(errorMessage(error, "The draft could not be generated."), "error");
    }
  });
}

async function createBlankApplication(job, button) {
  await withBusy(button, "Creating…", async () => {
    try {
      const payload = await apiRequest("/applications", {
        method: "POST",
        body: {
          job_id: job.id,
          channel: "email",
          recipient: job.contact_email || null,
          subject: null,
          body: null,
          metadata: {},
        },
      });
      const application = unwrapData(payload);
      await loadApplications(true);
      if (application?.id) await openApplicationReview(application.id);
      toast("Blank draft created for review.", "success");
    } catch (error) {
      toast(errorMessage(error, "A blank application could not be created."), "error");
    }
  });
}

function outreachJobScore(job) {
  return job?.fit?.evaluated && Number.isFinite(job.fit.score) ? job.fit.score : -1;
}

function outreachJobs() {
  return state.jobs
    .filter((job) => job?.id && job.company && job.status !== "archived" && !job.archived_at)
    .sort((left, right) => {
      const scoreDifference = outreachJobScore(right) - outreachJobScore(left);
      return scoreDifference || new Date(right.created_at || 0) - new Date(left.created_at || 0);
    })
    .slice(0, 20);
}

function selectedOutreachJobs() {
  const byJobId = new Map(outreachJobs().map((job) => [job.id, job]));
  return [...state.outreachSelectedJobIds].map((id) => byJobId.get(id)).filter(Boolean).slice(0, 10);
}

function applicationForOutreachJob(jobId) {
  return state.applications
    .filter((application) => application.job_id === jobId && application.channel === "email")
    .sort((left, right) => new Date(right.updated_at || right.created_at || 0) - new Date(left.updated_at || left.created_at || 0))[0] || null;
}

function renderHunterState() {
  const pill = byId("hunter-key-status");
  const quota = byId("hunter-quota");
  const deleteButton = byId("hunter-delete");
  const summary = byId("hunter-saved-summary");
  if (!pill || !quota || !deleteButton || !summary) return;
  const credential = providerCredential("hunter");
  const saved = credential.configured === true;
  const ready = credentialConfigured("hunter");
  summary.hidden = !saved;
  setText("hunter-masked-key", saved ? credentialHint("hunter") : "");
  pill.className = `status-pill ${ready ? credential.verified_at ? "status-success" : "status-info" : saved ? "status-warning" : "status-neutral"}`;
  pill.textContent = ready ? credential.verified_at ? "Hunter ready" : "Saved · check pending" : saved ? "Replace required" : "Not connected";
  deleteButton.disabled = !saved;
  if (!saved) {
    quota.textContent = "Add a free Hunter key to search HR and recruiting contacts only when you request it.";
  } else if (!ready) {
    quota.textContent = "The saved Hunter credential did not validate. Paste a replacement above.";
  } else {
    quota.textContent = `${hunterCredentialQuotaCopy(credential)}.`;
  }
}

function toggleOutreachJob(jobId, checked) {
  if (checked && !state.outreachSelectedJobIds.has(jobId) && state.outreachSelectedJobIds.size >= 10) {
    toast("Choose at most 10 jobs for one cold-email batch.", "error");
    renderOutreachJobs();
    return;
  }
  if (checked) state.outreachSelectedJobIds.add(jobId);
  else state.outreachSelectedJobIds.delete(jobId);
  renderOutreach();
}

function renderOutreachPrerequisites() {
  const container = byId("outreach-prerequisites");
  if (!container) return;
  clearNode(container);
  const checks = [
    [Boolean(activeParsedResume()), "Résumé parsed", "Profile"],
    [credentialConfigured("groq"), "Groq key", "Profile or Connections"],
    [credentialConfigured("hunter"), "Hunter key", "This page or Connections"],
    [isGmailConnected(), "Gmail connected", "Connections"],
  ];
  for (const [ready, label, location] of checks) {
    container.append(createElement("span", {
      className: `outreach-prerequisite ${ready ? "is-ready" : ""}`,
      text: `${ready ? "✓" : "○"} ${label}${ready ? "" : ` · ${location}`}`,
    }));
  }
}

function renderOutreachJobs() {
  const container = byId("outreach-job-list");
  if (!container) return;
  clearNode(container);
  const candidates = outreachJobs();
  const validIds = new Set(candidates.map((job) => job.id));
  for (const id of [...state.outreachSelectedJobIds]) {
    if (!validIds.has(id)) state.outreachSelectedJobIds.delete(id);
  }
  setText("outreach-selection-count", `${state.outreachSelectedJobIds.size} / 10 selected`);
  if (!candidates.length) {
    container.append(emptyState("No relevant jobs to contact yet", "Run Find jobs first, or add a job with a company and description.", "1"));
    return;
  }
  for (const job of candidates) {
    const row = createElement("label", { className: `outreach-choice-item${state.outreachSelectedJobIds.has(job.id) ? " is-selected" : ""}` });
    const checkbox = createElement("input", { type: "checkbox", attrs: { "aria-label": `Select ${job.title || "role"} at ${job.company}` } });
    checkbox.checked = state.outreachSelectedJobIds.has(job.id);
    checkbox.addEventListener("change", () => toggleOutreachJob(job.id, checkbox.checked));
    const score = outreachJobScore(job);
    const application = applicationForOutreachJob(job.id);
    row.append(
      checkbox,
      createElement("span", { className: "outreach-job-copy" }, [
        createElement("strong", { text: job.title || "Untitled role" }),
        createElement("small", { text: `${job.company}${job.location ? ` · ${job.location}` : ""}` }),
      ]),
      createElement("span", { className: "outreach-job-state", text: `${score >= 0 ? `${score}% fit` : "Not scored"}${application ? ` · ${humanize(application.status)}` : ""}` }),
    );
    container.append(row);
  }
}

function selectedContactForJob(job) {
  const result = state.outreachContacts[job.id];
  if (result?.selected) return result.selected;
  if (job.contact_email) return job.contact_email;
  return null;
}

function renderOutreachContacts() {
  const container = byId("outreach-contact-results");
  if (!container) return;
  clearNode(container);
  const jobs = selectedOutreachJobs();
  if (!jobs.length) {
    container.append(emptyState("Select jobs first", "Choose the strongest résumé matches before spending any Hunter search credits.", "2"));
    return;
  }
  for (const job of jobs) {
    const result = state.outreachContacts[job.id] || {};
    const contacts = Array.isArray(result.contacts) ? result.contacts : [];
    const row = createElement("article", { className: "outreach-result-item" });
    const copy = createElement("div", {}, [
      createElement("strong", { text: job.company }),
      createElement("small", { text: result.error || (contacts.length ? `${contacts.length} recruiting contact${contacts.length === 1 ? "" : "s"} found${result.domain ? ` at ${result.domain}` : ""}.` : job.contact_email ? "Using the saved job contact." : "Search not run yet.") }),
    ]);
    if (contacts.length) {
      const select = createElement("select", { attrs: { "aria-label": `Recruiter contact for ${job.company}` } });
      select.append(createElement("option", { text: "Choose a contact", attrs: { value: "" } }));
      for (const contact of contacts) {
        const option = createElement("option", {
          text: `${contact.name || "Recruiting contact"} · ${contact.email}${contact.position ? ` · ${contact.position}` : ""} · ${contact.confidence ?? 0}% confidence`,
          attrs: { value: contact.email },
        });
        option.selected = result.selected === contact.email;
        select.append(option);
      }
      select.addEventListener("change", () => {
        state.outreachContacts[job.id] = { ...result, selected: select.value || null };
        renderOutreach();
      });
      row.append(copy, select);
    } else {
      row.append(copy, makeStatus(result.error ? "failed" : job.contact_email ? "ready" : "pending", result.error ? "Needs attention" : job.contact_email ? job.contact_email : "Awaiting search"));
    }
    container.append(row);
  }
}

async function findOutreachContacts(button) {
  const jobs = selectedOutreachJobs();
  if (!jobs.length) {
    setFormMessage("outreach-contact-status", "Select at least one relevant job first.", "error");
    return;
  }
  if (!credentialConfigured("hunter")) {
    setFormMessage("outreach-contact-status", "Add your Hunter key above or in Connections, then start the search again.", "error");
    byId("hunter-api-key")?.focus();
    return;
  }
  await withBusy(button, "Checking Hunter key…", async () => {
    if (state.hunterValidation?.valid !== true) {
      try {
        const validation = await apiRequest("/hunter/validate", { method: "POST", hunter: true });
        state.hunterValidation = validation && typeof validation === "object" ? validation : null;
        renderHunterState();
        renderOutreachPrerequisites();
        if (state.hunterValidation?.valid !== true) {
          setFormMessage(
            "outreach-contact-status",
            state.hunterValidation?.message || "Hunter rejected this key. Replace it above or in Connections.",
            "error",
          );
          return;
        }
      } catch (error) {
        state.hunterValidation = null;
        renderHunterState();
        setFormMessage("outreach-contact-status", errorMessage(error, "Hunter could not validate this key."), "error");
        return;
      }
    }

    let found = 0;
    let failed = 0;
    for (let index = 0; index < jobs.length; index += 1) {
      const job = jobs[index];
      setBusyLabel(button, `Searching ${index + 1} / ${jobs.length}…`);
      try {
        const payload = await apiRequest(`/jobs/${encodeURIComponent(job.id)}/contacts/hunter?limit=5`, { method: "POST", hunter: true });
        const data = unwrapData(payload) || {};
        const contacts = Array.isArray(data.contacts) ? data.contacts : [];
        state.outreachContacts[job.id] = {
          contacts,
          domain: data.domain || null,
          selected: contacts.some((contact) => contact.email === job.contact_email)
            ? job.contact_email
            : null,
          error: contacts.length ? null : "Hunter found no HR contacts for this company.",
        };
        found += contacts.length ? 1 : 0;
      } catch (error) {
        failed += 1;
        state.outreachContacts[job.id] = {
          contacts: [],
          selected: job.contact_email || null,
          error: errorMessage(error, "Hunter contact search failed."),
        };
        if (error?.code === "hunter_quota_exhausted") break;
      }
      renderOutreachContacts();
    }
    setFormMessage("outreach-contact-status", `${found} compan${found === 1 ? "y has" : "ies have"} a selected recruiting contact${failed ? `; ${failed} search${failed === 1 ? " needs" : "es need"} attention` : ""}.`, failed ? "error" : "success");
    renderOutreach();
  });
}

function renderOutreachDrafts() {
  const container = byId("outreach-draft-list");
  if (!container) return;
  clearNode(container);
  const jobs = selectedOutreachJobs();
  const rows = jobs.map((job) => ({ job, application: applicationForOutreachJob(job.id) }));
  if (!rows.some(({ application }) => application)) {
    container.append(emptyState("No drafts in this batch", "Choose contacts, then let Groq create one factual draft per selected job.", "3"));
  } else {
    for (const { job, application } of rows) {
      if (!application) continue;
      const row = createElement("article", { className: "outreach-result-item" });
      const review = createElement("button", { className: "button button-ghost button-small", text: application.status === "approved" ? "Review approved" : "Review draft", type: "button" });
      review.addEventListener("click", () => openApplicationReview(application.id));
      row.append(
        createElement("div", {}, [createElement("strong", { text: `${job.title} — ${job.company}` }), createElement("small", { text: application.recipient || "Recipient missing" })]),
        makeStatus(application.status),
        review,
      );
      container.append(row);
    }
  }
  const approved = rows.filter(({ application }) => application?.status === "approved").length;
  setFormMessage("outreach-draft-status", `${approved} of ${jobs.length} selected message${jobs.length === 1 ? " is" : "s are"} approved for sending.`);
}

async function createOutreachDrafts(button) {
  const jobs = selectedOutreachJobs();
  const ready = jobs.filter((job) => selectedContactForJob(job));
  if (!credentialConfigured("groq")) {
    setFormMessage("outreach-draft-status", "Connect a Groq key in Profile or Connections first.", "error");
    return;
  }
  if (!ready.length || ready.length !== jobs.length) {
    setFormMessage("outreach-draft-status", "Choose one contact for every selected job before drafting.", "error");
    return;
  }
  await withBusy(button, `Drafting 0 / ${ready.length}…`, async () => {
    let completed = 0;
    const failures = [];
    for (let index = 0; index < ready.length; index += 1) {
      const job = ready[index];
      setBusyLabel(button, `Drafting ${index + 1} / ${ready.length}…`);
      try {
        await apiRequest(`/jobs/${encodeURIComponent(job.id)}`, {
          method: "PATCH",
          body: { contact_email: selectedContactForJob(job) },
        });
        await apiRequest(`/jobs/${encodeURIComponent(job.id)}/draft`, { method: "POST", groq: true });
        completed += 1;
      } catch (error) {
        failures.push(`${job.company}: ${errorMessage(error, "draft failed")}`);
        if (error?.code === "groq_request_rate_limited" || error?.code === "groq_rate_limited") break;
      }
    }
    await Promise.all([loadJobs(true), loadApplications(true)]);
    renderOutreach();
    setFormMessage(
      "outreach-draft-status",
      `${completed} draft${completed === 1 ? " is" : "s are"} ready for individual review${failures.length ? `. ${failures.slice(0, 2).join(" · ")}` : "."}`,
      failures.length ? "error" : "success",
    );
  });
}

async function sendApprovedOutreach(button) {
  const approved = selectedOutreachJobs()
    .map((job) => ({ job, application: applicationForOutreachJob(job.id) }))
    .filter(({ application }) => application?.status === "approved" && application.recipient)
    .slice(0, 10);
  if (!approved.length) {
    setFormMessage("outreach-send-status", "Review and approve at least one selected draft first.", "error");
    return;
  }
  if (!isGmailConnected()) {
    setFormMessage("outreach-send-status", "Connect Gmail before sending approved messages.", "error");
    switchView("connections");
    return;
  }
  const companies = approved.map(({ job }) => job.company).join(", ");
  if (!await confirmAction({
    eyebrow: "Final Gmail handoff",
    title: `Send ${approved.length} approved cold email${approved.length === 1 ? "" : "s"}?`,
    message: `Gmail will send the individually reviewed messages with your active résumé attached.\nRecipients: ${companies}.`,
    confirmLabel: `Send ${approved.length} email${approved.length === 1 ? "" : "s"}`,
    cancelLabel: "Review again",
    tone: "caution",
    ticketLabel: "Approved batch",
    symbol: String(approved.length),
  })) return;
  await withBusy(button, `Sending 0 / ${approved.length}…`, async () => {
    let sent = 0;
    const failures = [];
    for (let index = 0; index < approved.length; index += 1) {
      const { job, application } = approved[index];
      setBusyLabel(button, `Sending ${index + 1} / ${approved.length}…`);
      try {
        await apiRequest(`/applications/${encodeURIComponent(application.id)}/send`, {
          method: "POST",
          body: {
            idempotency_key: `outreach-send-${application.id}-${crypto.randomUUID()}`,
            attach_resume: true,
          },
        });
        sent += 1;
      } catch (error) {
        failures.push(`${job.company}: ${errorMessage(error, "send failed")}`);
        if (["daily_send_cap_reached", "provider_daily_send_cap_reached", "gmail_reauthorization_required"].includes(error?.code)) break;
      }
    }
    await loadApplications(true);
    setFormMessage(
      "outreach-send-status",
      `${sent} approved message${sent === 1 ? " was" : "s were"} accepted by Gmail${failures.length ? `. ${failures.slice(0, 2).join(" · ")}` : "."}`,
      failures.length ? "error" : "success",
    );
    renderOutreach();
  });
}

function renderOutreach() {
  if (!byId("outreach-job-list")) return;
  renderOutreachPrerequisites();
  renderHunterState();
  renderOutreachJobs();
  renderOutreachContacts();
  renderOutreachDrafts();
  const selected = selectedOutreachJobs();
  const withContacts = selected.filter((job) => selectedContactForJob(job));
  const approved = selected.filter((job) => applicationForOutreachJob(job.id)?.status === "approved");
  const findButton = byId("outreach-find-contacts");
  const draftButton = byId("outreach-create-drafts");
  const sendButton = byId("outreach-send-approved");
  const hunterKey = credentialConfigured("hunter");
  setText(
    "outreach-credit-estimate",
    selected.length
      ? `${selected.length} selected compan${selected.length === 1 ? "y" : "ies"} · up to ${selected.length} Hunter search credit${selected.length === 1 ? "" : "s"}`
      : "Select jobs to see the Hunter credit estimate.",
  );
  if (findButton) {
    findButton.disabled = !selected.length || !hunterKey;
    findButton.title = !selected.length
      ? "Select at least one company first."
      : !hunterKey
        ? "Add your Hunter API key above or in Connections."
        : state.hunterValidation?.valid === true
          ? `Search ${selected.length} selected compan${selected.length === 1 ? "y" : "ies"}.`
          : "Your saved Hunter key will be validated automatically before this search.";
  }
  if (selected.length && hunterKey && state.hunterValidation == null) {
    setFormMessage("outreach-contact-status", "Ready. Hunter will validate the saved key automatically, then search every selected company.");
  } else if (selected.length && state.hunterValidation?.valid === false) {
    setFormMessage("outreach-contact-status", state.hunterValidation.message || "Hunter rejected this key. Replace or validate it above.", "error");
  } else if (selected.length && !hunterKey) {
    setFormMessage("outreach-contact-status", "Add your Hunter key above or in Connections to unlock contact search.", "error");
  }
  if (draftButton) draftButton.disabled = !selected.length || withContacts.length !== selected.length || !credentialConfigured("groq");
  if (sendButton) sendButton.disabled = !approved.length || !isGmailConnected();
}

async function scanJobApplication(job, provider, button) {
  return withBusy(button, "Preparing form…", async () => {
    try {
      const identity = identitySnapshot();
      const existingApplication = state.formApplications.find(
        (item) => item.job_id === job.id && item.channel === "ats",
      );
      const baselineRevisionId = existingApplication?.id
        ? latestFormRevision(existingApplication.id)?.id || null
        : null;
      const payload = await apiRequest(`/jobs/${encodeURIComponent(job.id)}/application/scan`, {
        method: "POST",
        body: { idempotency_key: discoveryRunKey("scan"), form_revision_id: null },
        identity,
      });
      const data = unwrapData(payload) || {};
      const applicationId = data.application?.id || data.application_id || null;
      const automationJobId = data.automation_job?.id || null;
      if (data.automation_job?.id) rememberAutomationJob(data.automation_job);
      if (data.application?.id && data.application.channel === "ats") {
        state.formApplications = [
          data.application,
          ...state.formApplications.filter((item) => item.id !== data.application.id),
        ];
      }
      await Promise.all([
        loadFormApplications(true, identity),
        loadAutomationJobs(true, identity),
        loadGoogleForms(true, identity),
      ]);
      const application = applicationId
        ? state.formApplications.find((item) => item.id === applicationId)
        : state.formApplications.find((item) => item.job_id === job.id && item.channel === "ats");
      if (!application?.id) throw new AppError("The form scan started, but its review desk could not be opened.", "form_application_missing");
      await openFormApplicationReview(application.id, {
        monitorJobId: automationJobId,
        baselineRevisionId,
        identity,
      });
      toast(`${humanize(provider)} scan started. Form Pilot will show the questions and grounded suggestions automatically.`, "success");
      return true;
    } catch (error) {
      if (error?.code === "provider_connection_required" || error?.code === "browser_context_missing") {
        toast(`Connect ${humanize(provider)} in the provider center before scanning this form.`, "error");
        switchView("connections");
      } else {
        toast(errorMessage(error, "The application scan could not be queued."), "error");
      }
      return false;
    }
  });
}

async function loadApplications(quiet = false, identity = identitySnapshot()) {
  const container = byId("application-list");
  if (!quiet) showLoading(container);
  const payload = await apiRequest("/applications?channel=email&limit=50", { identity });
  state.applications = unwrapItems(payload, ["applications"]).filter((application) => application.channel === "email");
  if (state.selectedApplicationId && !state.applications.some((item) => item.id === state.selectedApplicationId)) {
    state.selectedApplicationId = null;
    state.applicationEditorDirty = false;
  }
  if (!state.selectedApplicationId && state.applications.length) {
    state.selectedApplicationId = state.applications[0].id;
  }
  renderApplications();
  renderOutreach();
  renderOverview();
  return state.applications;
}

async function loadFormApplications(quiet = false, identity = identitySnapshot()) {
  const payload = await apiRequest("/applications?channel=ats&limit=50", { identity });
  state.formApplications = unwrapItems(payload, ["applications"]).filter((application) => application.channel === "ats");
  if (
    state.selectedFormApplicationId
    && !state.formApplications.some((item) => item.id === state.selectedFormApplicationId)
  ) {
    state.selectedFormApplicationId = null;
    state.selectedFormRevisionId = null;
  }
  renderGoogleFormQueue();
  if (state.selectedFormApplicationId) {
    const selected = state.formApplications.find((item) => item.id === state.selectedFormApplicationId);
    if (selected) populateFormApplicationReview(selected);
  } else if (!quiet) {
    clearFormApplicationReview();
  }
  renderOverview();
  renderYcDesk();
  return state.formApplications;
}

function jobForApplication(application) {
  return state.jobs.find((job) => job.id === application.job_id) || null;
}

function filteredApplications() {
  const status = byId("application-status-filter").value;
  return state.applications.filter(
    (application) => application.channel === "email" && (!status || application.status === status),
  );
}

function renderApplications() {
  const container = byId("application-list");
  clearNode(container);
  const applications = filteredApplications();
  const draftCount = state.applications.filter((item) => ["draft_pending", "drafted", "approved"].includes(item.status)).length;
  const badge = byId("draft-count-badge");
  badge.hidden = draftCount === 0;
  badge.textContent = String(draftCount);
  all("[data-draft-tab-count]").forEach((tabBadge) => {
    tabBadge.hidden = draftCount === 0;
    tabBadge.textContent = String(draftCount);
  });
  if (!applications.length) {
    container.append(emptyState(state.applications.length ? "No applications match this filter" : "No applications yet", state.applications.length ? "Choose another status." : "Create a draft from a saved job to begin.", "✎"));
    if (!state.applicationEditorDirty) clearApplicationEditor(false);
    return;
  }
  for (const application of applications) {
    const job = jobForApplication(application);
    const card = createElement("article", { className: `application-card${application.id === state.selectedApplicationId ? " is-selected" : ""}` });
    const heading = createElement("div", { className: "application-card-header" }, [
      createElement("div", {}, [
        createElement("h3", { text: job?.title || application.subject || "Application draft" }),
        createElement("p", { text: job?.company || application.recipient || "Manual application" }),
      ]),
      makeStatus(application.status || "draft_pending"),
    ]);
    const body = createElement("p", { className: "application-body-preview", text: application.body || "No message written yet." });
    const meta = createElement("div", { className: "meta-row" }, [
      createElement("span", { text: humanize(application.channel || "email") }),
      createElement("span", { text: `Updated ${formatDate(application.updated_at || application.created_at)}` }),
    ]);
    const actions = createElement("div", { className: "card-actions" });
    const review = createElement("button", { className: "button button-ghost button-small", text: "Review", type: "button" });
    review.addEventListener("click", () => selectApplication(application.id));
    actions.append(review);
    if (!["sent", "applied", "archived"].includes(application.status)) {
      const handoff = createElement("button", { className: "link-button", text: "Queue manual handoff", type: "button" });
      handoff.addEventListener("click", () => queueManualHandoff(application, handoff));
      actions.append(handoff);
    }
    if (application.status === "queued") {
      const reconcile = createElement("button", { className: "link-button", text: "Resolve unconfirmed send", type: "button" });
      reconcile.title = "Move a stale, unconfirmed Gmail send to needs attention so it can be reviewed safely";
      reconcile.addEventListener("click", () => reconcileApplication(application, reconcile));
      actions.append(reconcile);
    }
    card.addEventListener("dblclick", () => selectApplication(application.id));
    card.append(heading, body, meta, actions);
    container.append(card);
  }
  if (state.selectedApplicationId) {
    const selected = state.applications.find((item) => item.id === state.selectedApplicationId);
    if (selected && !state.applicationEditorDirty) populateApplicationEditor(selected);
  }
}

async function selectApplication(id) {
  if (state.applicationEditorDirty && state.selectedApplicationId === id) {
    if (state.currentView === "applications") byId("application-editor").scrollIntoView({ behavior: "smooth", block: "start" });
    return true;
  }
  if (state.applicationEditorDirty && state.selectedApplicationId && state.selectedApplicationId !== id) {
    if (!await confirmAction({
      eyebrow: "Unsaved draft",
      title: "Discard your unsaved changes?",
      message: "The edits in the current application have not been saved. Switching drafts will remove them.",
      confirmLabel: "Discard changes",
      cancelLabel: "Keep editing",
      tone: "caution",
      ticketLabel: "Draft change",
      symbol: "↺",
    })) return false;
  }
  state.applicationEditorDirty = false;
  state.selectedApplicationId = id;
  const application = state.applications.find((item) => item.id === id);
  renderApplications();
  if (application) {
    populateApplicationEditor(application);
    if (state.currentView === "applications") byId("application-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  return true;
}

async function openApplicationReview(id) {
  if (!id) return false;
  const application = state.applications.find((item) => item.id === id && item.channel === "email");
  if (!application) return false;
  const selected = await selectApplication(id);
  if (!selected) return false;
  switchView("applications");
  requestAnimationFrame(() => byId("application-editor")?.focus?.());
  return true;
}

function formApplicationById(id) {
  return state.formApplications.find((item) => item.id === id && item.channel === "ats") || null;
}

function formScanJobs(applicationId) {
  return state.automationJobs
    .filter((job) => job.application_id === applicationId && job.kind === "application_scan")
    .sort((left, right) => {
      const rightAt = new Date(right.updated_at || right.created_at || 0).getTime() || 0;
      const leftAt = new Date(left.updated_at || left.created_at || 0).getTime() || 0;
      return rightAt - leftAt;
    });
}

function latestFormScanJob(applicationId) {
  return formScanJobs(applicationId)[0] || null;
}

function activeFormScanJob(applicationId) {
  return formScanJobs(applicationId).find(
    (job) => ["queued", "running"].includes(String(job.status || "").toLowerCase()),
  ) || null;
}

function formPreparationIsActive(applicationId) {
  return Boolean(
    applicationId
      && (
        state.formRecoveryScanApplicationIds.has(applicationId)
        || activeFormScanJob(applicationId)
      ),
  );
}

async function openFormApplicationReview(
  id,
  {
    monitorJobId = null,
    baselineRevisionId = undefined,
    identity = identitySnapshot(),
    scroll = true,
  } = {},
) {
  if (!id) return false;
  const application = formApplicationById(id);
  if (!application) return false;
  state.selectedFormApplicationId = id;
  populateFormApplicationReview(application);
  switchView("form_pilot");
  const trackedJobId = monitorJobId || activeFormScanJob(id)?.id || null;
  const trackedBaselineRevisionId = baselineRevisionId === undefined
    ? latestFormRevision(id)?.id || null
    : baselineRevisionId;
  try {
    await loadApplicationFormRevisions(id, false, !trackedJobId, identity);
  } catch (error) {
    if (isIdentityChanged(error)) return false;
    setFormMessage("form-revision-message", errorMessage(error, "The captured form revision could not be loaded."), "error");
  }
  if (trackedJobId) {
    monitorFormScan(trackedJobId, id, identity, trackedBaselineRevisionId).catch((error) => {
      if (!isIdentityChanged(error)) {
        setFormMessage("form-revision-message", errorMessage(error, "The live form scan status could not be refreshed."), "error");
      }
    });
  }
  if (scroll) requestAnimationFrame(() => byId("form-pilot-review")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  return true;
}

function populateFormApplicationReview(application) {
  const job = jobForApplication(application);
  byId("form-application-id").value = application.id || "";
  setText(
    "form-application-job-context",
    job
      ? `${job.title || "Role"} at ${job.company || "company"}`
      : "Captured application form without a linked saved job",
  );
  const exactTarget = exactYcJobUrl(job?.apply_url);
  const targetUrl = byId("form-application-target-url");
  const targetLink = byId("form-application-target-link");
  if (exactTarget) {
    targetUrl.textContent = exactTarget;
    targetUrl.hidden = false;
    targetLink.href = exactTarget;
    targetLink.hidden = false;
  } else {
    targetUrl.textContent = "";
    targetUrl.hidden = true;
    targetLink.href = "#";
    targetLink.hidden = true;
  }
  const pill = byId("form-application-status-pill");
  pill.className = `status-pill ${statusClass(application.status)}`;
  pill.textContent = humanize(application.status || "draft_pending");
  renderYcDesk();
}

function clearFormApplicationReview(clearSelection = true) {
  if (clearSelection) state.selectedFormApplicationId = null;
  state.selectedFormRevisionId = null;
  hideFormWorkflowProgress();
  byId("form-application-id").value = "";
  setText("form-application-job-context", "Choose Prepare form or Review prepared form above to begin.");
  const targetUrl = byId("form-application-target-url");
  const targetLink = byId("form-application-target-link");
  targetUrl.textContent = "";
  targetUrl.hidden = true;
  targetLink.href = "#";
  targetLink.hidden = true;
  const pill = byId("form-application-status-pill");
  pill.className = "status-pill status-neutral";
  pill.textContent = "Choose a prepared form";
  renderFormRevision(null);
  setFormMessage("form-revision-message");
  renderYcDesk();
}

function populateApplicationEditor(application) {
  const job = jobForApplication(application);
  state.applicationEditorDirty = false;
  byId("application-id").value = application.id || "";
  byId("application-recipient").value = application.recipient || "";
  byId("application-subject").value = application.subject || "";
  byId("application-body").value = application.body || "";
  byId("application-attach-resume").checked = true;
  byId("application-fields").disabled = false;
  byId("application-fields").hidden = false;
  setText("application-job-context", job ? `${job.title || "Role"} at ${job.company || "company"}` : "Application without a linked job");
  const pill = byId("application-status-pill");
  pill.className = `status-pill ${statusClass(application.status)}`;
  pill.textContent = humanize(application.status || "draft_pending");
  updateApplicationCharacterCount();
  updateApplicationActionState(application);
  setFormMessage("application-editor-message");
}

function updateApplicationActionState(application = null) {
  const selected = application || state.applications.find((item) => item.id === byId("application-id").value);
  if (!selected) return;
  const terminal = ["queued", "sent", "applied", "archived"].includes(selected.status);
  for (const id of ["application-recipient", "application-subject", "application-body"]) {
    byId(id).readOnly = terminal;
  }
  byId("save-application").disabled = terminal;
  byId("application-attach-resume").disabled = terminal;
  byId("approve-application").disabled = terminal || (selected.status === "approved" && !state.applicationEditorDirty);
  byId("send-application").disabled = terminal || state.applicationEditorDirty || selected.status !== "approved" || !isGmailConnected();
  byId("send-application").title = state.applicationEditorDirty
    ? "Save and re-approve these edits before sending"
    : !isGmailConnected()
      ? "Connect Gmail before sending"
      : selected.status !== "approved"
        ? "Approve this draft before sending"
        : "Send the approved message";
}

function markApplicationDirty() {
  if (!byId("application-id").value) return;
  state.applicationEditorDirty = true;
  const application = state.applications.find((item) => item.id === byId("application-id").value);
  const pill = byId("application-status-pill");
  pill.className = "status-pill status-warning";
  pill.textContent = "Unsaved edits";
  updateApplicationActionState(application);
  setFormMessage("application-editor-message", "Save these edits, then approve the current version before sending.");
}

async function clearApplicationEditor(clearSelection = true) {
  if (clearSelection && state.applicationEditorDirty && !await confirmAction({
    eyebrow: "Unsaved draft",
    title: "Close without saving?",
    message: "The current application edits will be discarded.",
    confirmLabel: "Discard changes",
    cancelLabel: "Keep editing",
    tone: "caution",
    ticketLabel: "Draft change",
    symbol: "↺",
  })) return;
  if (clearSelection) state.selectedApplicationId = null;
  state.applicationEditorDirty = false;
  byId("application-editor").reset();
  byId("application-id").value = "";
  byId("application-fields").disabled = true;
  byId("application-fields").hidden = false;
  setText("application-job-context", "Select a draft from the list to review it here.");
  const pill = byId("application-status-pill");
  pill.className = "status-pill status-neutral";
  pill.textContent = "Select an application";
  updateApplicationCharacterCount();
  setFormMessage("application-editor-message");
  if (clearSelection) renderApplications();
}

function revisionQuestions(revision) {
  const schema = revision?.question_schema || revision?.form_schema || {};
  if (Array.isArray(schema)) return schema;
  for (const key of ["questions", "fields", "items"]) {
    if (Array.isArray(schema?.[key])) return schema[key];
  }
  return [];
}

function latestFormRevision(applicationId) {
  const revisions = state.formRevisions[applicationId] || [];
  const ordered = [...revisions].sort((a, b) => Number(b.revision || 0) - Number(a.revision || 0));
  return ordered.find((revision) => formRevisionSubmissionIsVerified(revision)) || ordered[0] || null;
}

function waitForFormWorkflowPoll() {
  return new Promise((resolve) => window.setTimeout(resolve, FORM_WORKFLOW_POLL_INTERVAL_MS));
}

function formWorkflowJobIsTerminal(job) {
  return DISCOVERY_TERMINAL_STATUSES.has(String(job?.status || "").toLowerCase());
}

function showFormWorkflowProgress({
  title = "Preparing your form",
  detail = "Form Pilot is waiting for the isolated worker.",
  value = "Starting",
  percent = 8,
  tone = "active",
} = {}) {
  const panel = byId("form-workflow-progress");
  if (!panel) return;
  panel.hidden = false;
  panel.classList.toggle("is-complete", tone === "complete");
  panel.classList.toggle("is-error", tone === "error");
  panel.classList.toggle("is-attention", tone === "attention");
  setAriaBusy(panel, tone === "active");
  setText("form-workflow-progress-title", title);
  setText("form-workflow-progress-detail", detail);
  setText("form-workflow-progress-value", value);
  const bar = byId("form-workflow-progress-bar");
  if (bar) bar.style.width = `${Math.max(4, Math.min(100, Number(percent) || 0))}%`;
}

function hideFormWorkflowProgress() {
  const panel = byId("form-workflow-progress");
  if (!panel) return;
  panel.hidden = true;
  panel.classList.remove("is-complete", "is-error", "is-attention");
  setAriaBusy(panel, false);
}

function formJobDetail(job, fallback = "The isolated worker is preparing the form.") {
  const result = job?.result && typeof job.result === "object" ? job.result : {};
  const detail = result.message || progressSummary(job) || job?.error_message || fallback;
  return String(detail).slice(0, 500);
}

function setFormScanRetry(applicationId, visible) {
  const button = byId("retry-form-scan");
  if (!button) return;
  const application = formApplicationById(applicationId);
  const job = application ? jobForApplication(application) : null;
  button.hidden = !visible || !job?.id;
  button.disabled = false;
  button.dataset.jobId = button.hidden ? "" : job.id;
  button.title = button.hidden ? "" : "Create a new preparation run for this form";
}

function renderFormScanPlaceholder(applicationId) {
  const container = byId("form-revision-answers");
  const status = byId("form-revision-status");
  const applicationStatus = byId("form-application-status-pill");
  const submit = byId("submit-form-revision");
  const job = latestFormScanJob(applicationId);
  const jobStatus = String(job?.status || "").toLowerCase();
  const active = ["queued", "running"].includes(jobStatus);
  submit.disabled = true;

  if (!job) {
    hideFormWorkflowProgress();
    status.className = "status-pill status-neutral";
    status.textContent = "Not prepared";
    applicationStatus.className = "status-pill status-neutral";
    applicationStatus.textContent = "Not prepared";
    setText("form-revision-context", "This form has no captured questions yet. Start preparation from the form inbox above.");
    container.append(emptyState("Prepare this form first", "Choose Prepare form in the inbox to capture its current questions.", "⌕"));
    setFormScanRetry(applicationId, Boolean(applicationId));
    setFormMessage("form-revision-message");
    return;
  }

  if (active) {
    const running = jobStatus === "running";
    const detail = formJobDetail(
      job,
      running
        ? "The worker is recording the form's current questions."
        : "The preparation job is safely queued and waiting for a worker.",
    );
    status.className = "status-pill status-info";
    status.textContent = running ? "Capturing form" : "Waiting for worker";
    applicationStatus.className = "status-pill status-info";
    applicationStatus.textContent = running ? "Preparing form" : "Preparation queued";
    setText("form-revision-context", detail);
    container.append(emptyState(
      running ? "Capturing the form" : "Preparation is queued",
      running
        ? "The visible questions will appear here when capture finishes."
        : "You can keep using AutoApply. Activity shows whether the worker has claimed this job.",
      "⌕",
    ));
    showFormWorkflowProgress({
      title: running ? "Capturing the visible form" : "Waiting for the form worker",
      detail,
      value: humanize(jobStatus),
      percent: running ? 58 : 18,
    });
    setFormScanRetry(applicationId, false);
    setFormMessage("form-revision-message", running ? "The scan is running; nothing has been submitted." : "The scan is queued; nothing has been submitted.");
    return;
  }

  const succeededWithoutRevision = jobStatus === "succeeded";
  const detail = `${formJobDetail(
    job,
    succeededWithoutRevision
      ? "The worker finished without returning any reviewable questions."
      : "The worker could not capture a reviewable form revision.",
  )} Nothing was submitted.`;
  status.className = `status-pill ${succeededWithoutRevision ? "status-warning" : statusClass(jobStatus)}`;
  status.textContent = succeededWithoutRevision ? "No questions captured" : humanize(jobStatus || "needs_attention");
  applicationStatus.className = "status-pill status-warning";
  applicationStatus.textContent = "Preparation needs attention";
  setText("form-revision-context", detail);
  container.append(emptyState(
    succeededWithoutRevision ? "No questions were captured" : "Form preparation stopped",
    "Review the worker detail above, then retry preparation. The previous run cannot submit this form.",
    "!",
  ));
  showFormWorkflowProgress({
    title: "The form needs attention",
    detail,
    value: humanize(jobStatus || "stopped"),
    percent: 100,
    tone: "error",
  });
  setFormScanRetry(applicationId, true);
  setFormMessage("form-revision-message", detail, "error");
}

function rememberAutomationJob(job) {
  if (!job?.id) return;
  const index = state.automationJobs.findIndex((item) => item.id === job.id);
  if (index >= 0) state.automationJobs[index] = job;
  else state.automationJobs.unshift(job);
  if (job.kind === "application_submit") {
    const revisionId = job.form_revision_id || job.payload?.form_revision_id || job.result?.form_revision_id;
    if (revisionId) state.formSubmissionJobs.set(revisionId, job);
  }
  renderYcDesk();
}

async function monitorFormWorkflowJob(jobId, identity = identitySnapshot(), onUpdate = null) {
  if (!jobId) throw new AppError("The form worker did not return a trackable job.", "form_job_missing");
  if (state.formWorkflowMonitors.has(jobId)) return state.formWorkflowMonitors.get(jobId);
  const startedAt = Date.now();
  const monitor = (async () => {
    let consecutiveErrors = 0;
    let latest = null;
    let firstPoll = true;
    while (firstPoll || Date.now() - startedAt < FORM_WORKFLOW_MONITOR_TIMEOUT_MS) {
      firstPoll = false;
      assertCurrentIdentity(identity);
      try {
        const payload = await apiRequest(`/automation-jobs/${encodeURIComponent(jobId)}`, { identity });
        latest = unwrapData(payload) || {};
        consecutiveErrors = 0;
        rememberAutomationJob(latest);
        if (typeof onUpdate === "function") onUpdate(latest);
        if (formWorkflowJobIsTerminal(latest)) return { job: latest, timedOut: false };
      } catch (error) {
        if (isIdentityChanged(error)) throw error;
        consecutiveErrors += 1;
        if (consecutiveErrors >= 3) throw error;
      }
      await waitForFormWorkflowPoll();
    }
    return { job: latest, timedOut: true };
  })();
  state.formWorkflowMonitors.set(jobId, monitor);
  try {
    return await monitor;
  } finally {
    if (state.formWorkflowMonitors.get(jobId) === monitor) state.formWorkflowMonitors.delete(jobId);
  }
}

async function monitorFormScan(
  jobId,
  applicationId,
  identity = identitySnapshot(),
  baselineRevisionId = latestFormRevision(applicationId)?.id || null,
) {
  if (state.selectedFormApplicationId === applicationId) {
    showFormWorkflowProgress({
      title: "Capturing the visible form",
      detail: "The worker is opening this exact provider application and recording its current visible questions.",
      value: "Queued",
      percent: 12,
    });
  }
  const outcome = await monitorFormWorkflowJob(jobId, identity, (job) => {
    if (state.selectedFormApplicationId !== applicationId) return;
    const running = job.status === "running";
    if (!latestFormRevision(applicationId)) {
      renderFormRevision(null);
      return;
    }
    showFormWorkflowProgress({
      title: running ? "Capturing the visible form" : "Waiting for the form worker",
      detail: formJobDetail(job),
      value: humanize(job.status || "queued"),
      percent: running ? 58 : 18,
    });
  });
  assertCurrentIdentity(identity);
  if (outcome.timedOut) {
    if (state.selectedFormApplicationId === applicationId) {
      showFormWorkflowProgress({
        title: "Preparation is continuing in Activity",
        detail: "This is taking longer than five minutes. The durable job is still safe; Form Pilot will not submit or automatically retry anything.",
        value: "Still running",
        percent: 72,
        tone: "error",
      });
      setFormMessage("form-revision-message", "Preparation is still running. Check Activity for the latest worker status; no form was submitted.", "error");
      refreshFormSubmitPreflight();
    }
    return null;
  }
  await Promise.all([
    loadFormApplications(true, identity),
    loadGoogleForms(true, identity),
    loadAutomationJobs(true, identity),
  ]);
  await loadApplicationFormRevisions(applicationId, true, true, identity);
  const revision = latestFormRevision(applicationId);
  const scanJob = outcome.job || {};
  const scanStatus = String(scanJob.status || "").toLowerCase();
  const scanRevisionId = scanJob.progress?.scan_revision_id
    || scanJob.result?.scan_revision_id
    || scanJob.result?.form_revision_id
    || null;
  const capturedThisRun = Boolean(
    scanStatus === "succeeded"
      && revision?.id
      && (scanRevisionId ? revision.id === scanRevisionId : revision.id !== baselineRevisionId),
  );
  if (capturedThisRun) {
    if (state.selectedFormApplicationId === applicationId) {
      showFormWorkflowProgress({
        title: "Questions captured",
        detail: credentialConfigured("groq")
          ? "Form Pilot is now preparing résumé-grounded suggestions for your review."
          : "Review the captured fields below and complete any missing answers manually.",
        value: "Ready",
        percent: 100,
        tone: "complete",
      });
      refreshFormSubmitPreflight();
    }
    window.setTimeout(() => {
      if (state.selectedFormApplicationId === applicationId && !state.formWorkflowMonitors.size) hideFormWorkflowProgress();
    }, 1_800);
    return revision;
  }
  if (state.selectedFormApplicationId === applicationId) {
    showFormWorkflowProgress({
      title: "The form needs attention",
      detail: `${formJobDetail(scanJob, "The worker could not capture a new reviewable form revision.")} Nothing was submitted.`,
      value: humanize(scanJob.status || "stopped"),
      percent: 100,
      tone: "error",
    });
    setFormScanRetry(applicationId, true);
    setFormMessage(
      "form-revision-message",
      `${formJobDetail(scanJob, "The form could not be prepared.")} No new revision was created and nothing was submitted.`,
      "error",
    );
    refreshFormSubmitPreflight();
  }
  return null;
}

async function loadApplicationFormRevisions(
  applicationId,
  quiet = false,
  autoSuggest = true,
  identity = identitySnapshot(),
) {
  const answerList = byId("form-revision-answers");
  if (!quiet) showLoading(answerList, 2);
  const payload = await apiRequest(`/applications/${encodeURIComponent(applicationId)}/form-revisions`, { identity });
  state.formRevisions[applicationId] = unwrapItems(payload, ["revisions"]);
  const latest = latestFormRevision(applicationId);
  state.selectedFormRevisionId = latest?.id || null;
  renderFormRevision(latest);
  if (latest && autoSuggest) await maybeSuggestFormAnswers(applicationId, latest);
  renderYcDesk();
  return state.formRevisions[applicationId];
}

function formQuestionControl(question, value, answerKey) {
  const rawType = String(question.type || question.input_type || "text").toLowerCase();
  const attrs = { "data-answer-key": answerKey };
  let control;
  const options = Array.isArray(question.options) ? question.options : Array.isArray(question.choices) ? question.choices : [];
  const multiple = ["multiselect", "checkbox"].includes(rawType) && options.length > 0;
  if (["select", "combobox", "listbox", "radio", "dropdown", "multiselect", "checkbox"].includes(rawType) && options.length) {
    control = createElement("select", { attrs: { ...attrs, ...(multiple ? { multiple: "" } : {}) } });
    if (!multiple) control.append(createElement("option", { text: "Choose an answer", attrs: { value: "" } }));
    const selectedValues = new Set(Array.isArray(value) ? value.map(String) : [String(value ?? "")]);
    for (const option of options) {
      const optionValue = typeof option === "object" ? String(option.value ?? option.id ?? option.label ?? "") : String(option);
      const label = typeof option === "object" ? String(option.label ?? option.text ?? optionValue) : optionValue;
      const node = createElement("option", { text: label, attrs: { value: optionValue } });
      node.selected = selectedValues.has(optionValue);
      control.append(node);
    }
  } else if (["textarea", "long_text", "paragraph"].includes(rawType)) {
    control = createElement("textarea", { attrs: { ...attrs, rows: "4", maxlength: "5000" } });
    control.value = value == null ? "" : String(value);
  } else if (["checkbox", "boolean"].includes(rawType)) {
    control = createElement("input", { type: "checkbox", attrs });
    control.checked = value === true || value === "true";
  } else if (["file", "resume", "upload"].includes(rawType)) {
    const acceptsResume = question.accepts_resume === true || ["resume", "upload"].includes(rawType);
    control = createElement("input", {
      attrs: {
        ...attrs,
        value: acceptsResume
          ? "Active résumé is supplied securely by the worker"
          : "This provider file field requires Live View",
        readonly: "",
      },
    });
    control.dataset.systemAnswer = acceptsResume ? "resume" : "provider-file";
  } else {
    const inputType = ["email", "tel", "url", "number", "date"].includes(rawType) ? rawType : "text";
    control = createElement("input", { type: inputType, attrs: { ...attrs, maxlength: inputType === "text" ? "1000" : undefined } });
    control.value = value == null ? "" : String(value);
  }
  return control;
}

function formQuestionKey(question, index) {
  return String(question?.key || question?.id || question?.name || `field_${index + 1}`);
}

function formQuestionLabel(question, index) {
  const raw = String(question?.label || question?.title || question?.text || `Field ${index + 1}`);
  return raw.replace(/\s*\*+\s*$/, "").trim() || raw;
}

function formSubmissionJobForRevision(revision, applicationId = state.selectedFormApplicationId) {
  if (!revision?.id) return null;
  if (formRevisionSubmissionIsVerified(revision)) {
    return {
      status: "succeeded",
      result: revision.submission_result,
      application_id: applicationId,
      payload: { form_revision_id: revision.id },
    };
  }
  const tracked = state.formSubmissionJobs.get(revision.id);
  if (tracked?.id || tracked?.status) return tracked;
  const historical = state.automationJobs.find((job) => {
    if (job.kind !== "application_submit" || job.application_id !== applicationId) return false;
    const queuedRevisionId = job.form_revision_id
      || job.payload?.form_revision_id
      || job.result?.form_revision_id
      || null;
    const applicationRevisions = state.formRevisions[applicationId] || [];
    return queuedRevisionId === revision.id || (!queuedRevisionId && applicationRevisions.length === 1);
  });
  if (historical) return historical;
  if (revision.submission_result?.submission_state) {
    return {
      status: revision.status === "submitted" ? "succeeded" : "needs_attention",
      result: revision.submission_result,
      application_id: applicationId,
      payload: { form_revision_id: revision.id },
    };
  }
  return null;
}

function setFormSubmitRoute(stage = "review", tone = "active") {
  const order = ["review", "queue", "submit", "verify"];
  const target = Math.max(0, order.indexOf(stage));
  all("[data-form-submit-stage]", byId("form-submit-preflight")).forEach((node, index) => {
    node.classList.toggle("is-complete", tone === "verified" || index < target);
    node.classList.toggle("is-current", tone !== "verified" && index === target);
    node.classList.toggle("needs-attention", tone === "attention" && index === target);
    if (tone !== "verified" && index === target) node.setAttribute("aria-current", "step");
    else node.removeAttribute("aria-current");
  });
}

function updateFormSubmitTicket({
  heading = "Review readiness",
  detail = "Check every required answer before submission.",
  status = "Waiting",
  statusTone = "status-neutral",
  issues = [],
  stage = "review",
  tone = "active",
} = {}) {
  const panel = byId("form-submit-preflight");
  if (!panel) return;
  panel.hidden = false;
  panel.classList.toggle("is-ready", tone === "ready");
  panel.classList.toggle("is-running", tone === "active" && stage !== "review");
  panel.classList.toggle("is-verified", tone === "verified");
  panel.classList.toggle("needs-attention", tone === "attention");
  setText("form-submit-preflight-heading", heading);
  setText("form-submit-preflight-detail", detail);
  const pill = byId("form-submit-preflight-status");
  pill.className = `status-pill ${statusTone}`;
  pill.textContent = status;
  const list = byId("form-submit-missing-list");
  clearNode(list);
  for (const issue of issues) list.append(createElement("li", { text: issue }));
  list.hidden = issues.length === 0;
  setFormSubmitRoute(stage, tone);
}

function activeFormResume(revision) {
  if (revision?.resume_id) return { id: revision.resume_id };
  return state.resumes.find((resume) => resume.is_active !== false) || state.resumes[0] || null;
}

function formRevisionPreflight(revision, { markFields = false } = {}) {
  const controls = new Map(
    all("[data-answer-key]", byId("form-revision-answers")).map((control) => [control.dataset.answerKey, control]),
  );
  const missing = [];
  const missingKeys = new Set();
  const warnings = [];
  const resume = activeFormResume(revision);
  revisionQuestions(revision).forEach((question, index) => {
    const key = formQuestionKey(question, index);
    const label = formQuestionLabel(question, index);
    const rawType = String(question.type || question.input_type || "text").toLowerCase();
    const needsExactAnswer = question.required === true;
    const control = controls.get(key);
    const isResume = control?.dataset.systemAnswer === "resume"
      || (rawType === "file" && question.accepts_resume === true)
      || ["resume", "upload"].includes(rawType);
    const providerFile = control?.dataset.systemAnswer === "provider-file";
    const answered = isResume ? Boolean(resume?.id) : formControlHasAnswer(control);
    if (providerFile && needsExactAnswer) {
      missing.push(`${label}: attach this file in Live View`);
      missingKeys.add(key);
    } else if (needsExactAnswer && !answered) {
      missing.push(isResume ? `${label}: upload and activate a résumé first` : label);
      missingKeys.add(key);
    }
    if (question.disabled && needsExactAnswer) {
      warnings.push(`${label} is provider-controlled and may require Live View.`);
    }
  });
  if (markFields) {
    all(".form-answer-row", byId("form-revision-answers")).forEach((row) => {
      const invalid = missingKeys.has(row.dataset.answerKey);
      row.classList.toggle("has-preflight-error", invalid);
      const control = row.querySelector("[data-answer-key]");
      if (control) {
        if (invalid) control.setAttribute("aria-invalid", "true");
        else control.removeAttribute("aria-invalid");
      }
    });
  }
  return { ready: missing.length === 0, missing, warnings };
}

function formSubmissionIsVerified(job) {
  return Boolean(
    job?.status === "succeeded"
      && job.result?.code === "application_submitted"
      && job.result?.submission_state === "confirmed",
  );
}

function formRevisionSubmissionIsVerified(revision) {
  const result = revision?.submission_result;
  return Boolean(
    revision?.status === "submitted"
      && result?.submission_state === "confirmed",
  );
}

function hideFormSubmissionRecovery() {
  const panel = byId("form-submission-recovery");
  if (!panel) return;
  panel.hidden = true;
  const original = byId("form-open-original");
  original.hidden = true;
  original.removeAttribute("href");
  for (const id of ["form-mark-submitted", "form-prepare-submission-retry"]) {
    const button = byId(id);
    button.disabled = false;
    button.dataset.revisionId = "";
  }
}

function showFormSubmissionRecovery(revision, result = {}) {
  const panel = byId("form-submission-recovery");
  if (!panel || !revision?.id) return;
  panel.hidden = false;
  setText(
    "form-submission-recovery-detail",
    "The provider did not return a clear confirmation. Keep this sealed revision unchanged while you check the original form, your inbox, or the provider's response page.",
  );
  const originalUrl = safeHttpUrl(revision.form_url || result.form_url);
  const original = byId("form-open-original");
  original.hidden = !originalUrl;
  if (originalUrl) original.href = originalUrl;
  else original.removeAttribute("href");
  for (const id of ["form-mark-submitted", "form-prepare-submission-retry"]) {
    const button = byId(id);
    button.disabled = false;
    button.dataset.revisionId = revision.id;
  }
}

function formRevisionIsCurrent(revision, applicationId = state.selectedFormApplicationId) {
  return Boolean(
    revision?.id
      && applicationId
      && state.selectedFormApplicationId === applicationId
      && state.selectedFormRevisionId === revision.id
      && latestFormRevision(applicationId)?.id === revision.id,
  );
}

function renderFormSubmissionJob(revision, job, applicationId = state.selectedFormApplicationId) {
  if (
    !revision
      || !job
      || !formRevisionIsCurrent(revision, applicationId)
      || formPreparationIsActive(applicationId)
  ) return false;
  const button = byId("submit-form-revision");
  const liveLink = byId("form-live-review-link");
  const status = String(job.status || "queued").toLowerCase();
  const result = job.result && typeof job.result === "object" ? job.result : {};
  const detail = formJobDetail(job, "The background form submission is being processed.");
  const fallbackUrl = safeBrowserbaseLiveViewUrl(result.live_view_url);
  const missing = Array.isArray(result.missing_required)
    ? result.missing_required.slice(0, 12).map((label) => `Missing on provider form: ${String(label)}`)
    : [];
  liveLink.hidden = true;
  liveLink.removeAttribute("href");
  hideFormSubmissionRecovery();
  setFormScanRetry(state.selectedFormApplicationId, false);
  button.disabled = true;

  if (status === "queued") {
    button.textContent = "Submission queued";
    updateFormSubmitTicket({
      heading: "Submission queued",
      detail: "Your exact approved revision is sealed. A worker will claim this one-time submission; no duplicate retry is created.",
      status: "Queued",
      statusTone: "status-info",
      stage: "queue",
    });
    showFormWorkflowProgress({ title: "Submission queued", detail, value: "Queued", percent: 34 });
    return true;
  }
  if (status === "running") {
    button.textContent = "Submitting…";
    updateFormSubmitTicket({
      heading: "Submitting in the secure browser",
      detail: detail || "The worker is filling the sealed answers and waiting for a clear provider confirmation.",
      status: "Running",
      statusTone: "status-info",
      stage: "submit",
    });
    showFormWorkflowProgress({ title: "Submitting in the secure browser", detail, value: "Running", percent: 72 });
    return true;
  }
  if (formSubmissionIsVerified(job) || formRevisionSubmissionIsVerified(revision)) {
    const verifiedByUser = formRevisionSubmissionIsVerified(revision)
      && ["user", "user_resolution"].includes(revision.submission_result?.verification_source);
    button.textContent = "Submitted · verified";
    updateFormSubmitTicket({
      heading: verifiedByUser ? "Submission recorded after your verification" : "Provider confirmation verified",
      detail: verifiedByUser
        ? "You verified that the provider received this application. AutoApply recorded that outcome and permanently closed this one-time revision."
        : "The provider displayed a new confirmation and AutoApply recorded this application. No further action is needed.",
      status: "Verified success",
      statusTone: "status-success",
      stage: "verify",
      tone: "verified",
    });
    showFormWorkflowProgress({ title: "Application submitted", detail, value: "Verified success", percent: 100, tone: "complete" });
    setFormMessage("form-revision-message", "Submitted once and verified from the provider confirmation.", "success");
    const applicationPill = byId("form-application-status-pill");
    applicationPill.className = "status-pill status-success";
    applicationPill.textContent = "Submitted · verified";
    return true;
  }

  const uncertain = result.submission_state === "uncertain";
  const attentionDetail = uncertain
    ? `${detail} The provider may have received the application; do not submit it again until you verify the outcome.`
    : detail;
  button.textContent = uncertain ? "Outcome needs verification" : "Submission needs attention";
  updateFormSubmitTicket({
    heading: uncertain ? "Verify the provider outcome" : "A person needs to finish this submission",
    detail: attentionDetail,
    status: "Needs attention",
    statusTone: "status-warning",
    issues: missing,
    stage: result.code === "required_answers_missing" ? "submit" : "verify",
    tone: "attention",
  });
  showFormWorkflowProgress({ title: "Submission needs attention", detail: attentionDetail, value: "Needs attention", percent: 100, tone: "attention" });
  setFormMessage("form-revision-message", attentionDetail, "error");
  const applicationPill = byId("form-application-status-pill");
  applicationPill.className = "status-pill status-warning";
  applicationPill.textContent = "Needs attention";
  if (fallbackUrl) {
    liveLink.href = fallbackUrl;
    liveLink.hidden = false;
  }
  if (uncertain) showFormSubmissionRecovery(revision, result);
  else if (result.submission_state === "not_attempted") {
    setFormScanRetry(state.selectedFormApplicationId, true);
  }
  return true;
}

function refreshFormSubmitPreflight({ markFields = false } = {}) {
  const applicationId = byId("form-application-id").value;
  const revision = latestFormRevision(applicationId);
  if (!revision?.id) return null;
  if (formPreparationIsActive(applicationId)) {
    const scanJob = activeFormScanJob(applicationId);
    const running = String(scanJob?.status || "").toLowerCase() === "running";
    const button = byId("submit-form-revision");
    hideFormSubmissionRecovery();
    button.textContent = running ? "Capturing current form…" : "Waiting for current form…";
    button.disabled = true;
    button.title = "Approval stays locked until Form Pilot finishes capturing the provider's current fields.";
    updateFormSubmitTicket({
      heading: "Refreshing the provider fields",
      detail: "Approval is paused until Form Pilot finishes capturing the current form. Nothing can be submitted during this refresh.",
      status: running ? "Capturing" : "Queued",
      statusTone: "status-info",
      stage: "review",
    });
    return { locked: true, reason: "form_scan_active", scanJob };
  }
  const submissionJob = formSubmissionJobForRevision(revision, applicationId);
  if (submissionJob) {
    renderFormSubmissionJob(revision, submissionJob);
    return null;
  }
  hideFormSubmissionRecovery();
  const report = formRevisionPreflight(revision, { markFields });
  const capability = capabilityForProvider(revision.provider);
  const canSubmit = capability?.can_auto_apply === true;
  const button = byId("submit-form-revision");
  button.textContent = "Approve & submit in background";
  button.disabled = !report.ready || !canSubmit;
  button.title = canSubmit
    ? "Seal these exact answers and queue one background submission"
    : capability?.reason || "Background submission is not enabled for this provider.";
  updateFormSubmitTicket({
    heading: report.ready ? "Ready for your final action" : "Complete the required answers",
    detail: report.ready
      ? `All required answers are present.${report.warnings.length ? " A provider-controlled field may still require Live View." : ""} This exact revision will be sealed before one background submission is queued.`
      : `${report.missing.length} required answer${report.missing.length === 1 ? " is" : "s are"} still missing. Complete ${report.missing.length === 1 ? "it" : "them"} before the one-time action is enabled.`,
    status: report.ready ? (canSubmit ? "Ready" : "Unavailable") : `${report.missing.length} missing`,
    statusTone: report.ready && canSubmit ? "status-success" : "status-warning",
    issues: [...report.missing, ...report.warnings],
    stage: "review",
    tone: report.ready && canSubmit ? "ready" : "attention",
  });
  return { report, capability, canSubmit };
}

function renderFormRevision(revision) {
  const container = byId("form-revision-answers");
  clearNode(container);
  const status = byId("form-revision-status");
  const liveLink = byId("form-live-review-link");
  const preflight = byId("form-submit-preflight");
  liveLink.hidden = true;
  liveLink.removeAttribute("href");
  hideFormSubmissionRecovery();
  if (!revision) {
    preflight.hidden = true;
    renderFormScanPlaceholder(byId("form-application-id")?.value || state.selectedFormApplicationId || "");
    return;
  }
  setFormScanRetry(state.selectedFormApplicationId, false);
  const approved = Boolean(revision.approved_at) || revision.status === "approved";
  status.className = `status-pill ${approved ? "status-success" : "status-warning"}`;
  status.textContent = approved ? `Revision ${revision.revision} approved` : `Revision ${revision.revision} needs review`;
  setText("form-revision-context", `${humanize(revision.provider || "application form")} · ${revisionQuestions(revision).length} captured field${revisionQuestions(revision).length === 1 ? "" : "s"}. Approval applies only to schema ${String(revision.schema_hash || "").slice(0, 10) || "unknown"}.`);
  const storedAnswers = revision.answers && typeof revision.answers === "object" ? revision.answers : {};
  const cachedAnswers = state.formSuggestionCache.get(revision.id) || {};
  // The API derives these deterministic values from the saved Profile for an
  // unsealed revision. Keep them last so a model can never replace an exact
  // profile fact such as the public resume URL or graduation-year option.
  // Sealed revisions deliberately receive an empty profile_answers object.
  const profileAnswers = revision.profile_answers && typeof revision.profile_answers === "object"
    ? revision.profile_answers
    : {};
  const answers = { ...storedAnswers, ...cachedAnswers, ...profileAnswers };
  const questions = revisionQuestions(revision);
  if (!questions.length) {
    container.append(emptyState("No fillable questions found", "Open the source form to confirm whether it is still available.", "◇"));
  }
  questions.forEach((question, index) => {
    const answerKey = formQuestionKey(question, index);
    const labelText = formQuestionLabel(question, index);
    const wrapper = createElement("div", { className: "form-answer-row", attrs: { "data-answer-row": answerKey } });
    wrapper.dataset.answerKey = answerKey;
    const field = createElement("div", { className: "field" });
    const label = createElement("label", { text: labelText });
    if (question.required) label.append(createElement("span", { className: "question-required", text: " *", attrs: { "aria-label": "required" } }));
    const control = formQuestionControl(question, answers[answerKey] ?? question.suggested_answer ?? null, answerKey);
    if (approved) control.disabled = true;
    label.htmlFor = `form-answer-${index}`;
    control.id = `form-answer-${index}`;
    field.append(label, control);
    if (question.prefilled) {
      field.append(createElement("small", {
        text: question.disabled
          ? "The provider has a locked value here, so automation will stop for manual review."
          : "The provider already contains a value. Re-enter the exact approved value, or leave this blank to clear it.",
      }));
    }
    if (question.help_text || question.description) field.append(createElement("small", { text: question.help_text || question.description }));
    wrapper.append(field);
    container.append(wrapper);
  });
  refreshFormSubmitPreflight();
  setFormMessage(
    "form-revision-message",
    approved
      ? "This revision is already approved and sealed. Queue its one-time background submission when ready."
      : credentialConfigured("groq")
        ? "Profile facts and Groq suggestions are applied automatically once per captured revision. Review every answer before approving and submitting."
        : "Saved Profile facts are applied automatically. Connect Groq in Profile or Connections for open-ended answers, then review the completed form.",
  );
}

async function resolveFormSubmissionOutcome(outcome, button) {
  const applicationId = byId("form-application-id").value;
  const revision = latestFormRevision(applicationId);
  if (!revision?.id || button?.dataset.revisionId !== revision.id) {
    toast("This form changed while you were reviewing it. Refresh the review desk before resolving the outcome.", "error");
    return;
  }
  const submissionJob = formSubmissionJobForRevision(revision, applicationId);
  const submissionState = submissionJob?.result?.submission_state
    || revision.submission_result?.submission_state;
  if (submissionState !== "uncertain") {
    toast("This submission no longer has an uncertain outcome. The review desk will refresh now.", "info");
    const identity = identitySnapshot();
    await Promise.all([
      loadFormApplications(true, identity),
      loadAutomationJobs(true, identity),
    ]);
    await loadApplicationFormRevisions(applicationId, true, false, identity);
    return;
  }

  const submitted = outcome === "submitted";
  if (!await confirmAction({
    eyebrow: "Resolve the provider outcome",
    title: submitted ? "Record this form as submitted?" : "Capture the current form for a safe retry?",
    message: submitted
      ? "Use this only after you verify that the provider received the application. AutoApply will record it as submitted and will not send the sealed revision again."
      : "Use this only after you verify that the provider did not receive the application. AutoApply will preserve this sealed attempt, then capture the provider's current fields for a new review. Nothing is submitted now.",
    confirmLabel: submitted ? "Mark as submitted" : "Capture current form",
    cancelLabel: "Keep outcome unresolved",
    tone: "caution",
    ticketLabel: submitted ? "Verified by you" : "New revision",
    symbol: submitted ? "✓" : "↻",
  })) return;

  const identity = identitySnapshot();
  await withBusy(button, submitted ? "Recording…" : "Preparing…", async () => {
    try {
      const resolutionPayload = await apiRequest(`/application-form-revisions/${encodeURIComponent(revision.id)}/resolve-submission`, {
        method: "POST",
        body: {
          outcome,
          expected_revision: Number(revision.revision),
          schema_hash: revision.schema_hash,
        },
        identity,
      });
      assertCurrentIdentity(identity);
      state.formSubmissionJobs.delete(revision.id);
      if (!submitted) state.formRecoveryScanApplicationIds.add(applicationId);
      await Promise.all([
        loadFormApplications(true, identity),
        loadAutomationJobs(true, identity),
      ]);
      // A not-submitted resolution creates a safe fallback revision before the
      // current provider fields are captured. Keep that fallback locked and do
      // not auto-suggest it: it must never look ready during the rescan gap.
      await loadApplicationFormRevisions(applicationId, true, false, identity);
      assertCurrentIdentity(identity);
      if (submitted) {
        toast("The verified provider outcome is recorded. This sealed revision will not be submitted again.", "success", "Marked as submitted");
      } else {
        const resolution = resolutionPayload?.resolution || {};
        const application = formApplicationById(applicationId);
        const job = application ? jobForApplication(application) : null;
        if (resolution.rescan_required !== false && job?.id) {
          toast("The old attempt is preserved. Form Pilot is now capturing the provider's current fields; nothing has been submitted.", "success", "Fresh scan starting");
          // The recovery button is already busy, so queue the scan without a
          // nested busy wrapper and await the handoff. This removes the window
          // where the fallback revision used to be interactive.
          await scanJobApplication(job, providerForJob(job) || revision.provider || "google_forms", null);
        } else {
          state.formRecoveryScanApplicationIds.delete(applicationId);
          refreshFormSubmitPreflight();
          await maybeSuggestFormAnswers(applicationId, latestFormRevision(applicationId));
          toast("A fresh unapproved revision is ready. Review every answer before creating a new one-time submission.", "success", "Retry prepared");
          requestAnimationFrame(() => byId("form-revision-answers")?.scrollIntoView({ behavior: "smooth", block: "start" }));
        }
      }
    } catch (error) {
      if (isIdentityChanged(error)) return;
      const message = errorMessage(error, "The submission outcome could not be resolved.");
      setFormMessage("form-revision-message", message, "error");
      toast(message, "error", "Outcome still unresolved", 9_000);
      renderFormSubmissionJob(revision, submissionJob);
    } finally {
      if (!submitted) {
        state.formRecoveryScanApplicationIds.delete(applicationId);
        if (state.selectedFormApplicationId === applicationId) refreshFormSubmitPreflight();
      }
    }
  });
}

function formRevisionAnswers() {
  const answers = {};
  for (const control of all("[data-answer-key]", byId("form-revision-answers"))) {
    if (control.dataset.systemAnswer) continue;
    const key = control.dataset.answerKey;
    if (!key) continue;
    if (control.type === "checkbox") answers[key] = control.checked;
    else if (control.multiple) answers[key] = Array.from(control.selectedOptions).map((option) => option.value);
    else answers[key] = control.value;
  }
  return answers;
}

function formRevisionCanAutoSuggest(revision) {
  return Boolean(
    revision?.id
      && !(revision.approved_at || revision.status === "approved")
      && revisionQuestions(revision).length
      && !state.formSuggestionAttempts.has(revision.id),
  );
}

function formControlHasAnswer(control) {
  if (!control) return false;
  if (control.type === "checkbox") return control.checked;
  if (control.multiple) return control.selectedOptions.length > 0;
  return String(control.value || "").trim().length > 0;
}

function applyFormSuggestions(suggestions, { preserveCompleted = false } = {}) {
  let applied = 0;
  for (const control of all("[data-answer-key]", byId("form-revision-answers"))) {
    const key = control.dataset.answerKey;
    if (!key || !Object.hasOwn(suggestions, key) || control.dataset.systemAnswer) continue;
    if (preserveCompleted && formControlHasAnswer(control)) continue;
    const value = suggestions[key];
    if (control.type === "checkbox" && typeof value === "boolean") {
      control.checked = value;
      applied += 1;
    } else if (control.multiple && Array.isArray(value)) {
      const selected = new Set(value.map(String));
      for (const option of control.options) option.selected = selected.has(option.value);
      applied += 1;
    } else if (control.tagName === "SELECT") {
      const candidate = String(value);
      if (Array.from(control.options).some((option) => option.value === candidate)) {
        control.value = candidate;
        applied += 1;
      }
    } else if (["string", "number"].includes(typeof value)) {
      control.value = String(value);
      applied += 1;
    }
  }
  return applied;
}

async function requestFormSuggestions(revision, { button = null, automatic = false } = {}) {
  if (!revision?.id) return false;
  if (automatic && state.formSuggestionAttempts.has(revision.id)) return false;
  if (automatic) state.formSuggestionAttempts.add(revision.id);
  const run = async () => {
    showFormWorkflowProgress({
      title: "Preparing grounded answers",
      detail: credentialConfigured("groq")
        ? "AutoApply applies exact Profile facts first, then asks Groq only for remaining answers grounded in your active résumé and this role."
        : "AutoApply is applying exact saved Profile facts. Unknown questions stay blank for your review.",
      value: "Drafting",
      percent: 76,
    });
    try {
      const payload = await apiRequest(`/application-form-revisions/${encodeURIComponent(revision.id)}/suggest`, {
        method: "POST",
        // The endpoint always returns deterministic Profile facts. It uses Groq
        // only when this browser has supplied a key and open questions remain.
        ...(credentialConfigured("groq") ? { groq: true } : {}),
      });
      if (state.selectedFormRevisionId !== revision.id) return false;
      const suggestionData = unwrapData(payload) || {};
      const suggestions = suggestionData.answers || {};
      const source = String(suggestionData.source || "groq");
      const applied = applyFormSuggestions(suggestions, { preserveCompleted: automatic });
      state.formSuggestionCache.set(revision.id, formRevisionAnswers());
      refreshFormSubmitPreflight({ markFields: true });
      const preparedBy = source.startsWith("profile") ? "Profile facts ready" : "Grounded suggestions ready";
      showFormWorkflowProgress({
        title: preparedBy,
        detail: applied
          ? `${applied} answer${applied === 1 ? " was" : "s were"} filled. Review every field before approval.`
          : source.startsWith("profile")
            ? "Your saved Profile facts are already applied. Complete any genuinely unknown fields manually."
            : "Groq did not find another answer it could safely ground. Complete any remaining fields manually.",
        value: "Review now",
        percent: 100,
        tone: "complete",
      });
      setFormMessage(
        "form-revision-message",
        applied
          ? `${applied} grounded suggestion${applied === 1 ? " was" : "s were"} ${automatic ? "added automatically" : "refreshed"}. Review every answer before approving.`
          : source.startsWith("profile")
            ? "Your exact saved Profile facts are already applied. Complete any remaining unknown fields manually."
            : "Groq found no additional answers it could ground in your supplied facts. Complete the remaining fields manually.",
        applied ? "success" : "",
      );
      window.setTimeout(() => {
        if (state.selectedFormRevisionId === revision.id) hideFormWorkflowProgress();
      }, 1_800);
      return true;
    } catch (error) {
      showFormWorkflowProgress({
        title: "Suggestions need attention",
        detail: `${errorMessage(error, "Form suggestions could not be generated.")} You can still complete the captured fields manually.`,
        value: "Not generated",
        percent: 100,
        tone: "error",
      });
      setFormMessage("form-revision-message", errorMessage(error, "Form suggestions could not be generated."), "error");
      return false;
    } finally {
      // Preparation is automatic. The review desk intentionally has no second
      // AI-fill action that could overwrite an answer the user has just edited.
    }
  };
  return button ? withBusy(button, "Preparing…", run) : run();
}

async function maybeSuggestFormAnswers(applicationId, revision) {
  if (state.selectedFormApplicationId !== applicationId || !formRevisionCanAutoSuggest(revision)) return false;
  return requestFormSuggestions(revision, { automatic: true });
}

async function approveAndSubmitFormRevision(button) {
  const applicationId = byId("form-application-id").value;
  let revision = latestFormRevision(applicationId);
  if (!revision?.id) return;
  if (formPreparationIsActive(applicationId)) {
    refreshFormSubmitPreflight();
    toast("Form Pilot is still capturing the provider's current fields. Review and submission unlock when that refresh finishes.", "info", "Current form is still loading");
    return;
  }
  const preflight = formRevisionPreflight(revision, { markFields: true });
  if (!preflight.ready) {
    updateFormSubmitTicket({
      heading: "Complete the required answers",
      detail: "Nothing was queued. Complete the highlighted answers, then review the one-time action again.",
      status: `${preflight.missing.length} missing`,
      statusTone: "status-warning",
      issues: [...preflight.missing, ...preflight.warnings],
      stage: "review",
      tone: "attention",
    });
    setFormMessage("form-revision-message", "Complete every highlighted required answer before submitting.", "error");
    byId("form-revision-answers").querySelector('[aria-invalid="true"]')?.focus();
    return;
  }
  const capability = capabilityForProvider(revision.provider);
  if (capability?.can_auto_apply !== true) {
    const message = capability?.reason || "Background submission is not enabled for this provider.";
    updateFormSubmitTicket({
      heading: "Background submission is unavailable",
      detail: message,
      status: "Unavailable",
      statusTone: "status-warning",
      stage: "review",
      tone: "attention",
    });
    setFormMessage("form-revision-message", message, "error");
    return;
  }
  const existingJob = formSubmissionJobForRevision(revision, applicationId);
  if (existingJob) {
    renderFormSubmissionJob(revision, existingJob);
    return;
  }
  const identity = identitySnapshot();
  await withBusy(button, "Approving & queueing…", async () => {
    try {
      if (!(revision.approved_at || revision.status === "approved")) {
        showFormWorkflowProgress({
          title: "Sealing your reviewed answers",
          detail: "Form Pilot is locking this exact schema and answer set before creating one background submission.",
          value: "Approving",
          percent: 16,
        });
        updateFormSubmitTicket({
          heading: "Sealing this exact revision",
          detail: "Your reviewed answers are being locked to the captured form schema.",
          status: "Approving",
          statusTone: "status-info",
          stage: "review",
        });
        await apiRequest(`/application-form-revisions/${encodeURIComponent(revision.id)}/approve`, {
          method: "POST",
          body: {
            expected_revision: Number(revision.revision),
            schema_hash: revision.schema_hash,
            answers: formRevisionAnswers(),
          },
          identity,
        });
        state.formSuggestionCache.delete(revision.id);
        await loadApplicationFormRevisions(applicationId, true, false, identity);
        assertCurrentIdentity(identity);
        revision = latestFormRevision(applicationId);
        if (formPreparationIsActive(applicationId)) {
          throw new AppError(
            "The provider form is being refreshed. Nothing was queued; review the newly captured fields when they appear.",
            "form_scan_active",
          );
        }
        if (!revision?.id || !(revision.approved_at || revision.status === "approved")) {
          throw new AppError("The reviewed answers could not be sealed. Refresh the form and try again.", "form_revision_not_approved");
        }
      }
      if (formPreparationIsActive(applicationId) || !formRevisionIsCurrent(revision, applicationId)) {
        throw new AppError(
          "The provider form changed while this revision was being prepared. Nothing was queued; review the current fields first.",
          "form_revision_changed",
        );
      }
      updateFormSubmitTicket({
        heading: "Queueing one background submission",
        detail: "The approved revision is sealed. Form Pilot is creating one durable worker job now.",
        status: "Queueing",
        statusTone: "status-info",
        stage: "queue",
      });
      const payload = await apiRequest(`/application-form-revisions/${encodeURIComponent(revision.id)}/submit`, {
        method: "POST",
        body: {
          idempotency_key: `form-submit-${revision.id}`,
          form_revision_id: revision.id,
        },
        identity,
      });
      const queued = unwrapData(payload) || {};
      if (!queued.id) throw new AppError("The submission was accepted but no trackable worker job was returned.", "form_submit_job_missing");
      state.formSubmissionJobs.set(revision.id, queued);
      rememberAutomationJob(queued);
      renderFormSubmissionJob(revision, queued);
      const outcome = await monitorFormWorkflowJob(queued.id, identity, (job) => {
        state.formSubmissionJobs.set(revision.id, job);
        renderFormSubmissionJob(revision, job);
      });
      assertCurrentIdentity(identity);
      if (outcome.timedOut) {
        const latest = outcome.job || queued;
        state.formSubmissionJobs.set(revision.id, latest);
        if (formRevisionIsCurrent(revision, applicationId)) {
          updateFormSubmitTicket({
            heading: "Submission is still running",
            detail: "The durable worker job is continuing in Activity. No duplicate retry has been created; check this same submission before taking another action.",
            status: "Still running",
            statusTone: "status-info",
            stage: latest.status === "running" ? "submit" : "queue",
          });
          showFormWorkflowProgress({
            title: "Submission is still running",
            detail: "The same durable job remains active in Activity. Form Pilot will not create a duplicate submission.",
            value: "Check Activity",
            percent: latest.status === "running" ? 76 : 38,
          });
          setFormMessage("form-revision-message", "This submission is still running in Activity. Do not submit the form again.");
        }
        return;
      }
      const completed = outcome.job || queued;
      state.formSubmissionJobs.set(revision.id, completed);
      renderFormSubmissionJob(revision, completed);
      await Promise.all([
        loadAutomationJobs(true, identity),
        loadFormApplications(true, identity),
      ]);
      if (formRevisionIsCurrent(revision, applicationId)) {
        if (formSubmissionIsVerified(completed)) {
          toast("The provider confirmation was verified and this form was submitted once.", "success", "Application submitted");
        } else {
          toast("The provider did not return a verified success. Review the attention details before taking another action.", "error", "Submission needs attention", 9_000);
        }
      }
    } catch (error) {
      if (isIdentityChanged(error)) return;
      if (!formRevisionIsCurrent(revision, applicationId)) return;
      const message = errorMessage(error, "This form could not be approved and queued.");
      showFormWorkflowProgress({
        title: "Submission was not queued",
        detail: message,
        value: "Needs attention",
        percent: 100,
        tone: "attention",
      });
      updateFormSubmitTicket({
        heading: "Submission was not queued",
        detail: message,
        status: "Needs attention",
        statusTone: "status-warning",
        stage: "review",
        tone: "attention",
      });
      setFormMessage("form-revision-message", message, "error");
      toast(message, "error", "Check this form", 9_000);
    }
  });
  const current = latestFormRevision(applicationId);
  const tracked = current?.id ? state.formSubmissionJobs.get(current.id) : null;
  if (current && tracked) renderFormSubmissionJob(current, tracked);
}

function updateApplicationCharacterCount() {
  setText("application-character-count", `${byId("application-body").value.length.toLocaleString()} characters`);
}

async function saveApplication(event) {
  event.preventDefault();
  const id = byId("application-id").value;
  if (!id) return;
  const button = event.submitter;
  await withBusy(button, "Saving…", async () => {
    try {
      const payload = await apiRequest(`/applications/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: applicationPayloadFromEditor(),
      });
      const updated = unwrapData(payload);
      state.applicationEditorDirty = false;
      await loadApplications(true);
      if (updated?.id) selectApplication(updated.id);
      setFormMessage("application-editor-message", "Edits saved. Approval is still required.", "success");
    } catch (error) {
      setFormMessage("application-editor-message", errorMessage(error, "The draft could not be saved."), "error");
    }
  });
}

function applicationPayloadFromEditor() {
  return {
    recipient: byId("application-recipient").value.trim() || null,
    subject: byId("application-subject").value.trim() || null,
    body: byId("application-body").value.trim() || null,
  };
}

async function approveApplication(button) {
  const id = byId("application-id").value;
  if (!id) return;
  await withBusy(button, "Approving…", async () => {
    try {
      const updatedPayload = await apiRequest(`/applications/${encodeURIComponent(id)}`, {
        method: "PATCH",
        body: applicationPayloadFromEditor(),
      });
      const updated = unwrapData(updatedPayload);
      const expectedRevision = Number(updated?.content_revision);
      if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
        throw new AppError("The saved draft did not include a valid content revision. Refresh it before approving.", "revision_missing");
      }
      const payload = await apiRequest(`/applications/${encodeURIComponent(id)}/approve`, {
        method: "POST",
        body: { expected_revision: expectedRevision },
      });
      const approved = unwrapData(payload);
      state.applicationEditorDirty = false;
      await loadApplications(true);
      if (approved?.id) selectApplication(approved.id);
      setFormMessage("application-editor-message", "Approved. This draft can now be sent once through Gmail.", "success");
      toast("Application approved for a single reviewed send.", "success");
    } catch (error) {
      setFormMessage("application-editor-message", errorMessage(error, "The draft could not be approved."), "error");
    }
  });
}

async function reconcileApplication(application, button) {
  await withBusy(button, "Checking…", async () => {
    try {
      const payload = await apiRequest(`/applications/${encodeURIComponent(application.id)}/reconcile`, { method: "POST" });
      const reconciled = unwrapData(payload);
      await loadApplications(true);
      if (reconciled?.id && state.selectedApplicationId === reconciled.id) selectApplication(reconciled.id);
      toast("The stale send was released for manual review. Confirm Gmail delivery before trying anything else.", "success", "Send needs attention", 0);
    } catch (error) {
      toast(errorMessage(error, "This send could not be reconciled yet."), "error");
    }
  });
}

async function sendApplication(button) {
  const id = byId("application-id").value;
  if (!id) return;
  if (state.applicationEditorDirty) {
    toast("Save and re-approve the current edits before sending.", "error");
    return;
  }
  if (!isGmailConnected()) {
    toast("Connect Gmail before sending an approved draft.", "error");
    switchView("connections");
    return;
  }
  const attachResume = byId("application-attach-resume").checked;
  const attachmentCopy = attachResume ? " with your active résumé attached" : " without a résumé attachment";
  if (!await confirmAction({
    eyebrow: "Approved Gmail message",
    title: "Send this email?",
    message: `Gmail will send the exact approved message${attachmentCopy}. Sending cannot be reversed from AutoApply.`,
    confirmLabel: "Send email",
    cancelLabel: "Review message",
    tone: "caution",
    ticketLabel: "Final send",
    symbol: "@",
  })) return;
  await withBusy(button, "Sending…", async () => {
    try {
      const payload = await apiRequest(`/applications/${encodeURIComponent(id)}/send`, {
        method: "POST",
        body: { idempotency_key: `send-${id}-${crypto.randomUUID()}`, attach_resume: attachResume },
      });
      const sent = unwrapData(payload);
      await loadApplications(true);
      if (sent?.id) selectApplication(sent.id);
      setFormMessage("application-editor-message", "Gmail accepted the reviewed message.", "success");
      toast("Message sent once through Gmail.", "success");
    } catch (error) {
      setFormMessage("application-editor-message", errorMessage(error, "The message could not be sent."), "error");
    }
  });
}

async function loadConnections(quiet = false, identity = identitySnapshot()) {
  const container = byId("connection-list");
  if (!quiet) showLoading(container);
  const [payload, googleOauthPayload] = await Promise.all([
    apiRequest("/connections", { identity }),
    apiRequest("/connections/google-oauth-client", { identity }),
  ]);
  state.connections = unwrapItems(payload, ["connections", "providers"]);
  const googleOauthClient = unwrapData(googleOauthPayload);
  state.googleOauthClient = googleOauthClient && typeof googleOauthClient === "object"
    ? googleOauthClient
    : {};
  renderConnections();
  updateApplicationActionState();
  renderOverview();
  renderOutreach();
  renderYcDesk();
  return state.connections;
}

function normalizedProviderCredential(provider, payload) {
  const data = unwrapData(payload);
  const candidate = Array.isArray(data?.items)
    ? data.items.find((item) => item?.provider === provider)
    : data && typeof data === "object" && !Array.isArray(data) && data[provider]
      ? data[provider]
      : data;
  if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return {};

  // Treat even an authenticated status response as untrusted input. Provider
  // secrets and ciphertext must never be copied into browser state if a server
  // regression accidentally adds them to this response. Only the small,
  // documented status surface is retained here.
  const safeString = (value, maxLength = 160) => (
    typeof value === "string" && value.length <= maxLength ? value : null
  );
  const rawHint = safeString(candidate.key_hint, 32);
  const keyHint = rawHint && (rawHint.match(/[\u2022*]/g) || []).length >= 2
    ? rawHint
    : null;

  return {
    provider,
    configured: candidate.configured === true,
    verification_status: safeString(candidate.verification_status, 32) || "missing",
    verification_code: safeString(candidate.verification_code, 80),
    verified_at: safeString(candidate.verified_at, 64),
    updated_at: safeString(candidate.updated_at, 64),
    key_hint: keyHint,
    project_id_hint: safeString(candidate.project_id_hint, 48),
    project_name: safeString(candidate.project_name, 160),
    requires_reconfiguration: candidate.requires_reconfiguration === true,
  };
}

function replaceProviderCredentialState(provider, payload) {
  state.providerCredentials = {
    ...state.providerCredentials,
    [provider]: normalizedProviderCredential(provider, payload),
  };
}

function removeLegacyProviderKey(provider, userId) {
  if (provider === "groq") deleteLegacyGroqKey(userId);
  if (provider === "hunter") deleteLegacyHunterKey(userId);
}

async function migrateLegacyProviderCredentials(identity = identitySnapshot()) {
  if (state.providerCredentialMigrationUserId === identity.userId) return;
  state.providerCredentialMigrationUserId = identity.userId;
  const candidates = [
    ["groq", getLegacyGroqKey(identity.userId)],
    ["hunter", getLegacyHunterKey(identity.userId)],
  ];
  let imported = 0;
  const failures = [];
  for (const [provider, apiKey] of candidates) {
    if (!apiKey) continue;
    if (credentialConfigured(provider)) {
      try {
        removeLegacyProviderKey(provider, identity.userId);
      } catch {
        failures.push(`${humanize(provider)} is already synced, but its old browser copy could not be removed.`);
      }
      continue;
    }
    try {
      const payload = await apiRequest(`/provider-credentials/${encodeURIComponent(provider)}`, {
        method: "PUT",
        identity,
        body: { api_key: apiKey },
      });
      assertCurrentIdentity(identity);
      replaceProviderCredentialState(provider, payload);
      removeLegacyProviderKey(provider, identity.userId);
      imported += 1;
    } catch (error) {
      failures.push(errorMessage(error, `${humanize(provider)} could not be imported.`));
    }
  }
  if (imported) {
    toast(`${imported} browser-only credential${imported === 1 ? " was" : "s were"} moved into your encrypted account vault.`, "success");
  }
  if (failures.length) {
    const message = failures.join(" ");
    const banner = byId("credential-vault-message");
    if (banner) {
      banner.hidden = false;
      banner.className = "notice notice-warning credential-vault-message";
      banner.textContent = `${message} The browser copy was kept so you can retry safely.`;
    }
  }
}

async function loadProviderCredentials(quiet = false, identity = identitySnapshot()) {
  const status = byId("credential-vault-status");
  if (!quiet && status) {
    status.className = "status-pill status-info";
    status.textContent = "Refreshing credentials";
  }
  const payload = await apiRequest("/provider-credentials", { identity });
  assertCurrentIdentity(identity);
  const data = unwrapData(payload);
  state.providerCredentials = Object.fromEntries(PROVIDER_CREDENTIAL_NAMES.map((provider) => [
    provider,
    normalizedProviderCredential(provider, data),
  ]));
  state.hunterValidation = credentialConfigured("hunter")
    ? { ...providerCredential("hunter"), valid: true }
    : null;
  await migrateLegacyProviderCredentials(identity);
  assertCurrentIdentity(identity);
  state.hunterValidation = credentialConfigured("hunter")
    ? { ...providerCredential("hunter"), valid: true }
    : null;
  renderProviderCredentials();
  return state.providerCredentials;
}

function providerCredentialVerifiedCopy(credential) {
  return credential.verified_at
    ? `Validated ${formatDate(credential.verified_at, true)}`
    : credential.updated_at
      ? `Saved ${formatDate(credential.updated_at, true)}`
      : "Validated and stored securely";
}

function hunterCredentialQuotaCopy(credential = providerCredential("hunter")) {
  const quota = credential.quota && typeof credential.quota === "object" ? credential.quota : credential;
  const requests = quota.requests && typeof quota.requests === "object" ? quota.requests : {};
  const bucket = requests.searches || requests.credits || {};
  const remaining = Number.isFinite(bucket.remaining)
    ? bucket.remaining
    : Number.isFinite(quota.remaining)
      ? quota.remaining
      : null;
  const plan = quota.plan_name || credential.plan_name || "Hunter";
  return `${plan} plan${remaining == null ? " connected" : ` · ${remaining} search credit${remaining === 1 ? "" : "s"} remaining`}${quota.reset_date ? ` · resets ${quota.reset_date}` : ""}`;
}

function setCredentialMessage(provider, message = "", type = "") {
  for (const id of [`credential-${provider}-message`, provider === "groq" ? "groq-message" : provider === "hunter" ? "hunter-key-message" : null]) {
    if (id && byId(id)) setFormMessage(id, message, type);
  }
}

function renderProviderCredentials() {
  const vault = byId("provider-credential-vault");
  if (!vault) return;
  let configuredCount = 0;
  for (const provider of PROVIDER_CREDENTIAL_NAMES) {
    const credential = providerCredential(provider);
    const configured = credential.configured === true;
    const ready = credentialConfigured(provider);
    if (ready) configuredCount += 1;
    const editing = state.providerCredentialEditing.has(provider);
    const summary = byId(`credential-${provider}-summary`);
    const form = byId(`credential-form-${provider}`);
    const status = byId(`credential-${provider}-status`);
    if (summary) summary.hidden = !configured || editing;
    if (form) {
      form.hidden = configured && !editing;
      all("input", form).forEach((input) => { input.disabled = configured && !editing; });
    }
    const cancel = form?.querySelector(`[data-credential-cancel="${provider}"]`);
    if (cancel) cancel.hidden = !configured || !editing;
    if (status) {
      status.className = `status-pill ${ready ? credential.verified_at ? "status-success" : "status-info" : configured ? "status-warning" : "status-neutral"}`;
      status.textContent = ready
        ? credential.verified_at ? "Ready" : "Saved · check pending"
        : configured ? "Replace required" : "Not connected";
    }
    setText(`credential-${provider}-key-hint`, configured ? credentialHint(provider) : "");
    setText(`credential-${provider}-verified-at`, configured ? providerCredentialVerifiedCopy(credential) : "");
    if (provider === "hunter") setText("credential-hunter-quota", configured ? hunterCredentialQuotaCopy(credential) : "");
    if (provider === "browserbase") {
      const project = credential.project_name || credential.project_id_hint || "Project connected";
      setText("credential-browserbase-project", project);
    }
  }
  const vaultStatus = byId("credential-vault-status");
  if (vaultStatus) {
    vaultStatus.className = `status-pill ${configuredCount === PROVIDER_CREDENTIAL_NAMES.length ? "status-success" : configuredCount ? "status-info" : "status-neutral"}`;
    vaultStatus.textContent = `${configuredCount} of ${PROVIDER_CREDENTIAL_NAMES.length} ready`;
  }
  renderGroqState();
  renderHunterState();
  renderYcDesk();
}

function editProviderCredential(provider, { focus = true } = {}) {
  if (!PROVIDER_CREDENTIAL_NAMES.includes(provider)) return;
  state.providerCredentialEditing.add(provider);
  renderProviderCredentials();
  const input = byId(`credential-${provider}-api-key`);
  if (focus) {
    byId(`credential-card-${provider}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => input?.focus({ preventScroll: true }), 250);
  }
}

function cancelProviderCredentialEdit(provider) {
  state.providerCredentialEditing.delete(provider);
  byId(`credential-form-${provider}`)?.reset();
  setCredentialMessage(provider);
  renderProviderCredentials();
}

async function saveProviderCredential(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const provider = form.dataset.providerCredential;
  if (!PROVIDER_CREDENTIAL_NAMES.includes(provider)) return;
  const apiKey = String(new FormData(form).get("api_key") || "").trim();
  const projectId = String(new FormData(form).get("project_id") || "").trim();
  if (apiKey.length < 8) {
    setCredentialMessage(provider, `Enter a complete ${humanize(provider)} API key.`, "error");
    return;
  }
  if (provider === "browserbase" && !projectId) {
    setCredentialMessage(provider, "Enter the Browserbase Project ID shown beside your API key in Overview.", "error");
    return;
  }
  const button = event.submitter || form.querySelector('button[type="submit"]');
  await withBusy(button, "Validating…", async () => {
    try {
      const payload = await apiRequest(`/provider-credentials/${encodeURIComponent(provider)}`, {
        method: "PUT",
        body: { api_key: apiKey, ...(provider === "browserbase" ? { project_id: projectId } : {}) },
      });
      replaceProviderCredentialState(provider, payload);
      state.providerCredentialEditing.delete(provider);
      form.reset();
      if (provider === "hunter") {
        state.hunterValidation = { ...providerCredential("hunter"), valid: true };
      }
      renderProviderCredentials();
      const credential = providerCredential(provider);
      const verification = payload?.verification && typeof payload.verification === "object" ? payload.verification : {};
      if (credentialConfigured(provider)) {
        const pending = credential.verification_status === "unverified";
        setCredentialMessage(
          provider,
          pending
            ? verification.message || `${humanize(provider)} was saved; provider validation is temporarily pending.`
            : `${humanize(provider)} was validated and saved to your account.`,
          pending ? "" : "success",
        );
        toast(`${humanize(provider)} credential ${pending ? "was saved for a later check" : "is ready"}.`, pending ? "info" : "success");
      } else {
        setCredentialMessage(
          provider,
          verification.message || `${humanize(provider)} did not accept this credential. Replace it before using this service.`,
          "error",
        );
      }
    } catch (error) {
      setCredentialMessage(provider, errorMessage(error, `${humanize(provider)} could not validate this credential.`), "error");
    }
  });
}

async function deleteProviderCredential(provider, trigger = null) {
  if (!PROVIDER_CREDENTIAL_NAMES.includes(provider) || !credentialSaved(provider)) return;
  const confirmed = await confirmAction({
    eyebrow: "Account credential vault",
    title: `Delete the ${humanize(provider)} credential?`,
    message: `${humanize(provider)} features and queued work that needs this service will stop until you save a replacement key.`,
    confirmLabel: "Delete credential",
    cancelLabel: "Keep credential",
    tone: "danger",
    ticketLabel: "Encrypted secret",
    symbol: provider === "browserbase" ? "B" : provider === "hunter" ? "H" : "AI",
  });
  if (!confirmed) return;
  await withBusy(trigger, "Deleting…", async () => {
    try {
      await apiRequest(`/provider-credentials/${encodeURIComponent(provider)}`, { method: "DELETE" });
      state.providerCredentials = { ...state.providerCredentials, [provider]: {} };
      state.providerCredentialEditing.delete(provider);
      if (provider === "hunter") {
        state.hunterValidation = null;
        state.outreachContacts = {};
      }
      renderProviderCredentials();
      setCredentialMessage(provider, `${humanize(provider)} was removed from your account.`, "success");
      toast(`${humanize(provider)} credential deleted.`, "success");
    } catch (error) {
      setCredentialMessage(provider, errorMessage(error, `${humanize(provider)} could not be deleted.`), "error");
    }
  });
}

function focusProviderCredential(provider) {
  switchView("connections");
  state.providerCredentialEditing.add(provider);
  renderProviderCredentials();
  window.setTimeout(() => {
    byId(`credential-card-${provider}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    byId(`credential-${provider}-api-key`)?.focus({ preventScroll: true });
  }, 250);
}

function mergedProviders() {
  if (state.connections.length) return state.connections;
  return state.publicProviders;
}

function providerConnection(provider) {
  if (provider?.connection && typeof provider.connection === "object") return provider.connection;
  return provider;
}

function connectionStatus(provider) {
  if (!provider?.connection && ["partner_required", "manual_only"].includes(provider?.mode)) return provider.mode;
  const connection = providerConnection(provider);
  return connection?.status || provider?.connection_status || "disconnected";
}

function isGmailConnected() {
  const gmail = mergedProviders().find((provider) => String(provider.id || provider.provider || "").toLowerCase() === "gmail");
  return gmail ? ["connected", "active"].includes(connectionStatus(gmail)) : false;
}

function providerInitial(provider) {
  const id = String(provider.id || provider.provider || "").toLowerCase();
  if (id === "gmail" || id === "google") return "G";
  if (id === "linkedin") return "in";
  return (provider.label || id || "P").slice(0, 2).toUpperCase();
}

function providerIconClass(provider) {
  const id = String(provider.id || provider.provider || "").toLowerCase();
  if (id === "gmail" || id === "google") return "gmail";
  if (id === "linkedin") return "linkedin";
  if (id === "yc") return "yc";
  if (provider.mode === "public_ats") return "ats";
  return "";
}

function renderGmailRevocationWarning() {
  const warning = byId("gmail-revocation-warning");
  if (!warning) return;
  const visible = state.gmailRevocationWarning || hasGmailRevocationWarning();
  state.gmailRevocationWarning = visible;
  warning.hidden = !visible;
}

function setStatusPill(id, status, label) {
  const pill = byId(id);
  if (!pill) return;
  pill.className = `status-pill ${statusClass(status)}`;
  pill.textContent = label;
}

function googleOauthConnectedSource() {
  const configuredSource = state.googleOauthClient?.connected_source;
  if (["platform", "user"].includes(configuredSource)) return configuredSource;
  const gmail = mergedProviders().find((provider) => ["gmail", "google"].includes(
    String(provider.id || provider.provider || "").toLowerCase(),
  ));
  const connection = providerConnection(gmail);
  const metadataSource = connection?.metadata?.oauth_client_source
    || connection?.metadata?.credential_source;
  return ["platform", "user"].includes(metadataSource) ? metadataSource : null;
}

function selectedGoogleOauthMode() {
  const config = state.googleOauthClient || {};
  const connectedSource = googleOauthConnectedSource();
  if (connectedSource) return connectedSource;
  if (state.googleOauthMode === "user" && config.byoc_available === true) return "user";
  if (state.googleOauthMode === "platform" && config.platform_available === true) return "platform";
  if (config.platform_available === true) return "platform";
  return "user";
}

function renderGoogleOauthSetup() {
  const config = state.googleOauthClient || {};
  const connected = isGmailConnected();
  const connectedSource = googleOauthConnectedSource();
  const platformAvailable = config.platform_available === true;
  const byocAvailable = config.byoc_available === true;
  const saved = config.configured === true;
  const requiresReconfiguration = config.requires_reconfiguration === true;
  const configured = saved && !requiresReconfiguration;
  const mode = selectedGoogleOauthMode();
  state.googleOauthMode = mode;

  const platformRadio = byId("gmail-oauth-mode-platform");
  const userRadio = byId("gmail-oauth-mode-user");
  platformRadio.checked = mode === "platform";
  userRadio.checked = mode === "user";
  platformRadio.disabled = connected || !platformAvailable;
  userRadio.disabled = connected || !byocAvailable;
  byId("gmail-platform-choice").classList.toggle("is-selected", mode === "platform");
  byId("gmail-user-choice").classList.toggle("is-selected", mode === "user");
  byId("gmail-platform-choice").classList.toggle("is-unavailable", !platformAvailable);
  byId("gmail-user-choice").classList.toggle("is-unavailable", !byocAvailable);

  if (connected) {
    setStatusPill(
      "gmail-oauth-status",
      "connected",
      connectedSource === "user" ? "Connected · Your OAuth app" : "Connected · AutoApply app",
    );
  } else if (platformAvailable || byocAvailable) {
    setStatusPill("gmail-oauth-status", "ready", "Ready to connect");
  } else {
    setStatusPill("gmail-oauth-status", "unavailable", "Setup unavailable");
  }
  setStatusPill(
    "gmail-platform-choice-status",
    connectedSource === "platform" ? "connected" : platformAvailable ? "ready" : "unavailable",
    connectedSource === "platform" ? "Connected" : platformAvailable ? "Available" : "Unavailable",
  );
  setStatusPill(
    "gmail-user-choice-status",
    connectedSource === "user" ? "connected" : requiresReconfiguration ? "needs_attention" : configured ? "saved" : byocAvailable ? "disconnected" : "unavailable",
    connectedSource === "user" ? "Connected" : requiresReconfiguration ? "Replace required" : configured ? "Saved" : byocAvailable ? "Not saved" : "Unavailable",
  );

  byId("gmail-platform-panel").hidden = mode !== "platform";
  byId("gmail-user-panel").hidden = mode !== "user";
  setText(
    "gmail-platform-copy",
    platformAvailable
      ? "Choose your Gmail account on Google, approve send-only access, and return here."
      : "The deployment operator has not configured the shared Google app. Use your own OAuth app instead.",
  );
  byId("gmail-connect-platform").disabled = connected || !platformAvailable;

  const redirectUri = safeHttpUrl(config.redirect_uri) || "";
  byId("gmail-callback-uri").value = redirectUri;
  byId("gmail-copy-callback").disabled = !redirectUri;
  byId("gmail-oauth-client-unavailable").hidden = byocAvailable;

  byId("gmail-oauth-client-summary").hidden = !saved;
  setText("gmail-client-id-hint", config.client_id_hint || "Saved Google OAuth client");
  setText(
    "gmail-client-updated-at",
    config.updated_at ? `Updated ${formatDate(config.updated_at, true)}` : "",
  );
  byId("gmail-connect-user").disabled = connected || !byocAvailable || !configured;
  byId("gmail-replace-client").disabled = connected || !byocAvailable;
  byId("gmail-delete-client").disabled = connected || !byocAvailable;

  const showForm = byocAvailable && !connected && (!configured || state.googleOauthEditing);
  byId("gmail-oauth-client-form").hidden = !showForm;
  setText("gmail-client-form-heading", configured ? "Replace your OAuth credentials" : "Save your OAuth credentials");
  setText("gmail-save-client", configured ? "Replace OAuth app" : "Save OAuth app");
  byId("gmail-cancel-client-edit").hidden = !configured || !state.googleOauthEditing;
  for (const input of all("#gmail-oauth-client-form input")) input.disabled = !showForm;

  if (connected) {
    setFormMessage(
      "gmail-oauth-client-message",
      "Disconnect Gmail before changing or deleting the OAuth app that issued its refresh token.",
    );
  } else if (requiresReconfiguration) {
    setFormMessage(
      "gmail-oauth-client-message",
      "The saved OAuth app can no longer be validated. Enter the Client ID and Client Secret again.",
      "error",
    );
  }
}

function selectGoogleOauthMode(mode, { focus = false } = {}) {
  if (!["platform", "user"].includes(mode) || isGmailConnected()) return;
  const config = state.googleOauthClient || {};
  if (mode === "platform" && config.platform_available !== true) return;
  if (mode === "user" && config.byoc_available !== true) return;
  state.googleOauthMode = mode;
  if (mode === "platform") {
    state.googleOauthEditing = false;
    byId("gmail-oauth-client-form").reset();
  }
  setFormMessage("gmail-oauth-client-message");
  renderGoogleOauthSetup();
  if (focus) {
    const target = mode === "platform"
      ? byId("gmail-connect-platform")
      : state.googleOauthClient?.configured
        ? byId("gmail-connect-user")
        : byId("gmail-client-id");
    target?.focus();
  }
}

function focusGoogleOauthSetup() {
  const mode = selectedGoogleOauthMode();
  selectGoogleOauthMode(mode);
  byId("gmail-oauth-setup").scrollIntoView({ behavior: "smooth", block: "start" });
  window.setTimeout(() => {
    const target = mode === "platform" ? byId("gmail-connect-platform") : byId("gmail-oauth-mode-user");
    target?.focus({ preventScroll: true });
  }, 250);
}

function renderConnections() {
  const container = byId("connection-list");
  renderProviderCredentials();
  renderGmailRevocationWarning();
  renderGoogleOauthSetup();
  clearNode(container);
  const providers = mergedProviders();
  if (!providers.length) {
    container.append(emptyState("No provider catalog available", "The deployment operator has not published connection capabilities yet.", "⌁"));
    return;
  }
  const sorted = [...providers].sort((a, b) => {
    const order = { gmail: 0, yc: 1, linkedin: 2 };
    const aid = String(a.id || a.provider || "").toLowerCase();
    const bid = String(b.id || b.provider || "").toLowerCase();
    return (order[aid] ?? 10) - (order[bid] ?? 10) || String(a.label || aid).localeCompare(String(b.label || bid));
  });
  for (const provider of sorted) {
    const id = String(provider.id || provider.provider || "").toLowerCase();
    const connection = providerConnection(provider);
    const status = connectionStatus(provider);
    const card = createElement("article", { className: "connection-card" });
    card.dataset.providerCard = id;
    const icon = createElement("span", { className: `provider-icon ${providerIconClass(provider)}`, text: providerInitial(provider), attrs: { "aria-hidden": "true" } });
    const header = createElement("div", { className: "connection-card-header" }, [
      icon,
      makeStatus(status, ["partner_required", "manual_only"].includes(provider.mode) ? humanize(provider.mode) : null),
    ]);
    const copy = createElement("div", { className: "connection-card-copy" }, [
      createElement("h3", { text: provider.label || humanize(id) }),
      createElement("span", { className: "chip status-neutral", text: humanize(provider.mode || "manual_only") }),
      createElement("p", { text: provider.reason || provider.description || "Capability information supplied by this deployment." }),
    ]);
    card.append(header, copy);
    if (["connected", "active"].includes(status)) {
      const scopes = Array.isArray(connection.scopes) ? connection.scopes.join(", ") : "";
      const gmailSource = ["gmail", "google"].includes(id) ? googleOauthConnectedSource() : null;
      const details = createElement("div", { className: "connection-details" }, [
        createElement("strong", { text: connection.display_name || connection.external_account_id || "Connected account" }),
        ...(gmailSource ? [createElement("span", { text: gmailSource === "user" ? "OAuth app: Your Google Cloud project" : "OAuth app: AutoApply managed" })] : []),
        ...(scopes ? [createElement("span", { text: `Scopes: ${scopes}` })] : []),
        createElement("span", { text: `Checked: ${formatDate(connection.last_verified_at || connection.updated_at)}` }),
      ]);
      card.append(details);
    }
    const actions = createElement("div", { className: "card-actions" });
    if (id === "gmail" || id === "google") {
      if (["connected", "active"].includes(status)) {
        const disconnect = createElement("button", { className: "button button-danger-quiet button-small", text: "Disconnect Gmail", type: "button" });
        disconnect.addEventListener("click", () => disconnectProvider("gmail", disconnect));
        actions.append(disconnect);
      } else {
        const setupAvailable = state.googleOauthClient?.platform_available === true
          || state.googleOauthClient?.byoc_available === true;
        const connect = createElement("button", {
          className: "button button-primary button-small",
          text: setupAvailable ? "Choose connection method" : "Gmail setup unavailable",
          type: "button",
        });
        connect.disabled = !setupAvailable;
        connect.addEventListener("click", focusGoogleOauthSetup);
        actions.append(connect);
      }
    } else if (provider.mode === "managed_browser") {
      if (["connected", "active"].includes(status)) {
        const disconnect = createElement("button", { className: "button button-danger-quiet button-small", text: id === "yc" ? "Disconnect YC" : "Delete connection", type: "button" });
        disconnect.addEventListener("click", () => disconnectProvider(id, disconnect));
        actions.append(disconnect);
        const check = createElement("button", { className: "button button-ghost button-small", text: id === "yc" ? "Check YC session" : "Queue health check", type: "button" });
        check.addEventListener("click", () => queueConnectionCheck(id, check));
        actions.append(check);
        if (id === "yc") {
          const use = createElement("button", { className: "button button-primary button-small", text: "Open YC application desk", type: "button" });
          use.addEventListener("click", () => {
            switchView("jobs");
            window.setTimeout(() => byId("yc-application-desk")?.scrollIntoView({ behavior: "smooth", block: "start" }), 200);
          });
          actions.append(use);
        }
      } else if (provider.can_connect === false) {
        const use = createElement("button", { className: "button button-ghost button-small", text: provider.available === false ? "Not enabled" : "No login required", type: "button" });
        use.disabled = provider.available === false;
        use.addEventListener("click", () => switchView("jobs"));
        actions.append(use);
      } else if (status === "pending") {
        const complete = createElement("button", { className: "button button-primary button-small", text: id === "yc" ? "I completed YC login" : "I completed login", type: "button" });
        complete.addEventListener("click", () => completeBrowserConnection(id, complete));
        const restart = createElement("button", { className: "button button-ghost button-small", text: "Open a new login view", type: "button" });
        restart.addEventListener("click", () => startBrowserConnection(id, restart));
        actions.append(complete, restart);
      } else if (status === "needs_attention" && connection) {
        const restart = createElement("button", { className: "button button-ghost button-small", text: "Open a new login view", type: "button" });
        restart.addEventListener("click", () => startBrowserConnection(id, restart));
        const disconnect = createElement("button", { className: "button button-danger-quiet button-small", text: "Delete saved browser state", type: "button" });
        disconnect.addEventListener("click", () => disconnectProvider(id, disconnect));
        actions.append(restart, disconnect);
      } else {
        const connect = createElement("button", { className: "button button-primary button-small", text: id === "yc" ? "Connect YC" : "Open secure login", type: "button" });
        connect.disabled = provider.can_connect === false || provider.available === false;
        connect.addEventListener("click", () => startBrowserConnection(id, connect));
        actions.append(connect);
      }
    } else if (id === "linkedin") {
      const profileUrl = state.profile.linkedin_url;
      const manual = createElement("button", { className: "button button-ghost button-small", text: profileUrl ? "Open LinkedIn profile" : "Add profile URL", type: "button" });
      manual.addEventListener("click", () => profileUrl ? openExternal(profileUrl) : switchView("profile"));
      actions.append(manual);
    } else if (provider.mode === "public_ats" || provider.mode === "manual_only") {
      const use = createElement("button", { className: "button button-ghost button-small", text: "Use with a saved job", type: "button" });
      use.addEventListener("click", () => switchView("jobs"));
      actions.append(use);
    }
    card.append(actions);
    container.append(card);
  }
}

async function startGmailOAuth(credentialSource, button) {
  if (!["platform", "user"].includes(credentialSource)) return;
  await withBusy(button, "Opening Google…", async () => {
    try {
      const payload = await apiRequest(
        "/oauth/google/start?return_path=%2F%3Fview%3Dconnections",
        { method: "POST", body: { credential_source: credentialSource } },
      );
      const oauthStart = unwrapData(payload) || {};
      const url = safeGoogleAuthorizationUrl(oauthStart.authorization_url);
      if (!url) throw new AppError("The server did not return a valid Google authorization URL.", "oauth_url_missing");
      window.location.assign(url);
    } catch (error) {
      toast(errorMessage(error, "Gmail connection could not be started."), "error");
    }
  });
}

async function saveGoogleOauthClient(event) {
  event.preventDefault();
  const button = event.submitter;
  const clientId = byId("gmail-client-id").value.trim();
  const clientSecretInput = byId("gmail-client-secret");
  const clientSecret = clientSecretInput.value.trim();
  setFormMessage("gmail-oauth-client-message");
  if (!clientId.endsWith(".apps.googleusercontent.com")) {
    setFormMessage("gmail-oauth-client-message", "Enter the Web application Client ID ending in .apps.googleusercontent.com.", "error");
    byId("gmail-client-id").focus();
    return;
  }
  if (clientSecret.length < 8 || /\s/.test(clientSecret)) {
    setFormMessage("gmail-oauth-client-message", "Enter the complete OAuth Client Secret from Google Cloud.", "error");
    clientSecretInput.focus();
    return;
  }
  await withBusy(button, state.googleOauthClient?.configured ? "Replacing…" : "Saving…", async () => {
    try {
      await apiRequest("/connections/google-oauth-client", {
        method: "PUT",
        body: { client_id: clientId, client_secret: clientSecret },
      });
      byId("gmail-oauth-client-form").reset();
      state.googleOauthEditing = false;
      state.googleOauthMode = "user";
      await loadConnections(true);
      setFormMessage("gmail-oauth-client-message", "OAuth app saved. You can now continue to Google.", "success");
      toast("Your Google OAuth app was saved securely.", "success");
    } catch (error) {
      setFormMessage("gmail-oauth-client-message", errorMessage(error, "The OAuth app could not be saved."), "error");
    } finally {
      clientSecretInput.value = "";
    }
  });
}

function beginGoogleOauthClientReplacement() {
  if (isGmailConnected()) {
    toast("Disconnect Gmail before replacing the OAuth app that issued its token.", "error");
    return;
  }
  state.googleOauthEditing = true;
  byId("gmail-oauth-client-form").reset();
  setFormMessage("gmail-oauth-client-message", "Enter both the replacement Client ID and Client Secret.");
  renderGoogleOauthSetup();
  byId("gmail-client-id").focus();
}

function cancelGoogleOauthClientReplacement() {
  state.googleOauthEditing = false;
  byId("gmail-oauth-client-form").reset();
  setFormMessage("gmail-oauth-client-message");
  renderGoogleOauthSetup();
  byId("gmail-replace-client").focus();
}

async function deleteGoogleOauthClient(button) {
  if (isGmailConnected()) {
    toast("Disconnect Gmail before deleting the OAuth app that issued its token.", "error");
    return;
  }
  if (!await confirmAction({
    eyebrow: "Gmail connection",
    title: "Remove Google OAuth credentials?",
    message: "The saved Client ID and Client Secret will be deleted. You will need to enter both again before reconnecting Gmail.",
    confirmLabel: "Remove credentials",
    cancelLabel: "Keep credentials",
    tone: "danger",
    ticketLabel: "Stored secret",
    symbol: "G",
  })) return;
  await withBusy(button, "Deleting…", async () => {
    try {
      await apiRequest("/connections/google-oauth-client", { method: "DELETE" });
      state.googleOauthEditing = false;
      state.googleOauthMode = state.googleOauthClient?.platform_available ? "platform" : "user";
      byId("gmail-oauth-client-form").reset();
      await loadConnections(true);
      setFormMessage("gmail-oauth-client-message", "Your saved Google OAuth credentials were deleted.", "success");
      toast("Google OAuth credentials deleted.", "success");
    } catch (error) {
      setFormMessage("gmail-oauth-client-message", errorMessage(error, "The OAuth credentials could not be deleted."), "error");
    }
  });
}

async function copyGoogleCallbackUri(button) {
  const input = byId("gmail-callback-uri");
  const value = safeHttpUrl(input.value);
  if (!value) {
    setFormMessage("gmail-oauth-client-message", "The deployment has not published a callback URL yet.", "error");
    return;
  }
  await withBusy(button, "Copying…", async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(value);
      setFormMessage("gmail-oauth-client-message", "Return address copied. Paste it into Google Cloud exactly as shown.", "success");
      announce("Google return address copied.");
    } catch {
      input.focus();
      input.select();
      setFormMessage("gmail-oauth-client-message", "Copy the selected return address manually.");
    }
  });
}

async function startBrowserConnection(provider, button) {
  const popup = window.open("about:blank", "_blank");
  if (popup) popup.opener = null;
  await withBusy(button, "Starting…", async () => {
    try {
      const payload = await apiRequest(`/connections/${encodeURIComponent(provider)}/browser/start`, { method: "POST" });
      const url = safeHttpUrl(payload?.live_view_url || unwrapData(payload)?.live_view_url);
      if (!url) throw new AppError("The managed browser did not return a valid Live View URL.", "live_view_missing");
      if (popup) popup.location.replace(url);
      else openExternal(url);
      await loadConnections(true);
      toast("Complete login and MFA inside the Live View, then save and close the session here. Automatic provider-login verification is not enabled.", "info", "Secure login opened", 9_000);
    } catch (error) {
      if (popup) popup.close();
      toast(errorMessage(error, "The managed browser login could not be started."), "error");
    }
  });
}

async function completeBrowserConnection(provider, button) {
  await withBusy(button, "Checking…", async () => {
    try {
      await apiRequest(`/connections/${encodeURIComponent(provider)}/browser/complete`, { method: "POST" });
      await loadConnections(true);
      toast(
        provider === "yc"
          ? "YC browser state was saved. Each exact-job preparation still verifies the expected YC route and stops on login, MFA, or unexpected fields."
          : `${humanize(provider)} browser state was saved for manual use. The provider login remains unverified.`,
        "info",
      );
    } catch (error) {
      toast(errorMessage(error, "The provider login could not be confirmed yet."), "error");
    }
  });
}

async function disconnectProvider(provider, button) {
  const providerName = humanize(provider);
  if (!await confirmAction({
    eyebrow: "Connected service",
    title: `Disconnect ${providerName}?`,
    message: `AutoApply will delete the stored ${providerName} authorization and browser-context metadata.`,
    confirmLabel: "Disconnect",
    cancelLabel: "Keep connected",
    tone: "danger",
    ticketLabel: "Connection",
    symbol: "⌁",
  })) return;
  await withBusy(button, "Disconnecting…", async () => {
    try {
      const payload = await apiRequest(`/connections/${encodeURIComponent(provider)}`, { method: "DELETE" });
      const disconnectResult = unwrapData(payload) || {};
      const isGmail = ["gmail", "google"].includes(String(provider).toLowerCase());
      const googleRevoked = disconnectResult.revoked === true;
      if (isGmail) {
        setGmailRevocationWarning(!googleRevoked);
        renderGmailRevocationWarning();
        setFormMessage("gmail-oauth-client-message");
      }
      await loadConnections(true);
      if (isGmail && !googleRevoked) {
        toast(
          "Gmail is disconnected locally, but Google did not confirm revocation. Follow the Google Account instructions shown on this page.",
          "info",
          "Google revocation still required",
          0,
        );
      } else {
        toast(`${humanize(provider)} disconnected.`, "success");
      }
    } catch (error) {
      toast(errorMessage(error, "The provider could not be disconnected."), "error");
    }
  });
}

function showOAuthResult() {
  const params = new URL(window.location.href).searchParams;
  const oauth = params.get("oauth");
  const result = oauth || params.get("connection");
  const error = params.get("oauth_error") || (oauth === "error" ? params.get("code") || "oauth_error" : null);
  const banner = byId("oauth-result-banner");
  if (!result && !error) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  banner.className = `notice ${error ? "notice-danger" : "notice-success"}`;
  const oauthErrors = {
    google_oauth_client_not_configured: "That Google OAuth app is no longer saved. Review the Gmail setup and try again.",
    google_oauth_client_stale: "The Google OAuth app changed while connection was in progress. Start a new Gmail connection.",
    google_oauth_client_changed: "The Google OAuth app changed while connection was in progress. Start a new Gmail connection.",
    oauth_exchange_failed: "Google could not exchange the authorization code. Check the Client ID, Client Secret, and exact return address, then try again.",
    google_scope_missing: "Google did not grant Gmail send access. Start again and approve the requested send-email permission.",
    oauth_authorization_denied: "Google authorization was cancelled. No Gmail connection was saved.",
    oauth_state_invalid: "This Google connection link expired or was already used. Start a new Gmail connection.",
  };
  banner.textContent = error
    ? oauthErrors[error] || "The provider connection did not complete. Try again or review the Gmail setup."
    : "Gmail connected. Confirm the account and connection method shown below.";
  if (state.currentView !== "connections") switchView("connections", false);
}

async function loadAutomationJobs(quiet = false, identity = identitySnapshot()) {
  const container = byId("automation-list");
  if (!quiet) showLoading(container);
  const payload = await apiRequest("/automation-jobs?limit=100", { identity });
  state.automationJobs = unwrapItems(payload, ["automation_jobs", "jobs"]);
  const fetchedSubmissions = new Map();
  for (const job of state.automationJobs) {
    if (job.kind !== "application_submit") continue;
    const revisionId = job.payload?.form_revision_id || job.result?.form_revision_id;
    if (revisionId && !fetchedSubmissions.has(revisionId)) fetchedSubmissions.set(revisionId, job);
  }
  for (const [revisionId, job] of fetchedSubmissions) state.formSubmissionJobs.set(revisionId, job);
  renderAutomationJobs();
  renderOverview();
  const selectedRevision = latestFormRevision(byId("form-application-id")?.value || "");
  const selectedSubmission = selectedRevision ? formSubmissionJobForRevision(selectedRevision) : null;
  if (selectedRevision && selectedSubmission) renderFormSubmissionJob(selectedRevision, selectedSubmission);
  const selectedFormApplicationId = byId("form-application-id")?.value || state.selectedFormApplicationId || "";
  if (selectedFormApplicationId && !selectedRevision) renderFormRevision(null);
  renderYcDesk();
  return state.automationJobs;
}

function progressSummary(job) {
  const progress = job.progress;
  if (!progress) return "";
  if (typeof progress === "string") return progress;
  if (typeof progress !== "object") return String(progress);
  if (progress.message) return String(progress.message);
  const safe = Object.entries(progress)
    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
    .slice(0, 6)
    .map(([key, value]) => `${humanize(key)}: ${String(value)}`);
  return safe.join("\n");
}

function renderAutomationJobs() {
  const container = byId("automation-list");
  clearNode(container);
  if (!state.automationJobs.length) {
    container.append(emptyState("No durable work yet", "Permitted queued operations and manual handoffs will appear here.", "↻"));
    return;
  }
  for (const job of state.automationJobs) {
    const status = job.status || "queued";
    const item = createElement("article", { className: `timeline-item status-${status}` });
    const header = createElement("div", { className: "timeline-header" }, [
      createElement("div", {}, [
        createElement("h3", { text: humanize(job.kind || "automation job") }),
        createElement("p", { text: `${job.provider ? `${humanize(job.provider)} · ` : ""}${formatDate(job.created_at, true)}` }),
      ]),
      makeStatus(status),
    ]);
    item.append(header);
    const summary = progressSummary(job) || job.error_message || (job.result?.message ? String(job.result.message) : "");
    if (summary) item.append(createElement("div", { className: "timeline-progress", text: summary.slice(0, 1_500) }));
    const meta = createElement("div", { className: "meta-row" }, [
      createElement("span", { text: `Attempts ${Number(job.attempts || 0)}/${Number(job.max_attempts || 3)}` }),
      ...(job.updated_at ? [createElement("span", { text: `Updated ${formatDate(job.updated_at, true)}` })] : []),
    ]);
    item.append(meta);
    const actions = createElement("div", { className: "card-actions" });
    const liveViewUrl = safeBrowserbaseLiveViewUrl(job.result?.live_view_url);
    if (liveViewUrl) {
      actions.append(createElement("a", {
        className: "button button-accent button-small",
        text: "Open live review",
        attrs: { href: liveViewUrl, target: "_blank", rel: "noopener noreferrer" },
      }));
    }
    if (["queued", "running"].includes(status) && !job.cancel_requested_at) {
      const cancel = createElement("button", { className: "button button-danger-quiet button-small", text: "Request cancellation", type: "button" });
      cancel.addEventListener("click", () => cancelAutomationJob(job, cancel));
      actions.append(cancel);
    }
    if (actions.childElementCount) item.append(actions);
    container.append(item);
  }
}

async function createAutomationJob(body, button, busyLabel = "Queueing…") {
  return withBusy(button, busyLabel, async () => {
    const payload = await apiRequest("/automation-jobs", { method: "POST", body });
    await loadAutomationJobs(true);
    renderOverview();
    return unwrapData(payload);
  });
}

async function queueManualHandoff(application, button) {
  try {
    await createAutomationJob({
      kind: "manual_handoff",
      provider: "external_job_board",
      application_id: application.id,
      payload: {},
      idempotency_key: `manual-handoff-${application.id}`,
    }, button);
    toast("Manual handoff added to durable activity.", "success");
  } catch (error) {
    toast(errorMessage(error, "The handoff could not be queued."), "error");
  }
}

async function queueConnectionCheck(provider, button) {
  try {
    await createAutomationJob({
      kind: "connection_check",
      provider,
      application_id: null,
      payload: {},
      idempotency_key: `connection-check-${provider}-${Date.now()}`,
    }, button);
    toast("Connection check queued.", "success");
  } catch (error) {
    toast(errorMessage(error, "The connection check could not be queued."), "error");
  }
}

async function cancelAutomationJob(job, button) {
  await withBusy(button, "Requesting…", async () => {
    try {
      await apiRequest(`/automation-jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST" });
      await loadAutomationJobs(true);
      toast("Cancellation requested. A running worker will stop cooperatively.", "success");
    } catch (error) {
      toast(errorMessage(error, "Cancellation could not be requested."), "error");
    }
  });
}

function startAutomationPolling() {
  stopAutomationPolling();
  if (!byId("automation-auto-refresh").checked) return;
  state.automationTimer = window.setInterval(() => {
    if (state.session && state.currentView === "automation" && document.visibilityState === "visible") {
      loadAutomationJobs(true).catch(() => {});
    }
  }, 8_000);
}

function stopAutomationPolling() {
  if (state.automationTimer) window.clearInterval(state.automationTimer);
  state.automationTimer = null;
}

function renderJobIntelligence() {
  if (!byId("job-fit-summary")) return;
  const groqReady = credentialConfigured("groq");
  const activeResume = state.resumes.find((resume) => resume.is_active !== false) || state.resumes[0] || null;
  const resumeReady = activeResume?.parse_status === "parsed";
  const profileReady = Boolean(state.profile?.onboarding_completed);
  const scored = state.jobs.filter((job) => job.fit?.evaluated && Number.isFinite(job.fit?.score));
  const suggestionRoles = Array.isArray(state.resumeSuggestions?.target_roles)
    ? state.resumeSuggestions.target_roles
    : [];
  const savedRoles = Array.isArray(state.profile?.preferences?.target_roles)
    ? state.profile.preferences.target_roles
    : [];
  const inferredRoles = Array.isArray(state.fitSummary?.recommended_roles)
    ? state.fitSummary.recommended_roles
    : [];
  const roles = [...suggestionRoles, ...savedRoles, ...inferredRoles]
    .filter((role, index, allRoles) => typeof role === "string" && role.trim()
      && allRoles.findIndex((item) => String(item).trim().toLowerCase() === role.trim().toLowerCase()) === index)
    .slice(0, 5);

  const roleList = byId("job-role-directions");
  clearNode(roleList);
  roles.forEach((role) => roleList.append(createElement("span", { className: "role-direction", text: role })));

  if (!groqReady) {
    setText("job-fit-summary", "Add your free Groq key here first; it unlocks full résumé extraction and grounded drafts.");
  } else if (!activeResume) {
    setText("job-fit-summary", "Groq is ready. Upload a résumé next so AutoApply can build your applicant profile and compare roles.");
  } else if (!resumeReady) {
    setText("job-fit-summary", "Your résumé is uploaded but not parsed yet. Parse it to activate profile extraction and fit scoring.");
  } else if (!state.jobs.length) {
    setText("job-fit-summary", "Your résumé evidence is ready. Add or discover jobs to see the strongest alignments first.");
  } else if (scored.length) {
    const top = Math.max(...scored.map((job) => job.fit.score));
    setText("job-fit-summary", `${scored.length} saved job${scored.length === 1 ? " is" : "s are"} ranked by résumé evidence. Best current alignment: ${top}%. This is a relevance guide, not a hiring prediction.`);
  } else {
    setText("job-fit-summary", "Save the résumé-derived profile, then refresh jobs to rank them against your evidence.");
  }

  const setGate = (statusId, ready, readyText, missingText, buttonId, readyButton, missingButton) => {
    setText(statusId, ready ? readyText : missingText);
    setText(buttonId, ready ? readyButton : missingButton);
    byId(statusId)?.closest(".kit-gate")?.classList.toggle("is-ready", ready);
  };
  setGate("jobs-groq-status", groqReady, "Connected to account", "Not connected", "jobs-open-groq", "Manage", "Add key");
  setGate(
    "jobs-resume-status",
    resumeReady,
    activeResume?.original_name || "Parsed and ready",
    activeResume ? "Uploaded · parse required" : "Not uploaded",
    "jobs-open-resume",
    "Manage",
    activeResume ? "Parse" : "Upload",
  );
  setGate("jobs-profile-status", profileReady, "Saved and complete", "Review résumé-filled facts", "jobs-open-profile", "Review", "Complete");
}

function renderOverview() {
  if (!byId("workspace") || byId("workspace").hidden) return;
  const activeResumes = state.resumes.filter((resume) => resume.is_active !== false).length;
  const connected = mergedProviders().filter((provider) => ["connected", "active"].includes(connectionStatus(provider))).length;
  setText("stat-resumes", activeResumes ? "1" : "0");
  const visibleJobCount = state.jobs.filter((job) => job.status !== "archived" && !job.archived_at).length.toLocaleString();
  setText("stat-jobs", `${visibleJobCount}${state.jobsHasMore ? "+" : ""}`);
  setText("stat-applications", state.applications.length.toLocaleString());
  setText("stat-connections", connected.toLocaleString());

  const banner = byId("onboarding-banner");
  banner.hidden = Boolean(state.profile.onboarding_completed);
  if (!banner.hidden) {
    const parts = [];
    if (!state.profile.full_name || !state.profile.summary) parts.push("complete your core applicant facts");
    if (!activeResumes) parts.push("upload a PDF résumé");
    setText("onboarding-copy", parts.length ? `Next: ${parts.join(" and ")}.` : "Review the profile and mark onboarding complete.");
  }

  const recent = byId("overview-jobs-list");
  clearNode(recent);
  const recentJobs = [...state.jobs].sort((a, b) => new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0)).slice(0, 5);
  if (!recentJobs.length) recent.append(emptyState("No opportunities yet", "Add a role and its job description to begin.", "◇"));
  else {
    for (const job of recentJobs) {
      const item = createElement("div", { className: "compact-list-item" }, [
        createElement("span", {}, [createElement("strong", { text: job.title || "Untitled role" }), createElement("small", { text: job.company || "Unknown company" })]),
        createElement("span", {}, [createElement("strong", { text: job.location || "Location not set" }), createElement("small", { text: formatDate(job.updated_at || job.created_at) })]),
        makeStatus(job.status || "saved"),
      ]);
      recent.append(item);
    }
  }

  const readiness = byId("readiness-list");
  clearNode(readiness);
  const readinessItems = [
    [Boolean(state.profile.onboarding_completed), "Profile completed", "Profile needs review"],
    [activeResumes > 0, "Résumé uploaded", "Upload a PDF résumé"],
    [credentialConfigured("groq"), "Groq credential connected", "Connect a Groq key"],
    [isGmailConnected(), "Gmail connected", "Connect Gmail when ready"],
  ];
  for (const [ready, yes, no] of readinessItems) {
    readiness.append(createElement("li", { className: `readiness-item${ready ? "" : " is-missing"}` }, [
      createElement("span", { className: "readiness-dot", text: ready ? "✓" : "○", attrs: { "aria-hidden": "true" } }),
      createElement("span", { text: ready ? yes : no }),
    ]));
  }

  const activity = byId("overview-automation-list");
  clearNode(activity);
  const recentWork = [...state.automationJobs].sort((a, b) => new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0)).slice(0, 3);
  if (!recentWork.length) activity.append(emptyState("No durable activity", "Queued work will remain visible here even after you close the page.", "↻"));
  else {
    for (const job of recentWork) {
      activity.append(createElement("article", { className: "activity-card" }, [
        makeStatus(job.status || "queued"),
        createElement("strong", { text: humanize(job.kind || "automation job") }),
        createElement("small", { text: formatDate(job.updated_at || job.created_at, true) }),
      ]));
    }
  }
}

function bindAuthEvents() {
  all("[data-auth-mode]").forEach((button) => button.addEventListener("click", () => setAuthMode(button.dataset.authMode)));
  byId("show-reset").addEventListener("click", () => showResetForm(true));
  byId("hide-reset").addEventListener("click", () => showResetForm(false));

  byId("google-signin").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    setFormMessage("auth-message");
    await withBusy(button, "Opening Google…", async () => {
      try {
        const result = await state.supabase.auth.signInWithOAuth({
          provider: "google",
          options: {
            redirectTo: new URL("/", window.location.origin).href,
            scopes: "openid email profile",
          },
        });
        if (result.error) setFormMessage("auth-message", googleAuthProviderError(result.error), "error");
      } catch (error) {
        setFormMessage("auth-message", googleAuthProviderError(error), "error");
      }
    });
  });

  byId("signin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    setFormMessage("auth-message");
    await withBusy(button, "Signing in…", async () => {
      let attempted = false;
      try {
        const captchaToken = requireCaptchaToken();
        attempted = true;
        const result = await state.supabase.auth.signInWithPassword({
          email: byId("signin-email").value.trim(),
          password: byId("signin-password").value,
          ...(captchaToken ? { options: { captchaToken } } : {}),
        });
        if (result.error) setFormMessage("auth-message", authProviderError(result.error), "error");
        else if (result.data.session) await showWorkspace(result.data.session);
      } catch (error) {
        setFormMessage("auth-message", errorMessage(error, "Sign-in is temporarily unavailable."), "error");
      } finally {
        if (attempted) resetCaptcha();
      }
    });
  });

  byId("signup-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    setFormMessage("auth-message");
    await withBusy(button, "Creating…", async () => {
      let attempted = false;
      try {
        const captchaToken = requireCaptchaToken();
        attempted = true;
        const redirect = new URL("/", window.location.origin).href;
        const result = await state.supabase.auth.signUp({
          email: byId("signup-email").value.trim(),
          password: byId("signup-password").value,
          options: { emailRedirectTo: redirect, ...(captchaToken ? { captchaToken } : {}) },
        });
        if (result.error) setFormMessage("auth-message", authProviderError(result.error), "error");
        else if (result.data.session) await showWorkspace(result.data.session);
        else setFormMessage("auth-message", "Check your email to confirm the account, then return here to sign in.", "success");
      } catch (error) {
        setFormMessage("auth-message", errorMessage(error, "Account creation is temporarily unavailable."), "error");
      } finally {
        if (attempted) resetCaptcha();
      }
    });
  });

  byId("reset-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    setFormMessage("auth-message");
    await withBusy(button, "Sending…", async () => {
      let attempted = false;
      try {
        const captchaToken = requireCaptchaToken();
        attempted = true;
        const redirect = new URL("/?recovery=1", window.location.origin).href;
        const result = await state.supabase.auth.resetPasswordForEmail(byId("reset-email").value.trim(), {
          redirectTo: redirect,
          ...(captchaToken ? { captchaToken } : {}),
        });
        if (result.error) setFormMessage("auth-message", authProviderError(result.error), "error");
        else setFormMessage("auth-message", "If that account exists, a password-reset email is on its way.", "success");
      } catch (error) {
        setFormMessage("auth-message", errorMessage(error, "A reset email could not be requested right now."), "error");
      } finally {
        if (attempted) resetCaptcha();
      }
    });
  });

  byId("recovery-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    setFormMessage("auth-message");
    await withBusy(button, "Updating…", async () => {
      try {
        const result = await state.supabase.auth.updateUser({ password: byId("recovery-password").value });
        if (result.error) setFormMessage("auth-message", result.error.message, "error");
        else {
          showRecoveryForm(false);
          toast("Password updated. Your workspace is ready.", "success");
          const session = (await state.supabase.auth.getSession()).data.session;
          if (session) await showWorkspace(session);
        }
      } catch (error) {
        setFormMessage("auth-message", errorMessage(error, "The password could not be updated right now."), "error");
      }
    });
  });
}

function bindWorkspaceEvents() {
  all("[data-view]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  all("[data-view-target]").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.viewTarget)));
  all("[data-mass-email-view]").forEach((button) => {
    button.addEventListener("click", () => switchView(button.dataset.massEmailView));
    button.addEventListener("keydown", (event) => {
      const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
      if (!keys.includes(event.key)) return;
      event.preventDefault();
      const targetView = event.key === "ArrowLeft" || event.key === "Home" ? "outreach" : "applications";
      switchView(targetView);
      requestAnimationFrame(() => {
        const target = all(`[data-mass-email-view="${targetView}"]`).find(
          (candidate) => !candidate.closest("[data-view-panel]")?.hidden,
        );
        target?.focus();
      });
    });
  });
  byId("menu-toggle").addEventListener("click", () => byId("workspace-sidebar").classList.contains("is-open") ? closeMobileMenu() : openMobileMenu());
  byId("mobile-backdrop").addEventListener("click", closeMobileMenu);
  byId("sidebar-signout").addEventListener("click", signOut);
  byId("account-signout").addEventListener("click", signOut);
  byId("overview-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, "Refreshing…", loadWorkspace));

  byId("profile-form").addEventListener("submit", saveProfile);
  byId("profile-reload").addEventListener("click", () => loadProfile().catch((error) => toast(errorMessage(error), "error")));
  byId("profile-from-resume").addEventListener("click", (event) => fillProfileFromResume(event.currentTarget));
  all("#profile-form input, #profile-form textarea").forEach((input) => input.addEventListener("input", renderProfileCompleteness));
  byId("settings-form").addEventListener("submit", saveSettings);
  byId("account-delete-form").addEventListener("submit", deleteAccount);
  for (const input of all("#account-delete-form input")) input.addEventListener("input", updateAccountDeleteButton);
  byId("account-deletion-retry-form").addEventListener("submit", retryAccountDeletion);
  byId("account-deletion-confirmation").addEventListener("input", updateAccountDeletionRetryButton);
  byId("account-deletion-signout").addEventListener("click", signOut);

  byId("resume-upload-form").addEventListener("submit", uploadResume);
  byId("resume-file").addEventListener("change", (event) => {
    state.pendingResumeFile = event.target.files?.[0] || null;
    setText("resume-file-label", state.pendingResumeFile ? `${state.pendingResumeFile.name} · ${formatBytes(state.pendingResumeFile.size)}` : "or drop it here");
  });
  const uploadZone = byId("resume-upload-form");
  for (const eventName of ["dragenter", "dragover"]) {
    uploadZone.addEventListener(eventName, (event) => { event.preventDefault(); uploadZone.classList.add("is-dragging"); });
  }
  for (const eventName of ["dragleave", "drop"]) {
    uploadZone.addEventListener(eventName, (event) => { event.preventDefault(); uploadZone.classList.remove("is-dragging"); });
  }
  uploadZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0] || null;
    state.pendingResumeFile = file;
    setText("resume-file-label", file ? `${file.name} · ${formatBytes(file.size)}` : "or drop it here");
  });
  byId("apply-suggestions").addEventListener("click", () => applyResumeSuggestions());

  all("[data-provider-credential]").forEach((form) => form.addEventListener("submit", saveProviderCredential));
  all("[data-credential-edit]").forEach((button) => button.addEventListener("click", () => editProviderCredential(button.dataset.credentialEdit)));
  all("[data-credential-cancel]").forEach((button) => button.addEventListener("click", () => cancelProviderCredentialEdit(button.dataset.credentialCancel)));
  all("[data-credential-delete]").forEach((button) => button.addEventListener("click", () => deleteProviderCredential(button.dataset.credentialDelete, button)));
  byId("delete-groq").addEventListener("click", (event) => deleteProviderCredential("groq", event.currentTarget));
  byId("manage-groq-credential").addEventListener("click", () => focusProviderCredential("groq"));

  byId("job-form").addEventListener("submit", saveJob);
  byId("job-cancel-edit").addEventListener("click", resetJobForm);
  byId("yc-job-form").addEventListener("submit", saveAndPrepareYcJob);
  byId("yc-job-url").addEventListener("change", inferYcFieldsFromUrl);
  byId("yc-job-url").addEventListener("blur", inferYcFieldsFromUrl);
  byId("yc-preferences-form").addEventListener("submit", saveYcPreferences);
  byId("yc-connect").addEventListener("click", (event) => startBrowserConnection("yc", event.currentTarget));
  byId("yc-complete-login").addEventListener("click", (event) => completeBrowserConnection("yc", event.currentTarget));
  byId("yc-open-connections").addEventListener("click", openYcConnections);
  byId("job-search").addEventListener("input", renderJobs);
  byId("job-status-filter").addEventListener("change", renderJobs);
  byId("job-sort").addEventListener("change", renderJobs);
  byId("jobs-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, "…", () => loadJobs()));
  byId("jobs-load-more").addEventListener("click", (event) => withBusy(
    event.currentTarget,
    "Loading…",
    () => loadJobs(true, identitySnapshot(), true),
  ));
  byId("jobs-open-groq").addEventListener("click", () => {
    switchView("profile");
    requestAnimationFrame(() => byId("groq-key")?.focus());
  });
  byId("jobs-open-resume").addEventListener("click", async (event) => {
    const active = state.resumes.find((resume) => resume.is_active !== false) || state.resumes[0];
    if (active && active.parse_status !== "parsed") {
      await parseResume(active.id, event.currentTarget);
      return;
    }
    switchView("profile");
    requestAnimationFrame(() => byId("resume-file")?.focus());
  });
  byId("jobs-open-profile").addEventListener("click", () => switchView("profile"));

  byId("resume-discovery-form").addEventListener("submit", submitResumeGuidedDiscovery);
  byId("job-import-form").addEventListener("submit", importJobFile);
  byId("job-import-file").addEventListener("change", (event) => {
    state.pendingJobImportFile = event.target.files?.[0] || null;
    setText("job-import-file-label", state.pendingJobImportFile ? `${state.pendingJobImportFile.name} · ${formatBytes(state.pendingJobImportFile.size)}` : "Title and company are required; common header names are accepted.");
  });
  byId("ats-link-form").addEventListener("submit", ingestAtsLinks);
  byId("ats-board-form").addEventListener("submit", queueAtsBoardDiscovery);
  all("[data-form-intake-mode]").forEach((button) => {
    button.addEventListener("click", () => setFormIntakeMode(button.dataset.formIntakeMode));
  });
  byId("referral-ingest-form").addEventListener("submit", ingestReferralDigest);
  byId("google-form-intake-form").addEventListener("submit", addGoogleFormToPilot);
  byId("discovery-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, "Refreshing…", async () => {
    await Promise.all([loadJobs(), loadGoogleForms(true)]);
  }));
  byId("google-form-queue-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, "Refreshing…", () => loadGoogleForms()));
  byId("form-pilot-review-jump").addEventListener("click", async () => {
    const application = formApplicationById(state.selectedFormApplicationId) || state.formApplications[0] || null;
    if (!application?.id) {
      toast("Prepare a Google Form or exact provider application first; its captured questions will then appear here.", "info");
      byId("google-form-queue")?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    await openFormApplicationReview(application.id);
  });
  byId("retry-form-scan").addEventListener("click", async (event) => {
    const application = formApplicationById(state.selectedFormApplicationId);
    const job = application ? jobForApplication(application) : null;
    if (!job?.id) {
      toast("This form is no longer linked to a saved job. Add its URL again from the Form Pilot inbox.", "error");
      return;
    }
    await scanJobApplication(job, providerForJob(job) || "google_forms", event.currentTarget);
  });

  byId("hunter-delete").addEventListener("click", (event) => deleteProviderCredential("hunter", event.currentTarget));
  byId("manage-hunter-credential").addEventListener("click", () => focusProviderCredential("hunter"));
  byId("outreach-find-contacts").addEventListener("click", (event) => findOutreachContacts(event.currentTarget));
  byId("outreach-create-drafts").addEventListener("click", (event) => createOutreachDrafts(event.currentTarget));
  byId("outreach-send-approved").addEventListener("click", (event) => sendApprovedOutreach(event.currentTarget));

  byId("application-status-filter").addEventListener("change", renderApplications);
  byId("applications-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, "…", () => loadApplications()));
  byId("application-editor").addEventListener("submit", saveApplication);
  for (const id of ["application-recipient", "application-subject", "application-body"]) {
    byId(id).addEventListener("input", () => {
      if (id === "application-body") updateApplicationCharacterCount();
      markApplicationDirty();
    });
  }
  byId("approve-application").addEventListener("click", (event) => approveApplication(event.currentTarget));
  byId("send-application").addEventListener("click", (event) => sendApplication(event.currentTarget));
  byId("clear-application").addEventListener("click", () => clearApplicationEditor(true));
  byId("submit-form-revision").addEventListener("click", (event) => approveAndSubmitFormRevision(event.currentTarget));
  byId("form-mark-submitted").addEventListener("click", (event) => resolveFormSubmissionOutcome("submitted", event.currentTarget));
  byId("form-prepare-submission-retry").addEventListener("click", (event) => resolveFormSubmissionOutcome("not_submitted", event.currentTarget));
  for (const eventName of ["input", "change"]) {
    byId("form-revision-answers").addEventListener(eventName, () => {
      const current = latestFormRevision(byId("form-application-id").value);
      if (!current?.id || current.approved_at || current.status === "approved" || formSubmissionJobForRevision(current)) return;
      refreshFormSubmitPreflight({ markFields: true });
    });
  }

  byId("connections-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, "Refreshing…", () => Promise.all([
    loadProviderCredentials(),
    loadConnections(true),
  ])));
  byId("gmail-oauth-mode-platform").addEventListener("change", (event) => {
    if (event.currentTarget.checked) selectGoogleOauthMode("platform");
  });
  byId("gmail-oauth-mode-user").addEventListener("change", (event) => {
    if (event.currentTarget.checked) selectGoogleOauthMode("user");
  });
  byId("gmail-connect-platform").addEventListener("click", (event) => startGmailOAuth("platform", event.currentTarget));
  byId("gmail-connect-user").addEventListener("click", (event) => startGmailOAuth("user", event.currentTarget));
  byId("gmail-oauth-client-form").addEventListener("submit", saveGoogleOauthClient);
  byId("gmail-replace-client").addEventListener("click", beginGoogleOauthClientReplacement);
  byId("gmail-cancel-client-edit").addEventListener("click", cancelGoogleOauthClientReplacement);
  byId("gmail-delete-client").addEventListener("click", (event) => deleteGoogleOauthClient(event.currentTarget));
  byId("gmail-copy-callback").addEventListener("click", (event) => copyGoogleCallbackUri(event.currentTarget));
  byId("gmail-revocation-acknowledge").addEventListener("click", () => {
    setGmailRevocationWarning(false);
    renderGmailRevocationWarning();
    toast("The Google-side removal reminder was cleared for this account.", "info");
  });
  byId("automation-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, "Refreshing…", () => loadAutomationJobs()));
  byId("automation-auto-refresh").addEventListener("change", startAutomationPolling);
}

async function signOut() {
  const client = state.supabase;
  setSession(null);
  showPublicSite();
  setAuthMode("signin");
  try {
    await client?.auth.signOut();
  } finally {
    toast("Signed out of this workspace.", "success");
  }
}

function bindStaticEvents() {
  bindActionDialog();
  bindWorkspaceEvents();
  byId("boot-retry").addEventListener("click", () => window.location.reload());
  byId("workflow-dock-dismiss").addEventListener("click", () => {
    const run = state.discoveryRun;
    state.workflowDockDismissedRunId = (run?.jobIds || []).join(":") || String(run?.startedAt || "current-run");
    byId("workflow-dock").hidden = true;
    setAriaBusy(byId("workflow-dock"), false);
    announce("Workflow status dismissed. Its full history remains in Activity.");
  });
  window.addEventListener("popstate", () => {
    if (!state.session) return;
    switchView(viewFromUrl(), false);
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !byId("action-dialog")?.open) closeMobileMenu();
  });
}

async function initialise() {
  setBootCheckpoint(
    "service",
    "Preparing your workspace",
    "Checking that the workspace service is ready.",
    "Checking connection",
  );
  bindStaticEvents();
  await Promise.all([checkHealth(), loadPublicProviders()]);
  try {
    const config = await publicRequest("/config");
    state.config = config;
    if (!config?.supabase_url || !config?.supabase_publishable_key) {
      throw new AppError("Supabase public configuration is incomplete.", "config_incomplete");
    }
    state.supabase = createClient(config.supabase_url, config.supabase_publishable_key, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
      global: { headers: { "X-Client-Info": "autoapply-cloud-web/2.0" } },
    });
    setBootCheckpoint(
      "session",
      "Restoring your sign-in",
      "Checking this browser for an existing AutoApply session.",
      "Reading the identity file",
    );
    bindAuthEvents();
    // Bot protection gates only signed-out auth forms. Start it in parallel so a
    // provider outage cannot prevent restoration of an already valid session.
    const captchaPromise = initialiseCaptcha().catch((error) => {
      handleCaptchaLoadFailure(error);
    });
    state.supabase.auth.onAuthStateChange((event, session) => {
      const previousUserId = state.identityUserId;
      setSession(session);
      const identityChanged = previousUserId !== state.identityUserId;
      const eventGeneration = state.identityGeneration;
      const eventIdentity = session?.user?.id ? identitySnapshot() : null;
      window.setTimeout(() => {
        if (eventGeneration !== state.identityGeneration) return;
        if (event === "PASSWORD_RECOVERY") showRecoveryForm(true);
        else if (event === "SIGNED_OUT") showPublicSite();
        else if (event === "SIGNED_IN" && session && (identityChanged || byId("workspace").hidden) && !state.recoveryMode) {
          if (!isCurrentIdentity(eventIdentity)) return;
          showWorkspace(session).catch((error) => {
            if (!isIdentityChanged(error)) toast(errorMessage(error, "The workspace could not be loaded."), "error");
          });
        }
      }, 0);
    });
    const result = await state.supabase.auth.getSession();
    const authCallbackError = consumeAuthCallbackError();
    setSession(result.data?.session || null);
    const recoveryHint = new URL(window.location.href).searchParams.get("recovery") === "1";
    if (recoveryHint && state.session) showRecoveryForm(true);
    else if (state.session) await showWorkspace(state.session);
    else {
      showPublicSite();
      if (authCallbackError) setFormMessage("auth-message", authCallbackError, "error");
    }
    await captchaPromise;
  } catch (error) {
    if (state.session && state.supabase) {
      showBootFailure(error);
      toast(errorMessage(error, "The workspace could not be loaded."), "error", "Workspace paused", 0);
      return;
    }
    showPublicSite();
    byId("auth-unavailable").hidden = false;
    all("#auth-card input, #auth-card button").forEach((control) => { control.disabled = true; });
    toast(errorMessage(error, "Authentication is not configured on this deployment."), "error", "Setup required", 0);
  }
}

initialise();
