"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { createClient, type Session, type SupabaseClient } from "@supabase/supabase-js";
import { apiRequest, errorMessage, idempotencyKey, publicRequest, unwrapData, unwrapItems, ApiError } from "../lib/api";
import { bytesLabel, dateLabel, dateTimeLabel, humanize, safeUrl } from "../lib/format";
import type { Application, AutomationJob, Config, Connection, Contact, Credential, GoogleFormEntry, Job, Profile, ResearchPrompt, Resume, Settings } from "../lib/types";

type View = "overview" | "profile" | "discovery" | "form_pilot" | "outreach" | "jobs" | "applications" | "connections" | "activity" | "settings";
type Message = { text: string; tone?: "success" | "error" | "info" };

const viewMeta: Record<View, { label: string; group: string; icon: string }> = {
  overview: { label: "Overview", group: "START", icon: "⌂" },
  profile: { label: "Profile", group: "START", icon: "◎" },
  discovery: { label: "Find jobs", group: "APPLY", icon: "⌕" },
  form_pilot: { label: "Forms", group: "APPLY", icon: "▤" },
  outreach: { label: "Email leads", group: "APPLY", icon: "@" },
  jobs: { label: "Jobs", group: "LIBRARY", icon: "◇" },
  applications: { label: "Review & send", group: "LIBRARY", icon: "↗" },
  connections: { label: "Connections", group: "SYSTEM", icon: "⌁" },
  activity: { label: "Activity", group: "SYSTEM", icon: "◷" },
  settings: { label: "Settings", group: "SYSTEM", icon: "⚙" },
};

const navGroups = ["START", "APPLY", "LIBRARY", "SYSTEM"];

const simpleEyebrows: Record<string, string> = {
  "Applicant foundation": "Your setup",
  "Private document": "Your résumé",
  "Profile evidence": "Your details",
  "Bounded discovery": "Job search",
  "Additional public sources": "More sources",
  "Worker status": "Running searches",
  "Saved from discovery": "Latest results",
  "Review-gated applications": "Forms",
  "Review-gated outreach": "Email leads",
  "Opportunity library": "Your jobs",
  "Review desk": "Your drafts",
  "Service connections": "Connections",
  "BYOK vault": "AI tools",
  "Gmail delivery": "Email sending",
  "Managed browser providers": "Secure logins",
  "Durable worker history": "Background work",
  "Workspace controls": "Settings",
};

const simpleTitles: Record<string, string> = {
  "Profile & résumé": "Profile",
  "Active résumé": "Your résumé",
  "Save the details you want applications to use": "Your details",
  "Find jobs without hanging the browser": "Find jobs",
  "Search by source": "Other ways to search",
  "Runs that are still working": "Running searches",
  "Latest discovered roles": "Latest results",
  "Form Pilot": "Forms",
  "Prepare an application URL": "Add an application link",
  "Forms ready for preparation": "Saved forms",
  "Groq and Browserbase": "AI tools",
  "Secure provider login": "Secure logins",
  "Sending guardrails": "Sending limits",
};

const simpleCopy: Record<string, string> = {
  "The strongest results come from a parsed résumé, a saved target profile, and a clear send review.": "Start with a résumé, a profile, and a quick review before sending.",
  "Your profile is the source of truth for matching. A résumé upload is parsed privately; extracted values stay suggestions until you review and save them.": "Add your details once. We use them to match jobs and write better drafts.",
  "PDF only · maximum five files · stored in your private Supabase bucket.": "Upload one private PDF. We use it to match jobs.",
  "Manual profile fields remain useful for links, preferences, and facts that a résumé cannot safely infer.": "Add anything your résumé does not show.",
  "Choose a result budget and a hard worker deadline. The request queues quickly; Vercel never waits ten minutes for scraping.": "Choose how many jobs to find and set a time limit.",
  "Uses your parsed résumé and profile to build a focused public-source search. Jobs are saved as they complete.": "We use your résumé and profile to find matching jobs.",
  "These are optional and use the same bounded worker model.": "Optional searches with the same time limit.",
  "Completed and failed runs remain available in Activity.": "Finished runs stay in Activity.",
  "Queue a bounded search above. Results will appear here when the persistent worker saves them.": "Start a search above. Results will appear here when ready.",
  "Capture exact provider questions in the background, review grounded answers, then approve a one-time submission.": "Save a form, review suggested answers, then approve before sending.",
  "Paste a Google Form, YC job detail, or another supported public application URL. Search/listing pages are rejected.": "Paste the exact form or job link. We will prepare the questions.",
  "Answers are editable until you approve this exact revision.": "Edit answers before you approve.",
  "Every role here came from a bounded discovery run or an imported research workbook. There is no duplicate manual job form.": "Review jobs from search or your imported file.",
  "Edit, approve, and queue one exact message at a time. Approved email delivery is handled by the persistent worker outside Vercel.": "Check each message, approve it, and send when ready.",
  "Generate a strict external research brief, import the completed workbook, choose up to 30 roles in order, then draft and send only after review.": "Find leads with AI, choose up to 30, and review every email.",
  "This picker is a normal contained React control. It does not open an overlay or mutate page scroll. Required evidence columns are validated by the API.": "Upload the file Claude, ChatGPT, or Gemini created. We check the columns for you.",
  "Keep provider credentials encrypted on the server. Secrets are never returned to the browser after saving.": "Connect the tools you use. Keys are encrypted and never shown again.",
  "Groq powers grounded drafts and résumé analysis. Browserbase powers managed form workflows when enabled.": "Groq writes drafts. Browserbase helps with supported forms.",
  "Gmail is used only after you approve exact messages. Daily sending is capped in Settings.": "Connect Gmail to send approved emails. A daily cap is always on.",
  "Discovery, form scans, and email delivery continue in the persistent worker even after a Vercel request ends.": "Searches, forms, and emails keep running in the background.",
  "Delivery safety remains review-gated. Change the daily cap and duplicate window for the persistent Gmail worker.": "Set your daily email limit and duplicate window.",
  "This picker is a normal contained React control. It does not open an overlay or mutate page scroll. Email leads may be source-verified or source-unverified; both remain review-gated before drafting.": "Upload the file Claude, ChatGPT, or Gemini created. We check the columns before importing it.",
};

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function records(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function parseView(): View {
  if (typeof window === "undefined") return "overview";
  const value = new URL(window.location.href).searchParams.get("view") as View | null;
  return value && value in viewMeta ? value : "overview";
}

function statusTone(value?: string | null): "success" | "warning" | "danger" | "neutral" {
  const normalized = String(value || "").toLowerCase();
  if (["connected", "ready", "parsed", "approved", "applied", "completed", "success", "succeeded"].some((item) => normalized.includes(item))) return "success";
  if (["failed", "error", "rejected", "needs_attention"].some((item) => normalized.includes(item))) return "danger";
  if (["queued", "running", "pending", "parsing", "draft"].some((item) => normalized.includes(item))) return "warning";
  return "neutral";
}

function Status({ value, children }: { value?: string | null; children?: React.ReactNode }) {
  return <span className={`aa-status aa-status-${statusTone(value)}`}>{children || humanize(value || "pending")}</span>;
}

function Button({ children, busy, className = "", ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { busy?: boolean }) {
  return <button {...props} className={`aa-button ${className}`} disabled={props.disabled || busy}>{busy ? "Working…" : children}</button>;
}

function Notice({ message }: { message?: Message | null }) {
  if (!message?.text) return null;
  return <div className={`aa-notice aa-notice-${message.tone || "info"}`} role="status">{message.text}</div>;
}

function Empty({ title, text, action }: { title: string; text: string; action?: React.ReactNode }) {
  return <div className="aa-empty"><div className="aa-empty-mark">·</div><strong>{title}</strong><p>{text}</p>{action}</div>;
}

function SectionHeader({ eyebrow, title, text, action }: { eyebrow?: string; title: string; text?: string; action?: React.ReactNode }) {
  return <div className="aa-section-header"><div>{eyebrow && <p className="aa-eyebrow">{simpleEyebrows[eyebrow] || eyebrow}</p>}<h2>{simpleTitles[title] || title}</h2>{text && <p>{simpleCopy[text] || text}</p>}</div>{action}</div>;
}

function AuthScreen({ client, config }: { client: SupabaseClient | null; config: Config | null }) {
  const [mode, setMode] = useState<"signin" | "signup" | "reset">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!client) return;
    setBusy(true); setMessage(null);
    try {
      if (mode === "reset") {
        await client.auth.resetPasswordForEmail(email, { redirectTo: `${window.location.origin}/?recovery=1` });
        setMessage({ text: "If the account exists, a recovery link has been sent.", tone: "success" });
      } else if (mode === "signup") {
        const result = await client.auth.signUp({ email, password });
        if (result.error) throw result.error;
        setMessage({ text: result.data.session ? "Account created." : "Check your email to confirm the account.", tone: "success" });
      } else {
        const result = await client.auth.signInWithPassword({ email, password });
        if (result.error) throw result.error;
      }
    } catch (error) { setMessage({ text: errorMessage(error, "Authentication failed."), tone: "error" }); }
    finally { setBusy(false); }
  };

  const google = async () => {
    if (!client) return;
    setBusy(true); setMessage(null);
    const result = await client.auth.signInWithOAuth({ provider: "google", options: { redirectTo: window.location.origin } });
    if (result.error) { setMessage({ text: result.error.message, tone: "error" }); setBusy(false); }
  };

  return <main className="aa-auth-shell"><section className="aa-auth-card">
    <div className="aa-brand aa-brand-large"><span className="aa-brand-mark">A</span><span><strong>AutoApply</strong><small>CLOUD</small></span></div>
    <p className="aa-eyebrow">Evidence-first job workspace</p>
    <h1>{mode === "reset" ? "Reset your password" : mode === "signup" ? "Create your workspace" : "Welcome back"}</h1>
    <p className="aa-auth-copy">Find jobs, prepare applications, and send approved emails in one place.</p>
    {!config?.supabase_url ? <Notice message={{ text: "Authentication is not configured on this deployment.", tone: "error" }} /> : <>
      <form className="aa-form aa-auth-form" onSubmit={submit}>
        <label>Email<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required autoComplete="email" /></label>
        {mode !== "reset" && <label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required minLength={6} autoComplete={mode === "signup" ? "new-password" : "current-password"} /></label>}
        <Button type="submit" busy={busy} className="aa-button-primary">{mode === "reset" ? "Send recovery link" : mode === "signup" ? "Create account" : "Sign in"}</Button>
      </form>
      {mode !== "reset" && <><div className="aa-divider"><span>or</span></div><Button type="button" busy={busy} onClick={google} className="aa-button-secondary">Continue with Google</Button></>}
      <div className="aa-auth-links">
        {mode === "signin" && <><button type="button" onClick={() => setMode("signup")}>Create an account</button><button type="button" onClick={() => setMode("reset")}>Forgot password?</button></>}
        {mode !== "signin" && <button type="button" onClick={() => setMode("signin")}>Back to sign in</button>}
      </div>
      <Notice message={message} />
    </>}
    <p className="aa-legal">By continuing you agree to the <a href="/terms.html">Terms</a> and <a href="/privacy.html">Privacy Policy</a>.</p>
  </section></main>;
}

