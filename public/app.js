import { createClient } from "/vendor/supabase.js";

const API_PREFIX = "/api/v1";
const GROQ_STORAGE_PREFIX = "autoapply.groq_api_key.v2";
const GMAIL_REVOCATION_WARNING_PREFIX = "autoapply.gmail_revocation_warning.v1";
const UI_STORAGE_KEY = "autoapply.ui_preferences.v1";
const DEFAULT_RESUME_LIMIT = 6_291_456;
const TURNSTILE_SCRIPT_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
const TURNSTILE_FLEXIBLE_MIN_WIDTH = 300;

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
    kicker: "Public sourcing",
    title: "Job Radar",
    description: "Fetch public jobs from Telegram channels, LinkedIn guest listings, RSS feeds, and ATS boards.",
  },
  jobs: {
    kicker: "Opportunity workspace",
    title: "Jobs",
    description: "Capture job descriptions, contacts, and application links.",
  },
  applications: {
    kicker: "Review before action",
    title: "Applications",
    description: "Edit, approve, and send one deliberate application at a time.",
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
  connections: [],
  googleOauthClient: {},
  googleOauthMode: null,
  googleOauthEditing: false,
  publicProviders: [],
  automationJobs: [],
  discoverySources: [],
  formRevisions: {},
  selectedFormRevisionId: null,
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
  if (node) node.textContent = value == null ? "" : String(value);
}

