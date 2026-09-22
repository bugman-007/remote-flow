export type Role = "maker" | "manager" | "reviewer";

export interface User {
  id: string;
  name: string;
  email: string;
  role: Role;
  is_active: boolean;
  must_change_password: boolean;
  daily_limit: number | null;
  last_login_at: string | null;
  created_at: string;
  // USR-1 extras
  profile_id?: string | null;
  profile_name?: string | null;
  usage_today?: number;
  sessions?: number;
  open_interviews?: number;
}

export interface Pagination {
  page: number;
  page_size: number;
  total: number;
  pages: number;
}

export interface Paginated<T> {
  items: T[];
  pagination: Pagination;
}

export interface DerivedStatus {
  status: string;
  label: string;
  generation: number;
  stage: string | null;
  attempt: number | null;
  error?: string | null;
  error_code?: string | null;
  released_late?: boolean;
  blocker_seq?: number | null;
  blocker_state?: string | null;
  blocker_attempt?: number | null;
}

export interface DocSetSummary {
  id: string;
  candidate_name: string | null;
  company_name: string | null;
  job_title: string | null;
  docx_basename: string | null;
  is_selected: boolean;
  keep: boolean;
  current_generation_id: string | null;
  files_expired_at: string | null;
}

export interface FileArtifact {
  id: string;
  kind: "pdf" | "docx" | "txt" | "llm_json" | "meta_json" | string;
  filename: string;
  size_bytes: number;
  sha256: string;
  expired_at?: string | null;
}

export interface DocSetRow {
  id: string;
  doc_set_id: string;
  maker_id: string;
  maker_name?: string | null;
  profile_id: string | null;
  seq_no: number;
  submitted_date: string;
  submitted_at: string | null;
  duplicate_seq?: number | null;
  delivery_status: "pending" | "released" | "skipped";
  released_at: string | null;
  released_late: boolean;
  skipped_at: string | null;
  duplicate_of: string | null;
  status: DerivedStatus;
  doc_set: DocSetSummary | null;
  generation_count: number;
  files: FileArtifact[];
  is_selected: boolean;
  keep: boolean;
  expired: boolean;
  downloaded_at?: string | null;
  attempts?: number;
  tokens?: number;
  interview_count?: number;
}

export interface JobRow {
  id: string;
  maker_id: string;
  profile_id: string | null;
  seq_no: number;
  submitted_date: string;
  submitted_at: string;
  delivery_status: string;
  released_at: string | null;
  released_late: boolean;
  skipped_at: string | null;
  duplicate_of: string | null;
  duplicate_seq?: number | null;
  status: DerivedStatus;
  doc_set: DocSetSummary | null;
}

export interface GenerationAttempt {
  id: string;
  stage: "llm" | "render";
  attempt_no: number;
  worker: string | null;
  provider_id: string | null;
  model: string | null;
  started_at: string | null;
  finished_at: string | null;
  outcome: string | null;
  error_code: string | null;
  error_message: string | null;
  tokens_in: number | null;
  tokens_cached: number | null;
  tokens_out: number | null;
  latency_ms: number | null;
  artifacts_dir: string | null;
}

export interface Generation {
  id: string;
  job_id: string;
  generation_no: number;
  kind: "initial" | "regenerate" | "retry_after_skip" | string;
  status: string;
  stage: string | null;
  llm_attempts: number;
  render_attempts: number;
  next_retry_at: string | null;
  ready_at: string | null;
  expired_at: string | null;
  claimed_by: string | null;
  lease_expires_at: string | null;
  dispatch_state: string;
  last_error_code: string | null;
  last_error_message: string | null;
  consecutive_provider_errors: number;
  snapshot: {
    prompt_version_id: string | null;
    provider_id: string | null;
    model: string | null;
    llm_params: Record<string, unknown> | null;
    theme_id: string | null;
    theme_snapshot: Record<string, unknown> | null;
  };
  created_by: string | null;
  created_at: string;
  attempts: GenerationAttempt[];
  files: FileArtifact[];
}

export interface PromptVersion {
  id: string;
  profile_id: string;
  version_no: number;
  body: string;
  change_note: string | null;
  created_by: string | null;
  created_at: string;
  active?: boolean;
  author_name?: string | null;
  previous_body?: string | null;
  diff?: { op: "add" | "remove" | "context" | "hunk"; line: string }[] | null;
}

export interface Profile {
  id: string;
  name: string;
  url: string | null;
  description: string | null;
  start_date: string | null;
  end_date: string | null;
  theme_id: string | null;
  theme_name?: string | null;
  provider_id: string | null;
  provider_name?: string | null;
  model: string | null;
  temperature: number | null;
  max_tokens: number | null;
  tags: string[];
  custom_fields: Record<string, string>;
  shared_fields: string[];
  status: "active" | "archived";
  active_prompt_version_id: string | null;
  active_prompt: PromptVersion | null;
  maker_count: number;
  doc_set_count: number;
  created_at: string;
  updated_at: string;
}