function Topbar({ view, onMenu, onRefresh, userEmail }: { view: View; onMenu: () => void; onRefresh: () => void; userEmail?: string }) {
  return <header className="aa-topbar"><button className="aa-mobile-menu" onClick={onMenu} aria-label="Open navigation">☰</button><div><p className="aa-eyebrow">{viewMeta[view].group}</p><h1>{viewMeta[view].label}</h1></div><div className="aa-topbar-actions"><span className="aa-user-chip">{userEmail || "Workspace"}</span><Button type="button" onClick={onRefresh} className="aa-button-secondary aa-button-small">Refresh</Button></div></header>;
}

function Sidebar({ view, setView, userEmail, onSignOut, open }: { view: View; setView: (view: View) => void; userEmail?: string; onSignOut: () => void; open: boolean }) {
  return <aside className={`aa-sidebar ${open ? "aa-sidebar-open" : ""}`}>
    <div className="aa-brand"><span className="aa-brand-mark">A</span><span><strong>AutoApply</strong><small>CLOUD</small></span></div>
    <nav className="aa-nav" aria-label="Workspace navigation">{navGroups.map((group) => <div key={group} className="aa-nav-group"><span className="aa-nav-label">{group}</span>{Object.entries(viewMeta).filter(([, meta]) => meta.group === group).map(([key, meta]) => <button key={key} className={`aa-nav-item ${view === key ? "aa-nav-active" : ""}`} onClick={() => setView(key as View)}><span>{meta.icon}</span>{meta.label}{key === "outreach" && <em>30</em>}</button>)}</div>)}</nav>
    <div className="aa-sidebar-footer"><button className="aa-profile-chip" onClick={() => setView("profile")}><span className="aa-avatar">{(userEmail || "A").slice(0, 1).toUpperCase()}</span><span><strong>{userEmail?.split("@")[0] || "Your profile"}</strong><small>{userEmail || "Set up your workspace"}</small></span></button><button className="aa-signout" onClick={onSignOut}>Sign out</button></div>
  </aside>;
}

function StatCard({ label, value, detail }: { label: string; value: string | number; detail: string }) {
  return <div className="aa-stat"><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>;
}

function ServiceBadge({ kind, title, text }: { kind: "resume" | "search" | "groq" | "gmail"; title: string; text: string }) {
  const mark = { resume: "CV", search: "⌕", groq: "GQ", gmail: "G" }[kind];
  return <div className="aa-service-badge"><span className={`aa-service-logo aa-service-${kind}`}>{mark}</span><span><strong>{title}</strong><small>{text}</small></span></div>;
}

type WorkspaceProps = { client: SupabaseClient; session: Session; config: Config };

export default function Page() {
  const [config, setConfig] = useState<Config | null>(null);
  const [client, setClient] = useState<SupabaseClient | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [bootMessage, setBootMessage] = useState("Starting your workspace…");
  const [bootError, setBootError] = useState("");

  useEffect(() => {
    let active = true;
    const start = async () => {
      try {
        const [configResult, healthResult] = await Promise.allSettled([publicRequest<Config>("/config"), publicRequest<Record<string, unknown>>("/health")]);
        if (configResult.status === "rejected") throw configResult.reason;
        if (!active) return;
        const nextConfig = configResult.value;
        setConfig(nextConfig);
        if (healthResult.status === "fulfilled" && healthResult.value.status === "setup_required") setBootMessage("Some deployment services need configuration.");
        if (!nextConfig.supabase_url || !nextConfig.supabase_publishable_key) { setBootError("Supabase public configuration is incomplete."); return; }
        const nextClient = createClient(nextConfig.supabase_url, nextConfig.supabase_publishable_key, { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true }, global: { headers: { "X-Client-Info": "autoapply-cloud-next/2.1" } } });
        setClient(nextClient);
        const auth = await nextClient.auth.getSession();
        if (active) { setSession(auth.data.session); setBootMessage(""); }
        const subscription = nextClient.auth.onAuthStateChange((_event, nextSession) => { if (active) setSession(nextSession); });
        return () => subscription.data.subscription.unsubscribe();
      } catch (error) { if (active) setBootError(errorMessage(error, "The service could not be reached.")); }
    };
    void start();
    return () => { active = false; };
  }, []);

  if (bootError && !client) return <main className="aa-loading"><div className="aa-loading-card"><span className="aa-brand-mark">A</span><h1>AutoApply Cloud</h1><p>{bootError}</p><Button onClick={() => window.location.reload()} className="aa-button-primary">Retry</Button></div></main>;
  if (!client || !config) return <main className="aa-loading"><div className="aa-loading-card"><span className="aa-spinner" /><h1>AutoApply Cloud</h1><p>{bootMessage}</p></div></main>;
  if (!session) return <AuthScreen client={client} config={config} />;
  return <Workspace client={client} session={session} config={config} />;
}