function clearNode(node) {
  if (node) node.replaceChildren();
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
  clearNode(container);
  const list = createElement("div", { className: "loading-list", attrs: { "aria-label": "Loading" } });
  for (let index = 0; index < count; index += 1) {
    list.append(createElement("div", { className: "skeleton", attrs: { "aria-hidden": "true" } }));
  }
  container.append(list);
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
  state.connections = [];
  state.googleOauthClient = {};
  state.googleOauthMode = null;
  state.googleOauthEditing = false;
  state.automationJobs = [];
  state.discoverySources = [];
  state.formRevisions = {};
  state.selectedFormRevisionId = null;
  state.pendingJobImportFile = null;
  state.resumeSuggestions = null;
  state.pendingResumeFile = null;
  state.selectedApplicationId = null;
  state.applicationEditorDirty = false;
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
  button.dataset.busy = "true";
  button.disabled = true;
  button.replaceChildren(document.createTextNode(busyLabel));
  try {
    return await action();
  } finally {
    delete button.dataset.busy;
    button.replaceChildren(...originalNodes);
    if (isCaptchaProtectedAuthButton(button)) updateCaptchaControls();
    else button.disabled = false;
  }
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

function getGroqKey(userId = state.identityUserId) {
  try {
    const storageKey = groqStorageKey(userId);
    return storageKey ? localStorage.getItem(storageKey) || "" : "";
  } catch {
    return "";
  }
}

function saveGroqKey(value, userId = state.identityUserId) {
  const storageKey = groqStorageKey(userId);
  if (!storageKey) throw new AppError("Sign in before saving a Groq key.", "not_authenticated");
  try {
    localStorage.setItem(storageKey, value);
  } catch {
    throw new AppError("This browser blocked local storage. Allow site storage to save the Groq key.", "storage_unavailable");
  }
}

function deleteGroqKey(userId = state.identityUserId) {
  try {
    const storageKey = groqStorageKey(userId);
    if (storageKey) localStorage.removeItem(storageKey);
  } catch {
    throw new AppError("This browser blocked access to local storage.", "storage_unavailable");
  }
}

function maskSecret(value) {
  if (!value) return "";
  const prefix = value.startsWith("gsk_") ? "gsk_" : "key_";
  const suffix = value.slice(-4);
  return `${prefix}••••••••${suffix}`;
}

async function publicRequest(path) {
  const response = await fetch(`${API_PREFIX}${path}`, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  const payload = await readResponse(response);
  if (!response.ok) throw apiErrorFrom(response, payload);
  return payload;
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
  if (options.groq) {
    const key = getGroqKey(identity.userId);
    if (!key) throw new AppError("Save a Groq API key in this browser first.", "groq_key_missing");
    headers.set("X-Groq-Api-Key", key);
  }

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
  byId("boot-screen").hidden = true;
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
  byId("boot-screen").hidden = true;
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
  if (state.accountDeletionInProgress) {
    showAccountDeletionScreen();
    return;
  }
  setSession(session);
  state.accountDeletionInProgress = false;
  const identity = identitySnapshot();
  byId("skip-link").setAttribute("href", "#main-content");
  byId("boot-screen").hidden = true;
  byId("account-deletion-screen").hidden = true;
  byId("public-site").hidden = true;
  byId("workspace").hidden = false;
  document.body.classList.add("workspace-open");
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

  const requested = new URL(window.location.href).searchParams.get("view");
  switchView(requested === "assets" ? "profile" : requested, false);
  await loadWorkspace(identity);
  if (!isCurrentIdentity(identity) || state.accountDeletionInProgress) return;
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
    loadConnections(true, identity),
    loadAutomationJobs(true, identity),
    loadDiscoverySources(true, identity),
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

function switchView(view, push = true) {
  if (view === "assets") view = "profile";
  if (!Object.hasOwn(viewCopy, view)) view = "overview";
  state.currentView = view;
  all("[data-view-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.viewPanel !== view;
  });
  all("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const copy = viewCopy[view];
  setText("view-kicker", copy.kicker);
  setText("view-title", copy.title);
  setText("view-description", copy.description);
  closeMobileMenu();
  if (push) {
    const url = new URL(window.location.href);
    url.searchParams.set("view", view);
    url.searchParams.delete("oauth");
    url.searchParams.delete("connection");
    url.searchParams.delete("oauth_error");
    window.history.pushState({ view }, "", url);
  }
  if (view === "automation") startAutomationPolling();
  else stopAutomationPolling();
  byId("main-content")?.scrollTo({ top: 0, behavior: "smooth" });
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
      await loadJobs(true);
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
    deleteGroqKey(identity.userId);
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
    toast("Your account, workspace, and browser-stored Groq key were permanently deleted.", "success", "Account deleted", 0);
  } else {
    toast("Your account was deleted, but this browser blocked removal of the local Groq key. Clear site data or rotate the key in Groq Console.", "error", "Account deleted with a local cleanup warning", 0);
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
  if (!window.confirm(`Permanently delete ${email} and all of its AutoApply workspace data? This cannot be undone.`)) return;

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
      if (getGroqKey()) {
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
  if (!getGroqKey()) {
    switchView("profile");
    requestAnimationFrame(() => byId("groq-key")?.focus());
    toast("Add a Groq key to extract the complete profile from your résumé.", "info");
    return;
  }
  await analyzeResume(active.id, { button, autoFill: true });
  switchView("profile");
}

async function removeResume(resume, button) {
  if (!window.confirm(`Delete ${resume.original_name || "this résumé"}? This also removes its private stored object.`)) return;
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
  const key = getGroqKey();
  const summary = byId("groq-saved-summary");
  const pill = byId("groq-status-pill");
  summary.hidden = !key;
  setText("groq-masked-key", key ? maskSecret(key) : "");
  pill.className = `status-pill ${key ? "status-info" : "status-neutral"}`;
  pill.textContent = key ? "Saved in browser" : "Not saved";
  byId("validate-groq").disabled = !key;
  byId("delete-groq").disabled = !key;
  renderOverview();
  renderJobIntelligence();
}

function saveGroq(event) {
  event.preventDefault();
  const input = byId("groq-key");
  const key = input.value.trim();
  setFormMessage("groq-message");
  if (key.length < 10) {
    setFormMessage("groq-message", "Enter a complete Groq API key.", "error");
    return;
  }
  try {
    saveGroqKey(key);
    input.value = "";
    input.type = "password";
    byId("toggle-groq-visibility").setAttribute("aria-label", "Show API key");
    renderGroqState();
    setFormMessage("groq-message", "Saved only in this browser. Validate it before drafting.", "success");
    toast("Groq key saved in this browser.", "success");
  } catch (error) {
    setFormMessage("groq-message", errorMessage(error), "error");
  }
}

async function validateGroq(button) {
  await withBusy(button, "Validating…", async () => {
    try {
      const payload = await apiRequest("/groq/validate", { method: "POST", groq: true });
      const valid = payload?.valid === true;
      const failureState = {
        groq_invalid_key: { label: "Invalid key", tone: "status-danger" },
        groq_model_forbidden: { label: "Model blocked", tone: "status-warning" },
        groq_model_unavailable: { label: "Model unavailable", tone: "status-warning" },
        groq_rate_limited: { label: "Rate limited", tone: "status-warning" },
        unavailable: { label: "Groq unavailable", tone: "status-warning" },
        groq_unavailable: { label: "Groq unavailable", tone: "status-warning" },
      }[payload?.status] || { label: "Validation failed", tone: "status-warning" };
      const pill = byId("groq-status-pill");
      pill.className = `status-pill ${valid ? "status-success" : failureState.tone}`;
      pill.textContent = valid ? "Validated" : failureState.label;
      const failureMessage = typeof payload?.message === "string" && payload.message.trim()
        ? payload.message.trim()
        : "Groq could not validate this key. Try again.";
      setFormMessage(
        "groq-message",
        valid ? `Validated${payload.model ? ` for ${payload.model}` : ""}.` : failureMessage,
        valid ? "success" : "error",
      );
      if (valid) {
        saveUiPreferences({ groq_validated_at: new Date().toISOString(), groq_model: payload.model || null });
        toast("Groq key validated. It remains stored only in this browser.", "success");
      }
    } catch (error) {
      const pill = byId("groq-status-pill");
      const temporarilyLimited = error?.code === "groq_request_rate_limited";
      pill.className = "status-pill status-warning";
      pill.textContent = temporarilyLimited ? "Wait a moment" : "Validation failed";
      setFormMessage("groq-message", errorMessage(error, "The key could not be validated."), "error");
    }
  });
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

async function submitLinkedInDiscovery(event) {
  event.preventDefault();
  const button = event.submitter;
  await withBusy(button, "Queueing…", async () => {
    try {
      const payload = await apiRequest("/discovery/linkedin", {
        method: "POST",
        body: {
          keywords: byId("discovery-keywords").value.trim(),
          location: byId("discovery-location").value.trim() || null,
          remote_only: byId("discovery-remote-only").checked,
          limit: 20,
          idempotency_key: discoveryRunKey("linkedin"),
        },
      });
      const queued = unwrapData(payload) || {};
      showDiscoveryResult("LinkedIn public job scan queued. A running background worker is required; follow the run in Activity.", "success");
      await loadAutomationJobs(true);
      if (queued.id) toast("LinkedIn public job scan queued.", "success");
    } catch (error) {
      showDiscoveryResult(errorMessage(error, "The public search could not be queued."), "error");
    }
  });
}

async function queuePublicFeedDiscovery(button) {
  await withBusy(button, "Queueing…", async () => {
    try {
      await apiRequest("/discovery/public-feeds", {
        method: "POST",
        body: { source_ids: [], limit: 60, idempotency_key: discoveryRunKey("feeds") },
      });
      showDiscoveryResult("Telegram and RSS scan queued. A running background worker is required; follow the run in Activity.", "success");
      await loadAutomationJobs(true);
    } catch (error) {
      showDiscoveryResult(errorMessage(error, "The feed check could not be queued."), "error");
    }
  });
}

function importedCount(payload) {
  const data = unwrapData(payload) || {};
  for (const value of [data.imported, data.saved, data.count, payload?.count]) {
    if (Number.isInteger(value) && value >= 0) return value;
  }
  const items = unwrapItems(payload, ["jobs", "items"]);
  return items.length;
}

async function ingestReferralDigest(event) {
  event.preventDefault();
  const button = event.submitter;
  await withBusy(button, "Extracting…", async () => {
    try {
      const payload = await apiRequest("/discovery/referrals", {
        method: "POST",
        body: { text: byId("referral-digest").value.trim() },
      });
      const count = importedCount(payload);
      byId("referral-ingest-form").reset();
      await loadJobs(true);
      showDiscoveryResult(`${count} opportunit${count === 1 ? "y was" : "ies were"} extracted and saved to your workspace.`, "success");
    } catch (error) {
      showDiscoveryResult(errorMessage(error, "The referral digest could not be extracted."), "error");
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
      await loadJobs(true);
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
      await loadJobs(true);
      showDiscoveryResult(`${count} supported public application link${count === 1 ? " was" : "s were"} saved.`, "success");
    } catch (error) {
      showDiscoveryResult(errorMessage(error, "Those ATS links could not be saved."), "error");
    }
  });
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
  renderOverview();
  renderJobIntelligence();
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

function providerForJob(job) {
  const supplied = String(job?.metadata?.provider || job?.metadata?.ats_provider || job?.metadata?.discovery?.provider || "").toLowerCase();
  const supported = new Set(["google_forms", "greenhouse", "lever", "ashby", "yc", "wellfound", "cutshort", "instahyre"]);
  if (supported.has(supplied)) return supplied;
  const url = safeHttpUrl(job?.apply_url);
  if (!url) return null;
  const host = new URL(url).hostname.toLowerCase();
  if (host === "forms.gle" || (host === "docs.google.com" && new URL(url).pathname.startsWith("/forms/"))) return "google_forms";
  if (host === "greenhouse.io" || host.endsWith(".greenhouse.io")) return "greenhouse";
  if (host === "lever.co" || host.endsWith(".lever.co")) return "lever";
  if (host === "ashbyhq.com" || host.endsWith(".ashbyhq.com")) return "ashby";
  if (host === "workatastartup.com" || host.endsWith(".workatastartup.com")) return "yc";
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
      const scan = createElement("button", {
        className: `button ${canScan ? "button-accent" : "button-ghost"} button-small`,
        text: canScan ? "Scan application" : "Scan not enabled",
        type: "button",
      });
      scan.disabled = !canScan;
      scan.title = canScan
        ? `Capture the current ${humanize(applicationProvider)} form before reviewing answers`
        : capability?.reason || "Hosted form scanning is not enabled for this provider.";
      if (canScan) scan.addEventListener("click", () => scanJobApplication(job, applicationProvider, scan));
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
      if (application?.id) selectApplication(application.id);
      switchView("applications");
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
      if (application?.id) selectApplication(application.id);
      switchView("applications");
      toast("Blank draft created for review.", "success");
    } catch (error) {
      toast(errorMessage(error, "A blank application could not be created."), "error");
    }
  });
}

async function scanJobApplication(job, provider, button) {
  await withBusy(button, "Queueing scan…", async () => {
    try {
      const payload = await apiRequest(`/jobs/${encodeURIComponent(job.id)}/application/scan`, {
        method: "POST",
        body: { idempotency_key: discoveryRunKey("scan"), form_revision_id: null },
      });
      const data = unwrapData(payload) || {};
      const applicationId = data.application?.id || data.application_id || null;
      await Promise.all([loadApplications(true), loadAutomationJobs(true)]);
      const application = applicationId
        ? state.applications.find((item) => item.id === applicationId)
        : state.applications.find((item) => item.job_id === job.id && item.channel === "ats");
      if (application) selectApplication(application.id);
      switchView("applications");
      toast(`${humanize(provider)} form scan queued. Activity will show when its reviewable revision is ready.`, "success");
    } catch (error) {
      if (error?.code === "provider_connection_required" || error?.code === "browser_context_missing") {
        toast(`Connect ${humanize(provider)} in the provider center before scanning this form.`, "error");
        switchView("connections");
      } else {
        toast(errorMessage(error, "The application scan could not be queued."), "error");
      }
    }
  });
}

async function loadApplications(quiet = false, identity = identitySnapshot()) {
  const container = byId("application-list");
  if (!quiet) showLoading(container);
  const payload = await apiRequest("/applications?limit=50", { identity });
  state.applications = unwrapItems(payload, ["applications"]);
  if (state.selectedApplicationId && !state.applications.some((item) => item.id === state.selectedApplicationId)) {
    state.selectedApplicationId = null;
    state.applicationEditorDirty = false;
  }
  if (!state.selectedApplicationId && state.applications.length) {
    state.selectedApplicationId = state.applications[0].id;
  }
  renderApplications();
  renderOverview();
  return state.applications;
}

function jobForApplication(application) {
  return state.jobs.find((job) => job.id === application.job_id) || null;
}

function filteredApplications() {
  const status = byId("application-status-filter").value;
  return state.applications.filter((application) => !status || application.status === status);
}

function renderApplications() {
  const container = byId("application-list");
  clearNode(container);
  const applications = filteredApplications();
  const draftCount = state.applications.filter((item) => ["draft_pending", "drafted", "approved"].includes(item.status)).length;
  const badge = byId("draft-count-badge");
  badge.hidden = draftCount === 0;
  badge.textContent = String(draftCount);
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

function selectApplication(id) {
  if (state.applicationEditorDirty && state.selectedApplicationId && state.selectedApplicationId !== id) {
    if (!window.confirm("Discard the unsaved edits in the current application?")) return;
  }
  state.applicationEditorDirty = false;
  state.selectedApplicationId = id;
  const application = state.applications.find((item) => item.id === id);
  renderApplications();
  if (application) {
    populateApplicationEditor(application);
    if (state.currentView === "applications") byId("application-editor").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function populateApplicationEditor(application) {
  const job = jobForApplication(application);
  const isFormApplication = application.channel === "ats";
  state.applicationEditorDirty = false;
  byId("application-id").value = application.id || "";
  byId("application-recipient").value = application.recipient || "";
  byId("application-subject").value = application.subject || "";
  byId("application-body").value = application.body || "";
  byId("application-attach-resume").checked = true;
  byId("application-fields").disabled = false;
  byId("application-fields").hidden = isFormApplication;
  byId("application-form-review").hidden = !isFormApplication;
  setText("application-job-context", job ? `${job.title || "Role"} at ${job.company || "company"}` : "Application without a linked job");
  const pill = byId("application-status-pill");
  pill.className = `status-pill ${statusClass(application.status)}`;
  pill.textContent = humanize(application.status || "draft_pending");
  updateApplicationCharacterCount();
  updateApplicationActionState(application);
  setFormMessage("application-editor-message");
  if (isFormApplication) {
    loadApplicationFormRevisions(application.id).catch((error) => {
      setFormMessage("form-revision-message", errorMessage(error, "The captured form revision could not be loaded."), "error");
    });
  } else {
    state.selectedFormRevisionId = null;
    clearNode(byId("form-revision-answers"));
  }
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

function clearApplicationEditor(clearSelection = true) {
  if (clearSelection && state.applicationEditorDirty && !window.confirm("Discard these unsaved application edits?")) return;
  if (clearSelection) state.selectedApplicationId = null;
  state.applicationEditorDirty = false;
  byId("application-editor").reset();
  byId("application-id").value = "";
  byId("application-fields").disabled = true;
  byId("application-fields").hidden = false;
  byId("application-form-review").hidden = true;
  state.selectedFormRevisionId = null;
  clearNode(byId("form-revision-answers"));
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
  return [...revisions].sort((a, b) => Number(b.revision || 0) - Number(a.revision || 0))[0] || null;
}

async function loadApplicationFormRevisions(applicationId, quiet = false) {
  const answerList = byId("form-revision-answers");
  if (!quiet) showLoading(answerList, 2);
  const payload = await apiRequest(`/applications/${encodeURIComponent(applicationId)}/form-revisions`);
  state.formRevisions[applicationId] = unwrapItems(payload, ["revisions"]);
  const latest = latestFormRevision(applicationId);
  state.selectedFormRevisionId = latest?.id || null;
  renderFormRevision(latest);
  return state.formRevisions[applicationId];
}

function formQuestionControl(question, value, answerKey) {
  const rawType = String(question.type || question.input_type || "text").toLowerCase();
  const attrs = { "data-answer-key": answerKey };
  let control;
  const options = Array.isArray(question.options) ? question.options : Array.isArray(question.choices) ? question.choices : [];
  if (["select", "radio", "dropdown", "multiselect"].includes(rawType) && options.length) {
    control = createElement("select", { attrs: { ...attrs, ...(rawType === "multiselect" ? { multiple: "" } : {}) } });
    if (rawType !== "multiselect") control.append(createElement("option", { text: "Choose an answer", attrs: { value: "" } }));
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
    control = createElement("input", { attrs: { ...attrs, value: "Active résumé is supplied securely by the worker", readonly: "" } });
    control.dataset.systemAnswer = "resume";
  } else {
    const inputType = ["email", "tel", "url", "number", "date"].includes(rawType) ? rawType : "text";
    control = createElement("input", { type: inputType, attrs: { ...attrs, maxlength: inputType === "text" ? "1000" : undefined } });
    control.value = value == null ? "" : String(value);
  }
  return control;
}

function renderFormRevision(revision) {
  const container = byId("form-revision-answers");
  clearNode(container);
  const status = byId("form-revision-status");
  if (!revision) {
    status.className = "status-pill status-neutral";
    status.textContent = "Awaiting scan";
    setText("form-revision-context", "The worker has not returned a captured form yet. Follow the scan in Activity, then reopen this application.");
    container.append(emptyState("No captured form revision", "A scan records the exact visible questions before anything is filled.", "⌕"));
    for (const id of ["suggest-form-answers", "approve-form-revision", "prefill-form-revision", "submit-form-revision"]) byId(id).disabled = true;
    return;
  }
  const approved = Boolean(revision.approved_at) || revision.status === "approved";
  status.className = `status-pill ${approved ? "status-success" : "status-warning"}`;
  status.textContent = approved ? `Revision ${revision.revision} approved` : `Revision ${revision.revision} needs review`;
  setText("form-revision-context", `${humanize(revision.provider || "application form")} · ${revisionQuestions(revision).length} captured field${revisionQuestions(revision).length === 1 ? "" : "s"}. Approval applies only to schema ${String(revision.schema_hash || "").slice(0, 10) || "unknown"}.`);
  const answers = revision.answers && typeof revision.answers === "object" ? revision.answers : {};
  const questions = revisionQuestions(revision);
  if (!questions.length) {
    container.append(emptyState("No fillable questions found", "Open the source form to confirm whether it is still available.", "◇"));
  }
  questions.forEach((question, index) => {
    const answerKey = String(question.key || question.id || question.name || `field_${index + 1}`);
    const labelText = String(question.label || question.title || question.text || `Field ${index + 1}`);
    const wrapper = createElement("div", { className: "form-answer-row" });
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
  byId("approve-form-revision").disabled = approved;
  byId("suggest-form-answers").disabled = approved || !getGroqKey();
  byId("suggest-form-answers").title = approved
    ? "Run a new scan to change this sealed revision"
    : !getGroqKey()
      ? "Save a Groq key in Profile first"
      : "Suggest only answers grounded in your profile, résumé, and this job";
  const capability = capabilityForProvider(revision.provider);
  const canPrefill = capability?.can_prefill === true;
  const canSubmit = capability?.can_auto_apply === true;
  byId("prefill-form-revision").disabled = !approved || !canPrefill;
  byId("submit-form-revision").disabled = !approved || !canSubmit;
  byId("prefill-form-revision").title = canPrefill
    ? "Open the approved answers in an isolated browser for final review"
    : capability?.reason || "Hosted prefill is not enabled for this provider.";
  byId("submit-form-revision").title = canSubmit
    ? "Submit only this exact approved revision"
    : capability?.reason || "Hosted submission is not enabled for this provider.";
  setFormMessage("form-revision-message", approved ? "This exact answer/schema revision is sealed. Run a new scan to change it; prefill and submit remain separate actions." : "Review every captured answer. Approving seals this exact answer snapshot.");
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

async function suggestFormAnswers(button) {
  const applicationId = byId("application-id").value;
  const revision = latestFormRevision(applicationId);
  if (!revision?.id) return;
  await withBusy(button, "Suggesting…", async () => {
    try {
      const payload = await apiRequest(`/application-form-revisions/${encodeURIComponent(revision.id)}/suggest`, {
        method: "POST",
        groq: true,
      });
      const suggestions = unwrapData(payload)?.answers || {};
      let applied = 0;
      for (const control of all("[data-answer-key]", byId("form-revision-answers"))) {
        const key = control.dataset.answerKey;
        if (!key || !Object.hasOwn(suggestions, key) || control.dataset.systemAnswer) continue;
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
      setFormMessage(
        "form-revision-message",
        applied
          ? `${applied} grounded suggestion${applied === 1 ? " was" : "s were"} added. Review every answer before approving.`
          : "Groq found no answers it could ground in your supplied facts. Complete the remaining fields manually.",
        applied ? "success" : "",
      );
    } catch (error) {
      setFormMessage("form-revision-message", errorMessage(error, "Form suggestions could not be generated."), "error");
    }
  });
}

async function approveFormRevision(button) {
  const applicationId = byId("application-id").value;
  const revision = latestFormRevision(applicationId);
  if (!revision?.id) return;
  await withBusy(button, "Approving…", async () => {
    try {
      await apiRequest(`/application-form-revisions/${encodeURIComponent(revision.id)}/approve`, {
        method: "POST",
        body: {
          expected_revision: Number(revision.revision),
          schema_hash: revision.schema_hash,
          answers: formRevisionAnswers(),
        },
      });
      await loadApplicationFormRevisions(applicationId, true);
      toast("The exact captured form revision was approved.", "success");
    } catch (error) {
      setFormMessage("form-revision-message", errorMessage(error, "This form revision could not be approved."), "error");
    }
  });
}

async function queueFormRevisionStage(stage, button) {
  const applicationId = byId("application-id").value;
  const revision = latestFormRevision(applicationId);
  if (!revision?.id) return;
  if (!(revision.approved_at || revision.status === "approved")) {
    setFormMessage("form-revision-message", "Approve this exact revision before queueing browser work.", "error");
    return;
  }
  if (stage === "submit" && !window.confirm("Submit this exact approved application now? The worker will stop for CAPTCHA, MFA, or an uncertain confirmation.")) return;
  await withBusy(button, stage === "submit" ? "Queueing submit…" : "Queueing prefill…", async () => {
    try {
      await apiRequest(`/application-form-revisions/${encodeURIComponent(revision.id)}/${stage}`, {
        method: "POST",
        body: { idempotency_key: discoveryRunKey(stage), form_revision_id: revision.id },
      });
      await loadAutomationJobs(true);
      setFormMessage("form-revision-message", `${humanize(stage)} queued. Follow the isolated browser run in Activity.`, "success");
      toast(`${humanize(stage)} queued for the approved revision.`, "success");
    } catch (error) {
      setFormMessage("form-revision-message", errorMessage(error, `The ${stage} action could not be queued.`), "error");
    }
  });
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
  if (!window.confirm(`Send this approved message now through your connected Gmail account${attachmentCopy}? This action may not be reversible.`)) return;
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
  return state.connections;
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
  renderGmailRevocationWarning();
  renderGoogleOauthSetup();
  clearNode(container);
  const providers = mergedProviders();
  if (!providers.length) {
    container.append(emptyState("No provider catalog available", "The deployment operator has not published connection capabilities yet.", "⌁"));
    return;
  }
  const sorted = [...providers].sort((a, b) => {
    const order = { gmail: 0, linkedin: 1 };
    const aid = String(a.id || a.provider || "").toLowerCase();
    const bid = String(b.id || b.provider || "").toLowerCase();
    return (order[aid] ?? 10) - (order[bid] ?? 10) || String(a.label || aid).localeCompare(String(b.label || bid));
  });
  for (const provider of sorted) {
    const id = String(provider.id || provider.provider || "").toLowerCase();
    const connection = providerConnection(provider);
    const status = connectionStatus(provider);
    const card = createElement("article", { className: "connection-card" });
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
        const disconnect = createElement("button", { className: "button button-danger-quiet button-small", text: "Delete connection", type: "button" });
        disconnect.addEventListener("click", () => disconnectProvider(id, disconnect));
        actions.append(disconnect);
        const check = createElement("button", { className: "button button-ghost button-small", text: "Queue health check", type: "button" });
        check.addEventListener("click", () => queueConnectionCheck(id, check));
        actions.append(check);
      } else if (provider.can_connect === false) {
        const use = createElement("button", { className: "button button-ghost button-small", text: provider.available === false ? "Not enabled" : "No login required", type: "button" });
        use.disabled = provider.available === false;
        use.addEventListener("click", () => switchView("jobs"));
        actions.append(use);
      } else if (status === "pending") {
        const complete = createElement("button", { className: "button button-primary button-small", text: "I completed login", type: "button" });
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
        const connect = createElement("button", { className: "button button-primary button-small", text: "Open secure login", type: "button" });
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
  if (!window.confirm("Delete your saved Google OAuth Client ID and Client Secret? You will need to enter both again before reconnecting.")) return;
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
      toast(`${humanize(provider)} browser state was saved for manual use. The provider login remains unverified.`, "info");
    } catch (error) {
      toast(errorMessage(error, "The provider login could not be confirmed yet."), "error");
    }
  });
}

async function disconnectProvider(provider, button) {
  if (!window.confirm(`Disconnect ${humanize(provider)} and delete its stored authorization/context metadata?`)) return;
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
  renderAutomationJobs();
  renderOverview();
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
  const groqReady = Boolean(getGroqKey());
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
  setGate("jobs-groq-status", groqReady, "Saved in this browser", "Not saved", "jobs-open-groq", "Manage", "Add key");
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
    [Boolean(getGroqKey()), "Groq key saved locally", "Save a Groq key"],
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

  byId("groq-form").addEventListener("submit", saveGroq);
  byId("validate-groq").addEventListener("click", (event) => validateGroq(event.currentTarget));
  byId("delete-groq").addEventListener("click", () => {
    if (!window.confirm("Delete the Groq key from this browser?")) return;
    try {
      deleteGroqKey();
      renderGroqState();
      setFormMessage("groq-message", "The key was removed from this browser.", "success");
    } catch (error) {
      setFormMessage("groq-message", errorMessage(error), "error");
    }
  });
  byId("toggle-groq-visibility").addEventListener("click", () => {
    const input = byId("groq-key");
    input.type = input.type === "password" ? "text" : "password";
    byId("toggle-groq-visibility").setAttribute("aria-label", input.type === "password" ? "Show API key" : "Hide API key");
  });

  byId("job-form").addEventListener("submit", saveJob);
  byId("job-cancel-edit").addEventListener("click", resetJobForm);
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

  byId("linkedin-discovery-form").addEventListener("submit", submitLinkedInDiscovery);
  byId("public-feeds-run").addEventListener("click", (event) => queuePublicFeedDiscovery(event.currentTarget));
  byId("referral-ingest-form").addEventListener("submit", ingestReferralDigest);
  byId("job-import-form").addEventListener("submit", importJobFile);
  byId("job-import-file").addEventListener("change", (event) => {
    state.pendingJobImportFile = event.target.files?.[0] || null;
    setText("job-import-file-label", state.pendingJobImportFile ? `${state.pendingJobImportFile.name} · ${formatBytes(state.pendingJobImportFile.size)}` : "Title and company are required; common header names are accepted.");
  });
  byId("ats-link-form").addEventListener("submit", ingestAtsLinks);
  byId("ats-board-form").addEventListener("submit", queueAtsBoardDiscovery);
  byId("discovery-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, "Refreshing…", () => loadJobs()));

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
  byId("suggest-form-answers").addEventListener("click", (event) => suggestFormAnswers(event.currentTarget));
  byId("approve-form-revision").addEventListener("click", (event) => approveFormRevision(event.currentTarget));
  byId("prefill-form-revision").addEventListener("click", (event) => queueFormRevisionStage("prefill", event.currentTarget));
  byId("submit-form-revision").addEventListener("click", (event) => queueFormRevisionStage("submit", event.currentTarget));

  byId("connections-refresh").addEventListener("click", (event) => withBusy(event.currentTarget, "Refreshing…", () => loadConnections()));
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
  bindWorkspaceEvents();
  window.addEventListener("popstate", () => {
    if (!state.session) return;
    const view = new URL(window.location.href).searchParams.get("view") || "overview";
    switchView(view, false);
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMobileMenu();
  });
}

async function initialise() {
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
    showPublicSite();
    byId("auth-unavailable").hidden = false;
    all("#auth-card input, #auth-card button").forEach((control) => { control.disabled = true; });
    toast(errorMessage(error, "Authentication is not configured on this deployment."), "error", "Setup required", 0);
  }
}

initialise();
