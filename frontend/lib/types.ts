export type Json = Record<string, unknown>;

export type Config = {
  supabase_url: string;
  supabase_publishable_key: string;
  site_url?: string;
  captcha?: { enabled?: boolean; provider?: string | null; site_key?: string | null };
  feature_flags?: Record<string, unknown>;
};

export type Profile = Json & {
  user_id?: string;
  full_name?: string | null;
  email?: string | null;
  phone?: string | null;
  location?: string | null;
  headline?: string | null;
  summary?: string | null;
  years_experience?: number | null;
  work_authorization?: string | null;
  college?: string | null;
  degree?: string | null;
  graduation_year?: number | null;
  linkedin_url?: string | null;
  github_url?: string | null;
  portfolio_url?: string | null;
  resume_url?: string | null;
  skills?: string[] | null;
  education?: Json[] | null;
  preferences?: Json | null;
};

export type Settings = Json & {
  daily_send_cap?: number;
  duplicate_window_days?: number;
  require_review?: boolean;
  timezone?: string;
};

export type Resume = Json & {
  id: string;
  original_name?: string;
  size_bytes?: number;
  parse_status?: string;
  parse_error?: string | null;
  is_active?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type Fit = Json & {
  evaluated?: boolean;
  score?: number;
  label?: string;
  basis?: string;
  matched_skills?: string[];
};

export type Job = Json & {
  id: string;
  title?: string;
  company?: string;
  location?: string | null;
  description?: string;
  apply_url?: string | null;
  contact_email?: string | null;
  source?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  fit?: Fit;
  metadata?: Json;
};

export type Application = Json & {
  id: string;
  job_id?: string | null;
  channel?: string;
  recipient?: string | null;
  subject?: string | null;
  body?: string | null;
  status?: string;
  revision?: number;
  created_at?: string;
  updated_at?: string;
  metadata?: Json;
};

export type Contact = Json & {
  email: string;
  name?: string | null;
  position?: string | null;
  source_url?: string | null;
  verification_status?: string | null;
};

export type Connection = Json & {
  id: string;
  label?: string;
  description?: string;
  status?: string;
  connection?: Json | null;
  can_scan?: boolean;
  can_submit?: boolean;
};

export type Credential = Json & {
  provider: string;
  configured?: boolean;
  verification_status?: string;
  verification_code?: string | null;
  key_hint?: string | null;
  project_id_hint?: string | null;
  verified_at?: string | null;
  requires_reconfiguration?: boolean;
  key_url?: string;
  signup_url?: string;
  project_url?: string;
};

export type AutomationJob = Json & {
  id: string;
  kind?: string;
  provider?: string;
  status?: string;
  progress?: number | Json;
  result?: Json;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type GoogleFormEntry = Json & {
  id: string;
  job_id?: string | null;
  title?: string;
  company?: string;
  location?: string | null;
  apply_url?: string;
  saved?: boolean;
  application?: Application | null;
};

export type ResearchPrompt = Json & {
  prompt?: string;
  summary?: string;
  estimated_years_experience?: number;
  experience_basis?: string;
};

export type ApiEnvelope<T> = { data?: T; items?: T[]; count?: number; [key: string]: unknown };