function Workspace({ client, session, config }: WorkspaceProps) {
  const [view, setViewState] = useState<View>(parseView);
  const [mobileNav, setMobileNav] = useState(false);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState<Message | null>(null);
  const [profile, setProfile] = useState<Profile>({});
  const [settings, setSettings] = useState<Settings>({});
  const [resumes, setResumes] = useState<Resume[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [applications, setApplications] = useState<Application[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [activity, setActivity] = useState<AutomationJob[]>([]);
  const [forms, setForms] = useState<GoogleFormEntry[]>([]);
  const [fitSummary, setFitSummary] = useState<Record<string, unknown>>({});
  const [refreshError, setRefreshError] = useState("");

  const notify = useCallback((text: string, tone: Message["tone"] = "info") => {
    setMessage({ text, tone });
    window.setTimeout(() => setMessage((current) => current?.text === text ? null : current), 6000);
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true); setRefreshError("");
    const requests = await Promise.allSettled([
      apiRequest<{ data: Profile }>(client, "/profile"),
      apiRequest<{ data: Settings }>(client, "/settings"),
      apiRequest<{ items: Resume[] }>(client, "/resumes"),
      apiRequest<{ items: Job[]; fit_summary?: Record<string, unknown> }>(client, "/jobs?limit=50&offset=0"),
      apiRequest<{ items: Application[] }>(client, "/applications?limit=50"),
      apiRequest<{ items: Connection[] }>(client, "/connections"),
      apiRequest<{ items: Credential[] }>(client, "/provider-credentials"),
      apiRequest<{ items: AutomationJob[] }>(client, "/automation-jobs?limit=100"),
      apiRequest<{ items: GoogleFormEntry[] }>(client, "/discovery/google-forms?limit=100&offset=0"),
    ]);
    const [profileResult, settingsResult, resumesResult, jobsResult, appsResult, connectionsResult, credentialsResult, activityResult, formsResult] = requests;
    if (profileResult.status === "fulfilled") setProfile(unwrapData(profileResult.value) || {});
    if (settingsResult.status === "fulfilled") setSettings(unwrapData(settingsResult.value) || {});
    if (resumesResult.status === "fulfilled") setResumes(unwrapItems(resumesResult.value));
    if (jobsResult.status === "fulfilled") { setJobs(unwrapItems(jobsResult.value)); setFitSummary(jobsResult.value.fit_summary || {}); }
    if (appsResult.status === "fulfilled") setApplications(unwrapItems(appsResult.value));
    if (connectionsResult.status === "fulfilled") setConnections(unwrapItems(connectionsResult.value));
    if (credentialsResult.status === "fulfilled") setCredentials(unwrapItems(credentialsResult.value));
    if (activityResult.status === "fulfilled") setActivity(unwrapItems(activityResult.value));
    if (formsResult.status === "fulfilled") setForms(unwrapItems(formsResult.value));
    const firstError = requests.find((result) => result.status === "rejected");
    if (firstError?.status === "rejected") setRefreshError(errorMessage(firstError.reason, "Some workspace data could not be loaded."));
    setLoading(false);
  }, [client]);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const onPopState = () => setViewState(parseView);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  useEffect(() => {
    if (view !== "activity") return;
    const timer = window.setInterval(() => { void refresh(); }, 10000);
    return () => window.clearInterval(timer);
  }, [view, refresh]);

  const setView = (nextView: View) => {
    setViewState(nextView); setMobileNav(false);
    const url = new URL(window.location.href);
    url.searchParams.set("view", nextView);
    window.history.pushState({}, "", url);
  };

  const signOut = async () => { await client.auth.signOut(); setSessionSafely(null); };
  const setSessionSafely = (_value: null) => { /* Supabase auth state owns the signed-out transition. */ };

  const action = async (operation: () => Promise<void>, success?: string) => {
    try { await operation(); if (success) notify(success, "success"); } catch (error) { notify(errorMessage(error), "error"); }
  };

  const viewProps = { client, config, session, profile, settings, resumes, jobs, applications, connections, credentials, activity, forms, fitSummary, setProfile, setSettings, setResumes, setJobs, setApplications, setConnections, setCredentials, setActivity, setForms, notify, refresh, action, setView };

  return <div className="aa-shell"><Sidebar view={view} setView={setView} userEmail={session.user.email || undefined} onSignOut={signOut} open={mobileNav} /><div className="aa-main"><Topbar view={view} onMenu={() => setMobileNav((value) => !value)} onRefresh={() => void refresh()} userEmail={session.user.email || undefined} /><main className="aa-content"><Notice message={message} />{refreshError && <div className="aa-notice aa-notice-warning">Some panels could not refresh: {refreshError}</div>}{loading && !jobs.length && !profile.user_id ? <div className="aa-panel"><div className="aa-skeleton aa-skeleton-wide" /><div className="aa-skeleton" /></div> : <>{view === "overview" && <Overview {...viewProps} />} {view === "profile" && <ProfileView {...viewProps} />} {view === "discovery" && <DiscoveryView {...viewProps} />} {view === "form_pilot" && <FormPilotView {...viewProps} />} {view === "outreach" && <OutreachView {...viewProps} />} {view === "jobs" && <JobsView {...viewProps} />} {view === "applications" && <ApplicationsView {...viewProps} />} {view === "connections" && <ConnectionsView {...viewProps} />} {view === "activity" && <ActivityView {...viewProps} />} {view === "settings" && <SettingsView {...viewProps} />}</>}</main></div></div>;
}

type ViewProps = WorkspaceProps & {
  profile: Profile; settings: Settings; resumes: Resume[]; jobs: Job[]; applications: Application[];
  connections: Connection[]; credentials: Credential[]; activity: AutomationJob[]; forms: GoogleFormEntry[];
  fitSummary: Record<string, unknown>; setProfile: React.Dispatch<React.SetStateAction<Profile>>;
  setSettings: React.Dispatch<React.SetStateAction<Settings>>; setResumes: React.Dispatch<React.SetStateAction<Resume[]>>;
  setJobs: React.Dispatch<React.SetStateAction<Job[]>>; setApplications: React.Dispatch<React.SetStateAction<Application[]>>;
  setConnections: React.Dispatch<React.SetStateAction<Connection[]>>; setCredentials: React.Dispatch<React.SetStateAction<Credential[]>>;
  setActivity: React.Dispatch<React.SetStateAction<AutomationJob[]>>; setForms: React.Dispatch<React.SetStateAction<GoogleFormEntry[]>>;
  notify: (text: string, tone?: Message["tone"]) => void; refresh: () => Promise<void>;
  action: (operation: () => Promise<void>, success?: string) => Promise<void>; setView: (view: View) => void;
};

function Overview({ profile, resumes, jobs, applications, connections, activity, setView }: ViewProps) {
  const readyJobs = jobs.filter((job) => job.status !== "archived").length;
  const approved = applications.filter((application) => application.status === "approved").length;
  const queued = activity.filter((job) => ["queued", "running"].includes(stringValue(job.status))).length;
  const connected = connections.filter((item) => stringValue(item.connection && records(item.connection).status) === "connected").length;
  const parsed = resumes.find((resume) => resume.parse_status === "parsed");
  const name = stringValue(profile.full_name, "there").split(" ")[0];
  return <div className="aa-stack"><section className="aa-hero"><div><p className="aa-eyebrow">Your job workspace</p><h2>Good to see you, {name}.</h2><p>Find matching jobs, write better emails, and send only when you approve.</p></div><Button onClick={() => setView("discovery")} className="aa-button-primary">Find jobs</Button></section>
    <section className="aa-panel aa-use-cases"><div className="aa-panel-heading"><div><p className="aa-eyebrow">How it works</p><h3>From résumé to application</h3><p>Four simple steps. You stay in control.</p></div></div><div className="aa-use-case-grid"><ServiceBadge kind="resume" title="1. Add résumé" text="Build your profile" /><ServiceBadge kind="search" title="2. Find jobs" text="See your best matches" /><ServiceBadge kind="groq" title="3. Draft emails" text="Personalise each message" /><ServiceBadge kind="gmail" title="4. Send" text="Review before Gmail sends" /></div></section>
    <div className="aa-stat-grid"><StatCard label="Saved jobs" value={readyJobs} detail="matched to you" /><StatCard label="Email drafts" value={approved} detail="ready to review" /><StatCard label="Connections" value={connected} detail="Gmail and AI tools" /><StatCard label="Running now" value={queued} detail="working in the background" /></div>
    <div className="aa-two-column"><section className="aa-panel"><SectionHeader eyebrow="Start here" title="Get ready in three steps" text="Add your résumé, check your profile, then connect Gmail when you want to send." /><div className="aa-checklist"><Checklist done={Boolean(parsed)} title="Add your résumé" text={parsed ? "Ready for job matching." : "Upload one PDF."} action={() => setView("profile")} /><Checklist done={Boolean(profile.headline && profile.skills?.length)} title="Check your profile" text="Add your role and skills." action={() => setView("profile")} /><Checklist done={connected > 0} title="Connect your tools" text="Gmail is only needed to send." action={() => setView("connections")} /></div></section><section className="aa-panel"><SectionHeader eyebrow="Your jobs" title="Latest matches" action={<Button className="aa-button-link" onClick={() => setView("jobs")}>View all</Button>} />{jobs.slice(0, 4).map((job) => <JobMini key={job.id} job={job} />)}{!jobs.length && <Empty title="No jobs yet" text="Start a search or import a research file." action={<Button onClick={() => setView("discovery")} className="aa-button-secondary">Find jobs</Button>} />}</section></div>
  </div>;
}

function Checklist({ done, title, text, action }: { done: boolean; title: string; text: string; action: () => void }) { return <button className="aa-checklist-item" onClick={action}><span className={`aa-check ${done ? "aa-check-done" : ""}`}>{done ? "✓" : ""}</span><span><strong>{title}</strong><small>{text}</small></span><span className="aa-chevron">›</span></button>; }

function JobMini({ job }: { job: Job }) { const score = numberValue(job.fit?.score); return <div className="aa-list-row"><div className="aa-list-symbol">{stringValue(job.company, "?").slice(0, 1).toUpperCase()}</div><div className="aa-list-copy"><strong>{stringValue(job.title, "Untitled role")}</strong><span>{stringValue(job.company, "Unknown company")}{job.location ? ` · ${job.location}` : ""}</span></div>{score > 0 ? <span className="aa-fit-pill">{score}% fit</span> : <Status value={job.status} />}</div>; }

function Field({ label, value, onChange, type = "text", placeholder, required = false, min, max, step }: { label: string; value: string | number; onChange: (value: string) => void; type?: string; placeholder?: string; required?: boolean; min?: number; max?: number; step?: number }) {
  return <label className="aa-field"><span>{label}</span><input type={type} value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} required={required} min={min} max={max} step={step} /></label>;
}