export interface Provider {
  id: string;
  type: string;
  display_name: string;
  api_key_masked: string;
  has_api_key: boolean;
  base_url: string | null;
  default_model: string | null;
  max_concurrency: number;
  rpm: number | null;
  timeout_s: number;
  is_enabled: boolean;
  is_default: boolean;
  is_fallback: boolean;
  last_used_at: string | null;
  last_status: string | null;
  last_error: string | null;
}

export interface ThemeParams {
  font?: string;
  size?: number;
  accent?: string;
  background?: string | null;
  line_height?: number;
  section_gap?: number;
  margin_top?: number;
  margin_right?: number;
  margin_bottom?: number;
  margin_left?: number;
  [key: string]: unknown;
}

export interface Theme {
  id: string;
  name: string;
  description: string | null;
  params: ThemeParams;
  status: "active" | "archived";
  profiles: { id: string; name: string }[];
  created_at: string;
  updated_at: string;
}

export interface InterviewTemplateField {
  key: string;
  label: string;
  type: string;
  required: boolean;
  help?: string | null;
  default?: unknown;
  options?: string[];
  visible_to_reviewer: boolean;
  builtin?: boolean;
}

export interface InterviewTemplate {
  id: string;
  name: string;
  fields: InterviewTemplateField[];
  is_default: boolean;
  created_at: string;
}

export interface Interview {
  id: string;
  doc_set_id: string;
  generation_id: string;
  pinned_generation_no: number | null;
  newer_generation_available: boolean;
  reviewer_id: string;
  reviewer_name: string | null;
  template_id: string;
  template_snapshot: { id?: string; name?: string; fields: InterviewTemplateField[] } | null;
  values: Record<string, unknown>;
  meeting_at: string | null;
  meeting_tz: string | null;
  status: "scheduled" | "completed" | "cancelled" | "no_show";
  created_by: string | null;
  cancelled_at: string | null;
  seen_by_reviewer_at: string | null;
  created_at: string;
  doc_set: DocSetSummary | null;
  job: { id: string; seq_no: number; submitted_date: string } | null;
  files: FileArtifact[];
  profile: Record<string, unknown> | null;
  interview_count?: number;
  feedback?: Feedback | null;
  events?: InterviewEvent[];
}

export interface InterviewEvent {
  type: string;
  actor_id: string | null;
  details: Record<string, unknown>;
  at: string;
}

export interface Feedback {
  id: string;
  interview_id: string;
  author_id: string;
  outcome: "pass" | "fail" | "hold" | "no_show";
  rating: number | null;
  strengths: string | null;
  concerns: string | null;
  notes: string | null;
  version_no: number;
  edited: boolean;
  created_at: string;
}

export interface SystemStatus {
  queues: { llm: number; render: number; ops: number };
  in_flight_llm: number;
  active_leases: number;
  expired_leases_last_hour: number;
  outbox_backlog: number;
  oldest_queued_at: string | null;
  needs_attention: Record<string, number>;
  needs_attention_total: number;
  retries_last_hour: Record<string, number>;
  workers: { name: string; queues: string[]; concurrency: number | null; pid: number | null; started_at: string | null; last_heartbeat_at: string | null; alive: boolean }[];
  sweeps_24h: number;
  retention: { expired_generations: number; bytes_freed: number };
  disk: {
    usage_pct: number;
    warn_pct: number;
    pause_intake_pct: number;
    pause_render_pct: number;
    intake_paused: boolean;
    intake_pause_reason: string | null;
    storage_dir: string;
  };
  database_size_bytes: number | null;
  unoserver: { backend: string; max_conversions: number; max_rss_mb: number; available: boolean; conversions: number };
  versions: { app: string; generator: string; libreoffice: string | null };
}

export interface Settings {
  timezone: string;
  default_daily_limit: number;
  min_jd_chars: number;
  max_jd_chars: number;
  llm_timeout_s: number;
  render_timeout_s: number;
  max_llm_attempts: number;
  max_render_attempts: number;
  show_selection_to_makers: boolean;
  default_interview_template_id: string | null;
  retention_attempt_days: number;
  retention_superseded_days: number;
  retention_doc_set_days: number;
  retention_interview_days: number;
  disk_warn_pct: number;
  disk_pause_intake_pct: number;
  disk_pause_render_pct: number;
  [key: string]: unknown;
}

export interface RetentionPlan {
  bytes: number;
  generation_ids: string[];
  superseded_generations: string[];
  doc_set_generations: [string, string[]][];
  interview_generations: string[];
  attempt_dirs: string[];
  protected: { doc_set_id: string; label: string; is_selected: boolean; keep: boolean; generations: number; bytes: number }[];
  cutoffs: Record<string, string>;
  dry_run?: boolean;
  expired_generations?: number;
}