function ProfileView({ client, config, profile, resumes, setProfile, setResumes, notify, refresh }: ViewProps) {
  const [draft, setDraft] = useState<Profile>(profile);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState("");
  const [suggestions, setSuggestions] = useState<Record<string, unknown> | null>(null);
  useEffect(() => setDraft(profile), [profile]);
  const update = (key: keyof Profile, value: string | number | null) => setDraft((current) => ({ ...current, [key]: value }));
  const preference = records(draft.preferences);
  const updatePreference = (key: string, value: unknown) => setDraft((current) => ({ ...current, preferences: { ...records(current.preferences), [key]: value } }));
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy("save");
    try {
      const payload = { ...draft, years_experience: draft.years_experience === null || draft.years_experience === undefined ? null : Number(draft.years_experience), graduation_year: draft.graduation_year ? Number(draft.graduation_year) : null, skills: Array.isArray(draft.skills) ? draft.skills : [], preferences: { ...preference, target_roles: stringValue(preference.target_roles).split(",").map((item) => item.trim()).filter(Boolean), preferred_locations: stringValue(preference.preferred_locations).split(",").map((item) => item.trim()).filter(Boolean) } };
      const result = await apiRequest<{ data: Profile }>(client, "/profile", { method: "PATCH", body: payload });
      setProfile(unwrapData(result) || {}); notify("Profile saved.", "success");
    } catch (error) { notify(errorMessage(error, "Profile could not be saved."), "error"); } finally { setBusy(""); }
  };
  const hashFile = async (candidate: File) => {
    if (!globalThis.crypto?.subtle) return null;
    const digest = await crypto.subtle.digest("SHA-256", await candidate.arrayBuffer());
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  };
  const upload = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!file) { notify("Choose a PDF résumé first.", "error"); return; }
    const maxBytes = numberValue(records(config.feature_flags).max_resume_bytes, 6 * 1024 * 1024);
    if (!file.name.toLowerCase().endsWith(".pdf") || file.type && file.type !== "application/pdf") { notify("Only PDF résumés are accepted.", "error"); return; }
    if (file.size <= 0 || file.size > maxBytes) { notify(`The résumé must be smaller than ${bytesLabel(maxBytes)}.`, "error"); return; }
    setBusy("upload");
    const bucket = stringValue(records(config.feature_flags).resume_bucket, "resumes");
    let path = ""; let uploaded = false; let registered = false;
    try {
      const { data: listing, error: listingError } = await client.storage.from(bucket).list(client.auth.getUser ? (await client.auth.getUser()).data.user?.id || "" : "", { limit: 10, sortBy: { column: "name", order: "asc" } });
      if (listingError) throw new Error(listingError.message);
      const occupied = new Set((listing || []).map((entry) => entry.name));
      const slot = [1, 2, 3, 4, 5].find((candidate) => !occupied.has(`resume-${candidate}.pdf`));
      const userId = (await client.auth.getUser()).data.user?.id;
      if (!slot || !userId) throw new Error("Résumé storage is full or your session expired.");
      path = `${userId}/resume-${slot}.pdf`;
      const uploadedFile = await client.storage.from(bucket).upload(path, file, { contentType: "application/pdf", upsert: false, cacheControl: "3600" });
      if (uploadedFile.error) throw new Error(uploadedFile.error.message);
      uploaded = true;
      const result = await apiRequest<{ data: Resume }>(client, "/resumes/register", { method: "POST", body: { storage_path: path, original_name: file.name, mime_type: "application/pdf", size_bytes: file.size, sha256: await hashFile(file) } });
      registered = true; const resume = unwrapData(result); setFile(null); await refresh();
      if (resume?.id) { const parsed = await apiRequest<{ data: Resume; suggestions?: Record<string, unknown> }>(client, `/resumes/${encodeURIComponent(resume.id)}/parse`, { method: "POST" }); setSuggestions(parsed.suggestions || null); await refresh(); }
      notify("Résumé uploaded and parsed. Review the extracted fields below.", "success");
    } catch (error) { if (uploaded && !registered && path) await client.storage.from(bucket).remove([path]); notify(errorMessage(error, "The résumé could not be uploaded."), "error"); }
    finally { setBusy(""); }
  };
  const parse = async (resume: Resume) => { setBusy(`parse-${resume.id}`); try { const result = await apiRequest<{ suggestions?: Record<string, unknown> }>(client, `/resumes/${encodeURIComponent(resume.id)}/parse`, { method: "POST" }); setSuggestions(result.suggestions || null); await refresh(); notify("Résumé parsed successfully.", "success"); } catch (error) { notify(errorMessage(error, "The résumé could not be parsed."), "error"); } finally { setBusy(""); } };
  const analyze = async (resume: Resume) => { setBusy(`analyze-${resume.id}`); try { const result = await apiRequest<{ data?: { suggestions?: Record<string, unknown> } }>(client, `/resumes/${encodeURIComponent(resume.id)}/analyze`, { method: "POST" }); setSuggestions(result.data?.suggestions || null); notify("Groq analysis is ready for review; nothing was saved automatically.", "success"); } catch (error) { notify(errorMessage(error, "Add a valid Groq key before analyzing."), "error"); } finally { setBusy(""); } };
  const remove = async (resume: Resume) => { if (!window.confirm(`Delete ${resume.original_name || "this résumé"}?`)) return; setBusy(`delete-${resume.id}`); try { await apiRequest(client, `/resumes/${encodeURIComponent(resume.id)}`, { method: "DELETE" }); setResumes((current) => current.filter((item) => item.id !== resume.id)); notify("Résumé deleted.", "success"); } catch (error) { notify(errorMessage(error, "The résumé could not be deleted."), "error"); } finally { setBusy(""); } };
  const applySuggestions = () => { if (!suggestions) return; setDraft((current) => { const next = { ...current }; for (const key of ["full_name", "email", "phone", "location", "headline", "summary", "years_experience", "college", "degree", "graduation_year"] as const) { const value = suggestions[key]; if (value !== undefined && value !== null && !current[key]) next[key] = value as never; } if (Array.isArray(suggestions.skills) && !current.skills?.length) next.skills = suggestions.skills as string[]; return next; }); notify("Blank fields filled from deterministic résumé evidence. Review before saving.", "info"); };
  const parsed = resumes.find((resume) => resume.parse_status === "parsed");
  return <div className="aa-stack"><SectionHeader eyebrow="Applicant foundation" title="Profile & résumé" text="Your profile is the source of truth for matching. A résumé upload is parsed privately; extracted values stay suggestions until you review and save them." />
    <div className="aa-two-column aa-profile-layout"><section className="aa-panel"><SectionHeader eyebrow="Private document" title="Active résumé" text="PDF only · maximum five files · stored in your private Supabase bucket." /><form className="aa-upload" onSubmit={upload}><label className="aa-dropzone"><input type="file" accept="application/pdf,.pdf" onChange={(event) => setFile(event.target.files?.[0] || null)} /><span className="aa-upload-icon">↑</span><strong>{file ? file.name : "Choose a PDF résumé"}</strong><small>{file ? bytesLabel(file.size) : "The picker is contained inside this card."}</small></label><Button type="submit" busy={busy === "upload"} className="aa-button-primary">Upload & parse</Button></form><div className="aa-document-list">{resumes.map((resume) => <div className="aa-document" key={resume.id}><span className="aa-file-badge">PDF</span><div><strong>{resume.original_name || "Résumé.pdf"}</strong><small>{bytesLabel(resume.size_bytes)} · {dateLabel(resume.created_at)}</small></div><Status value={resume.parse_status} /><div className="aa-inline-actions">{resume.parse_status !== "parsed" && <Button className="aa-button-link" busy={busy === `parse-${resume.id}`} onClick={() => void parse(resume)}>Parse</Button>}{resume.parse_status === "parsed" && <Button className="aa-button-link" busy={busy === `analyze-${resume.id}`} onClick={() => void analyze(resume)}>Analyze with Groq</Button>}<Button className="aa-button-danger-link" busy={busy === `delete-${resume.id}`} onClick={() => void remove(resume)}>Delete</Button></div></div>)}{!resumes.length && <Empty title="No résumé uploaded" text="Upload one to unlock accurate experience handling and fit scoring." />}</div></section><section className="aa-panel"><SectionHeader eyebrow="Profile evidence" title="Save the details you want applications to use" text="Manual profile fields remain useful for links, preferences, and facts that a résumé cannot safely infer." /><form className="aa-form" onSubmit={save}><div className="aa-field-grid"><Field label="Full name" value={stringValue(draft.full_name)} onChange={(value) => update("full_name", value)} required /><Field label="Application email" value={stringValue(draft.email)} onChange={(value) => update("email", value)} type="email" required /><Field label="Phone" value={stringValue(draft.phone)} onChange={(value) => update("phone", value)} /><Field label="Location" value={stringValue(draft.location)} onChange={(value) => update("location", value)} /><Field label="Headline" value={stringValue(draft.headline)} onChange={(value) => update("headline", value)} /><Field label="Years of professional experience" value={draft.years_experience ?? ""} onChange={(value) => update("years_experience", value ? Number(value) : null)} type="number" min={0} max={80} step={0.1} /><Field label="College" value={stringValue(draft.college)} onChange={(value) => update("college", value)} /><Field label="Graduation year" value={draft.graduation_year ?? ""} onChange={(value) => update("graduation_year", value ? Number(value) : null)} type="number" min={1950} max={2100} /></div><label className="aa-field"><span>Summary</span><textarea value={stringValue(draft.summary)} onChange={(event) => update("summary", event.target.value)} rows={4} placeholder="A concise, truthful professional summary" /></label><label className="aa-field"><span>Skills <small>comma-separated</small></span><input value={(draft.skills || []).join(", ")} onChange={(event) => setDraft((current) => ({ ...current, skills: event.target.value.split(",").map((item) => item.trim()).filter(Boolean) }))} placeholder="Python, FastAPI, SQL" /></label><div className="aa-field-grid"><Field label="Target roles" value={stringValue(preference.target_roles, "AI Engineer, Software Engineer")} onChange={(value) => updatePreference("target_roles", value)} /><Field label="Preferred locations" value={stringValue(preference.preferred_locations, "New Delhi, India")} onChange={(value) => updatePreference("preferred_locations", value)} /></div><label className="aa-checkbox"><input type="checkbox" checked={Boolean(preference.remote)} onChange={(event) => updatePreference("remote", event.target.checked)} /><span>Include remote roles</span></label><label className="aa-field"><span>LinkedIn URL</span><input value={stringValue(draft.linkedin_url)} onChange={(event) => update("linkedin_url", event.target.value)} type="url" /></label><label className="aa-field"><span>GitHub URL</span><input value={stringValue(draft.github_url)} onChange={(event) => update("github_url", event.target.value)} type="url" /></label><div className="aa-form-actions"><Button type="submit" busy={busy === "save"} className="aa-button-primary">Save profile</Button>{suggestions && <Button type="button" onClick={applySuggestions} className="aa-button-secondary">Apply safe suggestions</Button>}</div></form>{suggestions && <div className="aa-suggestion"><strong>Résumé extraction ready for review</strong><p>Deterministic fields such as email and phone take priority. The experience estimate excludes education dates and academic projects.</p><pre>{JSON.stringify(suggestions, null, 2)}</pre></div>}{parsed && <p className="aa-muted">Active résumé parsed on {dateLabel(parsed.updated_at || parsed.created_at)}.</p>}</section></div>
  </div>;
}

function DiscoveryView({ client, profile, jobs, activity, notify, refresh, setView }: ViewProps) {
  const prefs = records(profile.preferences);
  const [location, setLocation] = useState(stringValue(prefs.preferred_locations, stringValue(profile.location, "New Delhi, India")));
  const [maxJobs, setMaxJobs] = useState("20");
  const [timeout, setTimeoutSeconds] = useState("60");
  const [remoteOnly, setRemoteOnly] = useState(Boolean(prefs.remote));
  const [keywords, setKeywords] = useState(stringValue(prefs.target_roles, "AI Engineer, Machine Learning Engineer, Software Engineer"));
  const [feedLimit, setFeedLimit] = useState("60");
  const [linkedinLocation, setLinkedinLocation] = useState("India");
  const [linkedinKeywords, setLinkedinKeywords] = useState("AI Engineer");
  const [busy, setBusy] = useState("");
  const activeRuns = activity.filter((item) => ["queued", "running"].includes(stringValue(item.status).toLowerCase())).slice(0, 5);
  const runResumeSearch = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy("resume");
    try { await apiRequest(client, "/discovery/resume-guided", { method: "POST", body: { location: location.trim() || null, remote_only: remoteOnly, linkedin_limit: Math.min(Number(maxJobs), 25), feed_limit: Math.min(Number(feedLimit), 200), max_jobs: Number(maxJobs), timeout_seconds: Number(timeout), idempotency_key: idempotencyKey("resume-search") } }); notify("Job discovery queued. You can leave this page; the worker will continue in the background.", "success"); await refresh(); } catch (error) { notify(errorMessage(error, "The discovery run could not be queued."), "error"); } finally { setBusy(""); }
  };
  const runLinkedin = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy("linkedin");
    try { await apiRequest(client, "/discovery/linkedin", { method: "POST", body: { keywords: linkedinKeywords, location: linkedinLocation, remote_only: remoteOnly, limit: Math.min(Number(maxJobs), 25), timeout_seconds: Number(timeout), idempotency_key: idempotencyKey("linkedin-search") } }); notify("LinkedIn guest discovery queued.", "success"); await refresh(); } catch (error) { notify(errorMessage(error, "The LinkedIn search could not be queued."), "error"); } finally { setBusy(""); }
  };
  const runFeeds = async () => { setBusy("feeds"); try { await apiRequest(client, "/discovery/public-feeds", { method: "POST", body: { source_ids: ["rss", "telegram"], limit: Math.min(Number(feedLimit), 200), timeout_seconds: Number(timeout), idempotency_key: idempotencyKey("feed-search") } }); notify("Public feed discovery queued.", "success"); await refresh(); } catch (error) { notify(errorMessage(error, "Public feed discovery could not be queued."), "error"); } finally { setBusy(""); } };
  const recent = jobs.filter((job) => job.source && job.source !== "manual").slice(0, 8);
  return <div className="aa-stack"><SectionHeader eyebrow="Bounded discovery" title="Find jobs without hanging the browser" text="Choose a result budget and a hard worker deadline. The request queues quickly; Vercel never waits ten minutes for scraping." action={<Button onClick={() => setView("activity")} className="aa-button-secondary">View activity</Button>} />
    <section className="aa-panel aa-discovery-primary"><div className="aa-panel-intro"><div><p className="aa-eyebrow">Best option</p><h3>Search with your résumé</h3><p>We use your résumé and profile to find matching jobs. Jobs are saved as they arrive.</p></div><span className="aa-timeout-badge">Time limit on</span></div><form className="aa-form" onSubmit={runResumeSearch}><div className="aa-field-grid"><Field label="Location" value={location} onChange={setLocation} placeholder="New Delhi, India" /><Field label="Target roles" value={keywords} onChange={setKeywords} placeholder="AI Engineer, Backend Engineer" /></div><div className="aa-field-grid aa-three-fields"><Field label="Jobs to find" value={maxJobs} onChange={setMaxJobs} type="number" min={2} max={50} /><Field label="Time limit (seconds)" value={timeout} onChange={setTimeoutSeconds} type="number" min={15} max={120} /><Field label="Feed results" value={feedLimit} onChange={setFeedLimit} type="number" min={1} max={200} /></div><label className="aa-checkbox"><input type="checkbox" checked={remoteOnly} onChange={(event) => setRemoteOnly(event.target.checked)} /><span>Remote only</span></label><div className="aa-callout"><strong>Good starting point: 20 jobs / 60 seconds</strong><span>You can leave this page. Slow sources stop at the time limit instead of leaving a spinner forever.</span></div><Button type="submit" busy={busy === "resume"} className="aa-button-primary">Find matching jobs</Button></form></section>
    <div className="aa-two-column"><section className="aa-panel"><SectionHeader eyebrow="Additional public sources" title="Search by source" text="These are optional and use the same bounded worker model." /><form className="aa-form aa-compact-form" onSubmit={runLinkedin}><div className="aa-field-grid"><Field label="Keywords" value={linkedinKeywords} onChange={setLinkedinKeywords} /><Field label="Location" value={linkedinLocation} onChange={setLinkedinLocation} /></div><Button type="submit" busy={busy === "linkedin"} className="aa-button-secondary">Search LinkedIn</Button></form><div className="aa-divider-line" /><div className="aa-source-action"><div><strong>Public job feeds</strong><small>Public feeds with no login needed.</small></div><Button onClick={() => void runFeeds()} busy={busy === "feeds"} className="aa-button-secondary">Search public feeds</Button></div><p className="aa-muted">Need more leads? Use Email leads for an AI research workbook.</p></section><section className="aa-panel"><SectionHeader eyebrow="Worker status" title="Runs that are still working" action={<span className="aa-count-badge">{activeRuns.length}</span>} />{activeRuns.map((run) => <div className="aa-run-row" key={run.id}><span className="aa-spinner aa-spinner-small" /><div><strong>{humanize(run.kind)}</strong><small>{humanize(run.status)} · started {dateTimeLabel(run.created_at)}</small></div><Status value={run.status} /></div>)}{!activeRuns.length && <Empty title="No active runs" text="Completed and failed runs remain available in Activity." action={<Button onClick={() => setView("activity")} className="aa-button-link">Open run history</Button>} />}</section></div>
    <section className="aa-panel"><SectionHeader eyebrow="Saved from discovery" title="Latest discovered roles" action={<Button onClick={() => setView("jobs")} className="aa-button-link">Open jobs library</Button>} />{recent.map((job) => <JobMini key={job.id} job={job} />)}{!recent.length && <Empty title="Nothing discovered yet" text="Queue a bounded search above. Results will appear here when the persistent worker saves them." />}</section>
  </div>;
}

function FormPilotView({ client, forms, applications, jobs, credentials, notify, refresh, setView }: ViewProps) {
  const [formUrl, setFormUrl] = useState("");
  const [busy, setBusy] = useState("");
  const [selectedApplication, setSelectedApplication] = useState<Application | null>(null);
  const [revisions, setRevisions] = useState<Record<string, unknown>[]>([]);
  const [answers, setAnswers] = useState<Record<string, unknown>>({});
  const [revisionMessage, setRevisionMessage] = useState("");
  const managed = credentials.find((item) => item.provider === "browserbase")?.configured || connectionsReadyForForm(jobs, applications);
  const scan = async (job: Job) => {
    setBusy(job.id);
    try { await apiRequest(client, `/jobs/${encodeURIComponent(job.id)}/application/scan`, { method: "POST", body: { idempotency_key: idempotencyKey("form-scan") } }); notify("Form scan queued. Watch Activity for the captured questions.", "success"); await refresh(); } catch (error) { notify(errorMessage(error, "The form scan could not be queued."), "error"); } finally { setBusy(""); }
  };
  const addForm = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!formUrl.trim()) return;
    setBusy("add");
    try { const result = await apiRequest<{ items?: Job[] }>(client, "/discovery/ats", { method: "POST", body: { urls: [formUrl.trim()] } }); const saved = unwrapItems(result)[0]; if (saved) await scan(saved); setFormUrl(""); } catch (error) { notify(errorMessage(error, "That application URL could not be added."), "error"); } finally { setBusy(""); }
  };
  const openReview = async (application: Application) => {
    setSelectedApplication(application); setBusy(`review-${application.id}`);
    try { const result = await apiRequest<{ items: Record<string, unknown>[] }>(client, `/applications/${encodeURIComponent(application.id)}/form-revisions`); const rows = unwrapItems(result); setRevisions(rows); const latest = rows[0]; setAnswers(records(latest?.answers)); } catch (error) { notify(errorMessage(error, "The form review could not be loaded."), "error"); } finally { setBusy(""); }
  };
  const suggest = async (revision: Record<string, unknown>) => { const id = stringValue(revision.id); setBusy(`suggest-${id}`); try { const result = await apiRequest<{ data?: { answers?: Record<string, unknown> } }>(client, `/application-form-revisions/${encodeURIComponent(id)}/suggest`, { method: "POST" }); setAnswers((current) => ({ ...current, ...records(result.data?.answers) })); notify("Grounded suggestions loaded. Review every answer before approval.", "success"); } catch (error) { notify(errorMessage(error, "Suggestions could not be generated."), "error"); } finally { setBusy(""); } };
  const approve = async (revision: Record<string, unknown>) => { const id = stringValue(revision.id); setBusy(`approve-${id}`); try { await apiRequest(client, `/application-form-revisions/${encodeURIComponent(id)}/approve`, { method: "POST", body: { expected_revision: numberValue(revision.revision, 1), schema_hash: stringValue(revision.schema_hash), answers } }); notify("Answers approved. The next submission action remains explicit.", "success"); await openReview(selectedApplication as Application); } catch (error) { notify(errorMessage(error, "The form revision changed; refresh and review again."), "error"); } finally { setBusy(""); } };
  const selectedJob = selectedApplication ? jobs.find((job) => job.id === selectedApplication.job_id) : null;
  return <div className="aa-stack"><SectionHeader eyebrow="Review-gated applications" title="Form Pilot" text="Capture exact provider questions in the background, review grounded answers, then approve a one-time submission." action={<Button onClick={() => setView("activity")} className="aa-button-secondary">Open activity</Button>} /><div className="aa-callout aa-callout-warning"><strong>{managed ? "Managed form connection available" : "Connect Browserbase before scanning"}</strong><span>Form Pilot only acts on a saved, supported application URL and never submits without an approved revision.</span></div><section className="aa-panel"><SectionHeader eyebrow="Add one exact form" title="Prepare an application URL" text="Paste a Google Form, YC job detail, or another supported public application URL. Search/listing pages are rejected." /><form className="aa-inline-form" onSubmit={addForm}><input type="url" value={formUrl} onChange={(event) => setFormUrl(event.target.value)} placeholder="https://forms.gle/..." required /><Button type="submit" busy={busy === "add"} className="aa-button-primary">Add & scan</Button></form></section><section className="aa-panel"><SectionHeader eyebrow="Inbox" title="Forms ready for preparation" action={<span className="aa-count-badge">{forms.length}</span>} />{forms.map((entry) => <div className="aa-list-row aa-form-row" key={entry.id}><div className="aa-list-symbol">F</div><div className="aa-list-copy"><strong>{stringValue(entry.title, "Application form")}</strong><span>{stringValue(entry.company, "Unknown company")}{entry.location ? ` · ${entry.location}` : ""}</span></div>{entry.application ? <><Status value={entry.application.status} /><Button className="aa-button-link" busy={busy === `review-${entry.application.id}`} onClick={() => void openReview(entry.application as Application)}>Review</Button></> : <Button className="aa-button-secondary" disabled={!entry.job_id} busy={busy === entry.job_id} onClick={() => { const job = jobs.find((item) => item.id === entry.job_id); if (job) void scan(job); }}>{entry.job_id ? "Prepare form" : "Save job first"}</Button>} {safeUrl(entry.apply_url) && <a className="aa-button-link" href={safeUrl(entry.apply_url) || "#"} target="_blank" rel="noreferrer">Open ↗</a>}</div>)}{!forms.length && <Empty title="No forms waiting" text="Add an exact application URL or run public discovery. Supported forms will appear here." />}</section>{selectedApplication && <section className="aa-panel aa-review-panel"><SectionHeader eyebrow="Captured revision" title={selectedJob ? `${stringValue(selectedJob.title)} · ${stringValue(selectedJob.company)}` : "Form answer review"} text="Answers are editable until you approve this exact revision." action={<Button className="aa-button-link" onClick={() => { setSelectedApplication(null); setRevisions([]); }}>Close</Button>} />{revisions.map((revision) => <div className="aa-revision" key={stringValue(revision.id)}><div className="aa-revision-header"><Status value={stringValue(revision.status)} /><span>Revision {numberValue(revision.revision, 1)}</span><Button className="aa-button-secondary" busy={busy === `suggest-${revision.id}`} onClick={() => void suggest(revision)}>Suggest grounded answers</Button></div>{Array.isArray(revision.question_schema) ? <div className="aa-answer-list">{(revision.question_schema as unknown[]).map((question, index) => { const item = records(question); const key = stringValue(item.key || item.id || item.name, `question_${index + 1}`); return <label className="aa-field" key={key}><span>{stringValue(item.label || item.title || item.text, key)}{item.required === true ? " *" : ""}</span><textarea rows={2} value={stringValue(answers[key])} onChange={(event) => setAnswers((current) => ({ ...current, [key]: event.target.value }))} /></label>; })}</div> : <p className="aa-muted">The worker has not returned a question schema yet.</p>}<Button className="aa-button-primary" busy={busy === `approve-${revision.id}`} onClick={() => void approve(revision)}>Approve this revision</Button></div>)}{!revisions.length && <Empty title="No captured revision yet" text="The worker may still be scanning. Refresh Activity when the queued run completes." />}{revisionMessage && <p className="aa-muted">{revisionMessage}</p>}</section>}</div>;
}

function connectionsReadyForForm(_jobs: Job[], _applications: Application[]): boolean { return false; }

function JobsView({ client, jobs, setJobs, notify, refresh, setView }: ViewProps) {
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState("fit");
  const [busy, setBusy] = useState("");
  const filtered = useMemo(() => jobs.filter((job) => `${job.title} ${job.company} ${job.location}`.toLowerCase().includes(search.toLowerCase()) && job.status !== "archived").sort((left, right) => sort === "fit" ? numberValue(right.fit?.score) - numberValue(left.fit?.score) : new Date(stringValue(right.created_at)).valueOf() - new Date(stringValue(left.created_at)).valueOf()), [jobs, search, sort]);
  const draft = async (job: Job) => { setBusy(`draft-${job.id}`); try { await apiRequest(client, `/jobs/${encodeURIComponent(job.id)}/draft`, { method: "POST" }); await refresh(); notify("Grounded draft created. Review it before approving.", "success"); setView("applications"); } catch (error) { notify(errorMessage(error, "The draft could not be generated. Add Groq in Connections."), "error"); } finally { setBusy(""); } };
  const archive = async (job: Job) => { setBusy(`archive-${job.id}`); try { const result = await apiRequest<{ data: Job }>(client, `/jobs/${encodeURIComponent(job.id)}`, { method: "PATCH", body: { status: "archived" } }); const updated = unwrapData(result); if (updated) setJobs((current) => current.map((item) => item.id === job.id ? updated : item)); notify("Role archived.", "success"); } catch (error) { notify(errorMessage(error, "The role could not be archived."), "error"); } finally { setBusy(""); } };
  return <div className="aa-stack"><SectionHeader eyebrow="Opportunity library" title="Jobs" text="Every role here came from a bounded discovery run or an imported research workbook. There is no duplicate manual job form." action={<Button onClick={() => setView("outreach")} className="aa-button-primary">Import research workbook</Button>} /><section className="aa-panel"><div className="aa-toolbar"><input className="aa-search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search roles, companies, locations" /><select value={sort} onChange={(event) => setSort(event.target.value)}><option value="fit">Strongest fit first</option><option value="newest">Newest first</option></select><span className="aa-count-badge">{filtered.length} roles</span></div>{filtered.map((job) => <JobCard key={job.id} job={job} draft={() => void draft(job)} archive={() => void archive(job)} busy={busy} setView={setView} />)}{!filtered.length && <Empty title={jobs.length ? "No roles match" : "No saved jobs"} text={jobs.length ? "Try another search." : "Queue discovery or import a verified workbook from Email outreach."} action={<Button onClick={() => setView("discovery")} className="aa-button-secondary">Find jobs</Button>} />}</section></div>;
}

function JobCard({ job, draft, archive, busy, setView }: { job: Job; draft: () => void; archive: () => void; busy: string; setView: (view: View) => void }) {
  const score = numberValue(job.fit?.score);
  return <article className="aa-job-card"><div className="aa-job-header"><div><h3>{stringValue(job.title, "Untitled role")}</h3><p>{stringValue(job.company, "Unknown company")}</p></div><Status value={job.status} /></div><div className="aa-job-meta"><span>{job.location || "Location not stated"}</span><span>{job.source || "imported"}</span><span>{dateLabel(job.created_at)}</span>{score > 0 && <span className="aa-fit-pill">{score}% fit</span>}</div><div className="aa-fit-meter"><span style={{ width: `${Math.min(100, Math.max(0, score))}%` }} /></div><p className="aa-job-description">{stringValue(job.description, "No description was included with this role.").slice(0, 420)}{stringValue(job.description).length > 420 ? "…" : ""}</p><div className="aa-card-actions"><Button onClick={draft} busy={busy === `draft-${job.id}`} className="aa-button-primary">Draft with Groq</Button>{safeUrl(job.apply_url) && <a className="aa-button-secondary" href={safeUrl(job.apply_url) || "#"} target="_blank" rel="noreferrer">Open job ↗</a>}<Button onClick={() => setView("outreach")} className="aa-button-link">Add to outreach</Button><Button onClick={archive} busy={busy === `archive-${job.id}`} className="aa-button-danger-link">Archive</Button></div></article>;
}

function ApplicationsView({ client, applications, jobs, setApplications, notify, refresh }: ViewProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Application | null>(null);
  const [busy, setBusy] = useState("");
  const [filter, setFilter] = useState("all");
  useEffect(() => { if (selectedId) setDraft(applications.find((item) => item.id === selectedId) || null); }, [applications, selectedId]);
  const rows = applications.filter((item) => filter === "all" || item.status === filter);
  const save = async (event: React.FormEvent) => { event.preventDefault(); if (!draft) return; setBusy("save"); try { const result = await apiRequest<{ data: Application }>(client, `/applications/${encodeURIComponent(draft.id)}`, { method: "PATCH", body: { recipient: draft.recipient || null, subject: draft.subject || null, body: draft.body || null } }); const updated = unwrapData(result); if (updated) { setApplications((current) => current.map((item) => item.id === updated.id ? updated : item)); setDraft(updated); } notify("Draft saved.", "success"); } catch (error) { notify(errorMessage(error, "Draft could not be saved."), "error"); } finally { setBusy(""); } };
  const approve = async () => { if (!draft) return; setBusy("approve"); try { const result = await apiRequest<{ data: Application }>(client, `/applications/${encodeURIComponent(draft.id)}/approve`, { method: "POST", body: { expected_revision: numberValue(draft.revision, 1) } }); const updated = unwrapData(result); if (updated) { setApplications((current) => current.map((item) => item.id === updated.id ? updated : item)); setDraft(updated); } notify("Message approved. Sending remains a separate explicit action.", "success"); } catch (error) { notify(errorMessage(error, "The draft changed; refresh and review it again."), "error"); } finally { setBusy(""); } };
  const send = async () => { if (!draft) return; if (!window.confirm("Queue this approved message for the persistent Gmail worker?")) return; setBusy("send"); try { await apiRequest(client, `/applications/${encodeURIComponent(draft.id)}/send`, { method: "POST", body: { idempotency_key: idempotencyKey("email-send"), attach_resume: true } }); await refresh(); notify("Message queued for the persistent Gmail worker.", "success"); } catch (error) { notify(errorMessage(error, "The message could not be queued."), "error"); } finally { setBusy(""); } };
  const selectedJob = draft ? jobs.find((job) => job.id === draft.job_id) : null;
  return <div className="aa-stack"><SectionHeader eyebrow="Review desk" title="Applications" text="Edit, approve, and queue one exact message at a time. Approved email delivery is handled by the persistent worker outside Vercel." /><div className="aa-review-layout"><section className="aa-panel"><div className="aa-toolbar"><select value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">All applications</option>{["draft_pending", "drafted", "approved", "queued", "applied", "rejected"].map((item) => <option key={item} value={item}>{humanize(item)}</option>)}</select><span className="aa-count-badge">{rows.length}</span></div>{rows.map((application) => <button className={`aa-application-row ${selectedId === application.id ? "aa-row-active" : ""}`} key={application.id} onClick={() => setSelectedId(application.id)}><div className="aa-list-symbol">@</div><div className="aa-list-copy"><strong>{stringValue(application.subject, "Untitled application")}</strong><span>{stringValue(application.recipient, "Recipient not selected")}</span></div><Status value={application.status} /></button>)}{!rows.length && <Empty title="No applications" text="Create a grounded draft from the Jobs library or Outreach." />}</section><section className="aa-panel aa-editor">{draft ? <><SectionHeader eyebrow="Exact message review" title={stringValue(selectedJob?.title, "Application draft")} text={stringValue(selectedJob?.company, "") + (selectedJob?.location ? ` · ${selectedJob.location}` : "")} /><form className="aa-form" onSubmit={save}><Field label="Recipient" value={stringValue(draft.recipient)} onChange={(value) => setDraft({ ...draft, recipient: value })} type="email" /><Field label="Subject" value={stringValue(draft.subject)} onChange={(value) => setDraft({ ...draft, subject: value })} /><label className="aa-field"><span>Message</span><textarea rows={15} value={stringValue(draft.body)} onChange={(event) => setDraft({ ...draft, body: event.target.value })} /></label><div className="aa-form-actions"><Button type="submit" busy={busy === "save"} className="aa-button-secondary">Save edits</Button>{draft.status !== "approved" && <Button type="button" busy={busy === "approve"} onClick={() => void approve()} className="aa-button-primary">Approve exact draft</Button>}{draft.status === "approved" && <Button type="button" busy={busy === "send"} onClick={() => void send()} className="aa-button-primary">Queue Gmail send</Button>}</div></form></> : <Empty title="Choose a message" text="Select an application to review its exact recipient, subject, and body." />}</section></div></div>;
}

function OutreachView({ client, profile, jobs, applications, notify, refresh, setView }: ViewProps) {
  const [selected, setSelected] = useState<string[]>([]);
  const [contacts, setContacts] = useState<Record<string, Contact[]>>({});
  const [chosen, setChosen] = useState<Record<string, string>>({});
  const [prompt, setPrompt] = useState<ResearchPrompt | null>(null);
  const [targetRole, setTargetRole] = useState("");
  const [location, setLocation] = useState(stringValue(profile.location, "New Delhi, India"));
  const [remoteOnly, setRemoteOnly] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState("");
  const candidates = useMemo(() => jobs.filter((job) => job.status !== "archived" && job.company).sort((left, right) => numberValue(right.fit?.score) - numberValue(left.fit?.score)).slice(0, 50), [jobs]);
  const appFor = (jobId: string) => applications.filter((item) => item.job_id === jobId && item.channel === "email").sort((left, right) => new Date(stringValue(right.updated_at || right.created_at)).valueOf() - new Date(stringValue(left.updated_at || left.created_at)).valueOf())[0];
  const toggle = (id: string) => setSelected((current) => current.includes(id) ? current.filter((item) => item !== id) : current.length >= 30 ? current : [...current, id]);
  const generatePrompt = async (event: React.FormEvent) => { event.preventDefault(); setBusy("prompt"); try { const result = await apiRequest<{ data: ResearchPrompt }>(client, "/outreach/research-prompt", { method: "POST", body: { target_role: targetRole || null, location: location || null, remote_only: remoteOnly } }); setPrompt(unwrapData(result)); notify("Strict research brief generated. Copy it into Claude, ChatGPT, or Gemini.", "success"); } catch (error) { notify(errorMessage(error, "Upload and parse a résumé before generating a research prompt."), "error"); } finally { setBusy(""); } };
  const importWorkbook = async (event: React.FormEvent) => { event.preventDefault(); if (!file) { notify("Choose the completed CSV or XLSX first.", "error"); return; } setBusy("import"); try { const result = await apiRequest<{ count?: number }>(client, "/discovery/import", { method: "POST", file }); setFile(null); await refresh(); notify(`${numberValue(result.count)} research rows imported into Jobs.`, "success"); } catch (error) { notify(errorMessage(error, "The workbook could not be imported."), "error"); } finally { setBusy(""); } };
  const findContacts = async () => { if (!selected.length) { notify("Select at least one role first.", "error"); return; } setBusy("contacts"); const result = await Promise.allSettled(selected.map(async (id) => { const response = await apiRequest<{ data?: { contacts?: Contact[] } }>(client, `/jobs/${encodeURIComponent(id)}/contacts/public`, { method: "POST" }); return [id, unwrapData(response)?.contacts || []] as const; })); const next = { ...contacts }; let found = 0; result.forEach((item) => { if (item.status === "fulfilled") { next[item.value[0]] = item.value[1]; found += item.value[1].length; } }); setContacts(next); setBusy(""); notify(`${found} public contact lead${found === 1 ? "" : "s"} loaded. Review each one before drafting.`, found ? "success" : "info"); };
  const selectedContact = (job: Job) => chosen[job.id] || job.contact_email || contacts[job.id]?.[0]?.email || "";
  const createDrafts = async () => { const selectedJobs = selected.map((id) => jobs.find((job) => job.id === id)).filter((job): job is Job => Boolean(job)); const missing = selectedJobs.filter((job) => !selectedContact(job)); if (!selectedJobs.length || missing.length) { notify("Select a public contact for every chosen role before drafting.", "error"); return; } setBusy("drafts"); let count = 0; for (const job of selectedJobs) { try { await apiRequest(client, `/jobs/${encodeURIComponent(job.id)}`, { method: "PATCH", body: { contact_email: selectedContact(job) } }); await apiRequest(client, `/jobs/${encodeURIComponent(job.id)}/draft`, { method: "POST" }); count += 1; } catch (error) { notify(`${job.company}: ${errorMessage(error, "draft failed")}`, "error"); } } await refresh(); setBusy(""); notify(`${count} grounded draft${count === 1 ? "" : "s"} ready for review.`, count ? "success" : "error"); setView("applications"); };
  const sendApproved = async () => { const approved = selected.map((id) => appFor(id)).filter((app): app is Application => Boolean(app && app.status === "approved" && app.recipient)); if (!approved.length) { notify("Approve at least one selected draft before sending.", "error"); return; } if (!window.confirm(`Queue ${approved.length} approved email${approved.length === 1 ? "" : "s"} for Gmail?`)) return; setBusy("send"); try { const result = await apiRequest<{ count?: number }>(client, "/applications/send-batch", { method: "POST", body: { application_ids: approved.slice(0, 30).map((app) => app.id), attach_resume: true, idempotency_key: idempotencyKey("outreach-batch") } }); await refresh(); notify(`${numberValue(result.count, approved.length)} message${approved.length === 1 ? "" : "s"} queued for the persistent Gmail worker.`, "success"); } catch (error) { notify(errorMessage(error, "The email batch could not be queued."), "error"); } finally { setBusy(""); } };
  const copyPrompt = async () => { const value = stringValue(prompt?.prompt); if (!value) return; await navigator.clipboard.writeText(value); notify("Research brief copied to your clipboard.", "success"); };
  return <div className="aa-stack"><SectionHeader eyebrow="Review-gated outreach" title="Email outreach" text="Generate a strict external research brief, import the completed workbook, choose up to 30 roles in order, then draft and send only after review." action={<Button onClick={() => setView("applications")} className="aa-button-secondary">Review & send</Button>} /><div className="aa-prerequisites"><span className="aa-prereq aa-prereq-ready">✓ Résumé parsed</span><span className="aa-prereq">○ Groq key · Profile or Connections</span><span className="aa-prereq">○ Gmail connected</span></div><section className="aa-panel"><SectionHeader eyebrow="Step 1 · External AI research" title="Generate the 100-lead research brief" text="The output targets 100 distinct email leads across at least 25 companies, with no more than four contacts per company. Contacts are not necessarily HR: recruiters, founders, engineering leaders, team members, or company inboxes are valid; source-verified status is preferred but not required." /><form className="aa-form" onSubmit={generatePrompt}><div className="aa-field-grid"><Field label="Target role (optional)" value={targetRole} onChange={setTargetRole} placeholder="AI Engineer or Backend Engineer" /><Field label="Location" value={location} onChange={setLocation} placeholder="New Delhi, India" /></div><label className="aa-checkbox"><input type="checkbox" checked={remoteOnly} onChange={(event) => setRemoteOnly(event.target.checked)} /><span>Remote roles only</span></label><Button type="submit" busy={busy === "prompt"} className="aa-button-primary">Generate 100-lead prompt</Button></form>{prompt?.prompt && <div className="aa-prompt-output"><div className="aa-section-header"><div><strong>Copy into Claude, ChatGPT, or Gemini</strong><small>{stringValue(prompt.summary, "100-lead research brief")}</small></div><Button onClick={() => void copyPrompt()} className="aa-button-secondary">Copy prompt</Button></div><textarea value={prompt.prompt} readOnly rows={18} aria-label="External AI research prompt" /></div>}</section><section className="aa-panel"><SectionHeader eyebrow="Step 2 · Import" title="Upload the completed workbook" text="This picker is a normal contained React control. It does not open an overlay or mutate page scroll. Email leads may be source-verified or source-unverified; both remain review-gated before drafting." /><form className="aa-import-card" onSubmit={importWorkbook}><label className="aa-dropzone aa-dropzone-small"><input type="file" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={(event) => setFile(event.target.files?.[0] || null)} /><span className="aa-upload-icon">↑</span><strong>{file ? file.name : "Choose completed CSV or XLSX"}</strong><small>{file ? bytesLabel(file.size) : "The prompt creates the workbook; upload it here when ready."}</small></label><Button type="submit" busy={busy === "import"} className="aa-button-primary">Import into Jobs</Button></form></section><section className="aa-panel"><div className="aa-step-heading"><span>3</span><div><p className="aa-eyebrow">Choose deliberately</p><h3>Select relevant jobs</h3><p>Selection order is preserved. Maximum 30 roles per batch.</p></div><span className="aa-count-badge">{selected.length} / 30</span></div><div className="aa-outreach-list">{candidates.map((job) => { const index = selected.indexOf(job.id); const app = appFor(job.id); return <label className={`aa-outreach-row ${index >= 0 ? "aa-row-active" : ""}`} key={job.id}><input type="checkbox" checked={index >= 0} onChange={() => toggle(job.id)} /><span className="aa-order">{index >= 0 ? String(index + 1).padStart(2, "0") : "·"}</span><span className="aa-list-copy"><strong>{stringValue(job.title, "Untitled role")}</strong><span>{stringValue(job.company)}{job.location ? ` · ${job.location}` : ""}</span></span>{job.fit?.score ? <span className="aa-fit-pill">{job.fit.score}% fit</span> : <Status value={app?.status || job.status} />}</label>; })}{!candidates.length && <Empty title="No imported roles" text="Generate the prompt, finish the workbook in your external AI, then upload it above." />}</div></section><section className="aa-panel"><div className="aa-step-heading"><span>4</span><div><p className="aa-eyebrow">Review contacts</p><h3>Email leads</h3><p>Contacts can be any relevant company lead, not only HR. Nothing is sent until you approve the draft.</p></div></div><div className="aa-button-row"><Button onClick={() => void findContacts()} busy={busy === "contacts"} disabled={!selected.length} className="aa-button-primary">Find contacts</Button><Button onClick={() => void createDrafts()} busy={busy === "drafts"} disabled={!selected.length} className="aa-button-secondary">Create Groq drafts</Button><Button onClick={() => void sendApproved()} busy={busy === "send"} disabled={!selected.length} className="aa-button-secondary">Queue approved emails</Button></div><div className="aa-contact-list">{selected.map((id) => { const job = jobs.find((item) => item.id === id); if (!job) return null; const found = contacts[id] || []; return <div className="aa-contact-row" key={id}><div className="aa-list-copy"><strong>{stringValue(job.company)}</strong><span>{found.length ? `${found.length} email lead${found.length === 1 ? "" : "s"}` : "Not searched yet"}</span></div>{found.length ? <select value={chosen[id] || ""} onChange={(event) => setChosen((current) => ({ ...current, [id]: event.target.value }))}><option value="">Choose an email</option>{found.map((contact) => <option value={contact.email} key={`${contact.email}-${contact.name}`}>{contact.email}{contact.name ? ` · ${contact.name}` : ""}</option>)}</select> : <span className="aa-muted">{job.contact_email || "No email lead"}</span>}</div>; })}{!selected.length && <Empty title="Select jobs first" text="Choose the strongest roles above, then review their email leads here." />}</div></section></div>;
}

function ConnectionsView({ client, connections, credentials, notify, refresh }: ViewProps) {
  const [busy, setBusy] = useState("");
  const [provider, setProvider] = useState("groq");
  const [apiKey, setApiKey] = useState("");
  const [projectId, setProjectId] = useState("");
  const [oauthClientId, setOauthClientId] = useState("");
  const [oauthSecret, setOauthSecret] = useState("");
  const [oauthStatus, setOauthStatus] = useState<Record<string, unknown>>({});
  const credential = credentials.find((item) => item.provider === provider);
  const gmail = connections.find((item) => item.id === "gmail");
  const saveCredential = async (event: React.FormEvent) => { event.preventDefault(); setBusy("credential"); try { await apiRequest(client, `/provider-credentials/${encodeURIComponent(provider)}`, { method: "PUT", body: { api_key: apiKey, ...(provider === "browserbase" ? { project_id: projectId } : {}) } }); setApiKey(""); setProjectId(""); await refresh(); notify(`${humanize(provider)} credential saved and validated.`, "success"); } catch (error) { notify(errorMessage(error, "Credential validation failed."), "error"); } finally { setBusy(""); } };
  const disconnect = async (id: string) => { if (!window.confirm(`Disconnect ${humanize(id)}?`)) return; setBusy(`disconnect-${id}`); try { await apiRequest(client, `/connections/${encodeURIComponent(id)}`, { method: "DELETE" }); await refresh(); notify(`${humanize(id)} disconnected.`, "success"); } catch (error) { notify(errorMessage(error, "The connection could not be removed."), "error"); } finally { setBusy(""); } };
  const connectGmail = async (source: "platform" | "user") => { setBusy(`gmail-${source}`); try { const result = await apiRequest<{ authorization_url?: string }>(client, `/oauth/google/start?return_path=${encodeURIComponent("/?view=connections")}`, { method: "POST", body: { credential_source: source } }); if (result.authorization_url) window.location.assign(result.authorization_url); } catch (error) { notify(errorMessage(error, "Gmail OAuth could not start."), "error"); setBusy(""); } };
  const saveOauthClient = async (event: React.FormEvent) => { event.preventDefault(); setBusy("oauth"); try { const result = await apiRequest<{ data?: Record<string, unknown> }>(client, "/connections/google-oauth-client", { method: "PUT", body: { client_id: oauthClientId, client_secret: oauthSecret } }); setOauthStatus(unwrapData(result) || {}); setOauthClientId(""); setOauthSecret(""); notify("Your Google OAuth app was encrypted and saved on the server.", "success"); } catch (error) { notify(errorMessage(error, "The Google OAuth app could not be saved."), "error"); } finally { setBusy(""); } };
  const browserLogin = async (id: string) => { setBusy(`browser-${id}`); try { const result = await apiRequest<{ data?: { live_view_url?: string } }>(client, `/connections/${encodeURIComponent(id)}/browser/start`, { method: "POST" }); const url = unwrapData(result)?.live_view_url; if (url) window.open(url, "autoapply-browser-login", "noopener,noreferrer"); notify(`Finish ${humanize(id)} login in the secure browser, then refresh Connections.`, "info"); } catch (error) { notify(errorMessage(error, "Secure browser login could not start."), "error"); } finally { setBusy(""); } };
  const callbackUri = `${typeof window === "undefined" ? "https://your-domain.example" : window.location.origin}/api/v1/oauth/google/callback`;
  return <div className="aa-stack"><SectionHeader eyebrow="Service connections" title="Connections" text="Keep provider credentials encrypted on the server. Secrets are never returned to the browser after saving." /><div className="aa-two-column"><section className="aa-panel"><SectionHeader eyebrow="BYOK vault" title="Groq and Browserbase" text="Groq powers grounded drafts and résumé analysis. Browserbase powers managed form workflows when enabled." /><div className="aa-toggle"><button className={provider === "groq" ? "active" : ""} onClick={() => setProvider("groq")}>Groq</button><button className={provider === "browserbase" ? "active" : ""} onClick={() => setProvider("browserbase")}>Browserbase</button></div><form className="aa-form" onSubmit={saveCredential}><label className="aa-field"><span>{humanize(provider)} API key</span><input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={credential?.key_hint || "Paste a key; it is not stored in this page"} required /></label>{provider === "browserbase" && <Field label="Project ID" value={projectId} onChange={setProjectId} placeholder={credential?.project_id_hint || "Browserbase project ID"} required />}{provider === "groq" ? <a href="https://console.groq.com/keys" target="_blank" rel="noreferrer">Open Groq keys ↗</a> : <div className="aa-link-row"><a href="https://www.browserbase.com/settings" target="_blank" rel="noreferrer">Settings ↗</a><a href="https://www.browserbase.com/overview" target="_blank" rel="noreferrer">Project ↗</a></div>}<Button type="submit" busy={busy === "credential"} className="aa-button-primary">Save & validate</Button></form>{credential && <div className="aa-credential-status"><Status value={credential.verification_status} /><span>{credential.configured ? `Saved ${credential.key_hint || "credential"}` : "Not configured"}</span>{credential.requires_reconfiguration && <small>Replace this credential before using the provider.</small>}</div>}</section><section className="aa-panel"><SectionHeader eyebrow="Gmail delivery" title="Connect Gmail" text="Gmail is used only after you approve exact messages. Daily sending is capped in Settings." /><div className="aa-connection-card"><div className="aa-list-symbol">G</div><div className="aa-list-copy"><strong>{gmail?.label || "Gmail"}</strong><span>{gmail?.connection && records(gmail.connection).display_name ? stringValue(records(gmail.connection).display_name) : "Not connected"}</span></div><Status value={gmail?.connection ? stringValue(records(gmail.connection).status) : "not connected"} />{gmail?.connection ? <Button className="aa-button-danger-link" busy={busy === "disconnect-gmail"} onClick={() => void disconnect("gmail")}>Disconnect</Button> : <div className="aa-button-row"><Button className="aa-button-primary" busy={busy === "gmail-platform"} onClick={() => void connectGmail("platform")}>Connect platform Gmail</Button><Button className="aa-button-secondary" busy={busy === "gmail-user"} onClick={() => void connectGmail("user")}>Connect my OAuth app</Button></div>}</div><div className="aa-oauth-client"><h3>Use your own Google OAuth app</h3><p>For BYOC Gmail, create a Google Web OAuth client and add the callback URI below. The secret is encrypted server-side and never saved in this browser.</p><code>{callbackUri}</code><form className="aa-form" onSubmit={saveOauthClient}><Field label="Client ID" value={oauthClientId} onChange={setOauthClientId} placeholder="…apps.googleusercontent.com" required /><label className="aa-field"><span>Client secret</span><input type="password" value={oauthSecret} onChange={(event) => setOauthSecret(event.target.value)} required /></label><Button type="submit" busy={busy === "oauth"} className="aa-button-secondary">Save OAuth app</Button></form>{Object.keys(oauthStatus).length > 0 && <p className="aa-muted">OAuth app saved: {stringValue(oauthStatus.client_id_hint, "configured")}</p>}</div></section></div><section className="aa-panel"><SectionHeader eyebrow="Managed browser providers" title="Secure provider login" text="These sessions are isolated and time-limited. Connect only providers you actively use." />{connections.filter((item) => item.id !== "gmail").map((item) => <div className="aa-connection-row" key={item.id}><div className="aa-list-copy"><strong>{stringValue(item.label, humanize(item.id))}</strong><span>{stringValue(item.description, "Managed browser connection")}</span></div><Status value={item.connection ? stringValue(records(item.connection).status) : "not connected"} />{item.connection ? <Button className="aa-button-danger-link" onClick={() => void disconnect(item.id)}>Disconnect</Button> : <Button className="aa-button-secondary" busy={busy === `browser-${item.id}`} onClick={() => void browserLogin(item.id)}>Open secure login</Button>}</div>)}</section></div>;
}

function ActivityView({ client, activity, setActivity, notify, refresh }: ViewProps) {
  const [busy, setBusy] = useState("");
  const cancel = async (job: AutomationJob) => { if (!window.confirm("Cancel this queued run?")) return; setBusy(job.id); try { await apiRequest(client, `/automation-jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST" }); setActivity((current) => current.map((item) => item.id === job.id ? { ...item, status: "cancelled" } : item)); notify("Run cancelled.", "success"); } catch (error) { notify(errorMessage(error, "The run could not be cancelled."), "error"); } finally { setBusy(""); } };
  return <div className="aa-stack"><SectionHeader eyebrow="Durable worker history" title="Activity" text="Discovery, form scans, and email delivery continue in the persistent worker even after a Vercel request ends." action={<Button onClick={() => void refresh()} className="aa-button-secondary">Refresh runs</Button>} /><section className="aa-panel"><div className="aa-run-table">{activity.map((job) => <div className="aa-activity-row" key={job.id}><span className="aa-activity-icon">{stringValue(job.kind, "?").slice(0, 1).toUpperCase()}</span><div className="aa-list-copy"><strong>{humanize(job.kind)}</strong><span>{humanize(job.provider)} · created {dateTimeLabel(job.created_at)}</span>{job.error && <small className="aa-error-text">{job.error}</small>}</div><Status value={job.status} /><div className="aa-inline-actions">{["queued", "running"].includes(stringValue(job.status)) && <Button className="aa-button-danger-link" busy={busy === job.id} onClick={() => void cancel(job)}>Cancel</Button>}</div></div>)}{!activity.length && <Empty title="No background work yet" text="Discovery, Form Pilot, and email sends will appear here." />}</div></section></div>;
}

function SettingsView({ client, settings, setSettings, session, notify }: ViewProps) {
  const [draft, setDraft] = useState<Settings>(settings); const [busy, setBusy] = useState(""); const [deleteEmail, setDeleteEmail] = useState(""); const [confirmation, setConfirmation] = useState("");
  useEffect(() => setDraft(settings), [settings]);
  const save = async (event: React.FormEvent) => { event.preventDefault(); setBusy("save"); try { const result = await apiRequest<{ data: Settings }>(client, "/settings", { method: "PATCH", body: { daily_send_cap: Number(draft.daily_send_cap), duplicate_window_days: Number(draft.duplicate_window_days), timezone: draft.timezone, require_review: true } }); setSettings(unwrapData(result) || {}); notify("Settings saved.", "success"); } catch (error) { notify(errorMessage(error, "Settings could not be saved."), "error"); } finally { setBusy(""); } };
  const deleteAccount = async (event: React.FormEvent) => { event.preventDefault(); if (deleteEmail.trim().toLowerCase() !== (session.user.email || "").toLowerCase() || confirmation !== "DELETE") { notify("Enter your signed-in email and DELETE exactly.", "error"); return; } if (!window.confirm("Permanently delete this account and all workspace data?")) return; setBusy("delete"); try { await apiRequest(client, "/account", { method: "DELETE", body: { confirmation: "DELETE" }, retry: false }); await client.auth.signOut(); } catch (error) { notify(errorMessage(error, "Account deletion could not complete. Sign in again and retry."), "error"); } finally { setBusy(""); } };
  return <div className="aa-stack"><SectionHeader eyebrow="Workspace controls" title="Settings" text="Delivery safety remains review-gated. Change the daily cap and duplicate window for the persistent Gmail worker." /><div className="aa-two-column"><section className="aa-panel"><SectionHeader title="Sending guardrails" text="The maximum supported daily cap is 150 messages." /><form className="aa-form" onSubmit={save}><Field label="Daily Gmail send cap" value={draft.daily_send_cap ?? 150} onChange={(value) => setDraft({ ...draft, daily_send_cap: Number(value) })} type="number" min={0} max={150} /><Field label="Duplicate window (days)" value={draft.duplicate_window_days ?? 7} onChange={(value) => setDraft({ ...draft, duplicate_window_days: Number(value) })} type="number" min={1} max={90} /><Field label="Timezone" value={stringValue(draft.timezone, "Asia/Kolkata")} onChange={(value) => setDraft({ ...draft, timezone: value })} /><label className="aa-checkbox"><input type="checkbox" checked readOnly /><span>Require review before every send (always on)</span></label><Button type="submit" busy={busy === "save"} className="aa-button-primary">Save settings</Button></form></section><section className="aa-panel"><SectionHeader title="Account" text={session.user.email || "Signed-in account"} /><p className="aa-muted">Deleting your account removes your workspace data, provider credentials, connections, and private résumé objects. This action cannot be undone.</p><form className="aa-form aa-danger-zone" onSubmit={deleteAccount}><Field label="Type your signed-in email" value={deleteEmail} onChange={setDeleteEmail} type="email" required /><Field label="Type DELETE" value={confirmation} onChange={setConfirmation} required /><Button type="submit" busy={busy === "delete"} className="aa-button-danger">Delete account permanently</Button></form></section></div></div>;
}
