export type Role = "maker" | "manager" | "reviewer";

export interface UserStats {
  // Maker card
  total_resumes?: number;
  resumes_today?: number;
  interviews?: number;
  interview_pct?: number;
  // Reviewer card
  reviews_today?: number;
  total_interviews?: number;
  by_step?: { name: string; color: string | null; count: number }[];
}

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
  // USR-9 extras
  info?: string | null;
  stats?: UserStats | null;
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
  /** BULK-1: ``bulk`` rows came from a Bulk Resumes CSV and carry its job link. */
  source?: "manual" | "bulk";
  job_link?: string | null;
  skip_reason?: "duplicate" | "failed" | null;
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
  source?: "manual" | "bulk";
  job_link?: string | null;
  skip_reason?: "duplicate" | "failed" | null;
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

export interface GroupRef {
  id: string;
  name: string;
  color: string;
}

/** PRO-11: a named, coloured set of Profiles (one group per Profile). */
export interface ProfileGroup extends GroupRef {
  profile_ids: string[];
  profile_count: number;
  created_at: string;
}

export interface BulkBatch {
  id: string;
  filename: string | null;
  created_at: string;
  generated_at: string | null;
  usable_rows: number;
  rejected_rows: number;
  rejected: { row: number; reason: string }[];
  summary: BulkSummary | null;
}

export interface BulkSummary {
  count: number;
  created: number;
  profiles: {
    profile_id: string;
    profile_name: string;
    maker_id: string;
    maker_name: string;
    created: number;
    skipped_duplicates: number;
    short_by: number;
  }[];
  skipped_profiles: { profile_id: string; profile_name: string; reason: string }[];
  csv_duplicates: number;
  rejected_rows: number;
}

export interface Profile {
  id: string;
  name: string;
  group?: GroupRef | null;
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

/** One inline styling rule (Appendix C.2): text is matched case-insensitively. */
export interface ThemeTextRule {
  text: string;
  bold?: boolean | null;
  weight?: number | null;
  italic?: boolean | null;
  underline?: boolean | null;
  uppercase?: boolean | null;
  color?: string | null;
  bg?: string | null;
}

/** Per-element style override; only the keys present are overridden. */
export type ThemeElementStyle = Record<string, string | number | boolean | null>;

export interface ThemeParams {
  font?: string;
  size?: number;
  accent?: string;
  background?: string | null;
  bg_color?: string;
  body_color?: string;
  muted_color?: string;
  bullet_glyph?: string;
  page_size?: string;
  line_height?: number;
  section_gap?: number;
  margin_top?: number;
  margin_right?: number;
  margin_bottom?: number;
  margin_left?: number;
  elements?: Record<string, ThemeElementStyle>;
  text_rules?: ThemeTextRule[];
  [key: string]: unknown;
}

export interface ThemeField {
  key: string;
  type: string;
  min: number | null;
  max: number | null;
  step: number | null;
  default: unknown;
  group?: string;
  options: string[] | null;
  font_labels: { name: string }[] | null;
}

export interface ThemeElementField {
  key: string;
  type: string;
  min: number | null;
  max: number | null;
  step: number | null;
}

export interface ThemeEditorSpec {
  elements: { key: string; label: string; fields: ThemeElementField[]; aligns: string[] }[];
  element_labels: Record<string, string>;
  text_rules: { fields: { key: string; type: string }[]; max: number };
  presets: { key: string; name: string; params: ThemeParams }[];
  page_sizes: string[];
  aligns: string[];
  generator_version: string;
}

export interface ThemeSchema {
  fields: ThemeField[];
  defaults: ThemeParams;
  fonts: { name: string; pdf_substitute: string }[];
  editor: ThemeEditorSpec;
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

export interface InterviewStep {
  id: string;
  name: string;
  color: string | null;
  position: number;
  is_active: boolean;
}

export interface InterviewStatus {
  id: string;
  name: string;
  color: string | null;
  position: number;
  is_active: boolean;
}

export interface InterviewStepRecord {
  id: string;
  step_id: string | null;
  step_name: string | null;
  step_color: string | null;
  position: number;
  reviewer_id: string | null;
  reviewer_name: string | null;
  done: boolean;
  rejected: boolean;
  note: string | null;
  done_at: string | null;
  created_at: string;
}

export interface InterviewAttachment {
  id: string;
  kind: "resume" | "jd" | string;
  filename: string;
  content_type: string | null;
  size_bytes: number | null;
}

export interface Interview {
  id: string;
  doc_set_id: string | null;
  generation_id: string | null;
  pinned_generation_no: number | null;
  newer_generation_available: boolean;
  reviewer_id: string | null;
  reviewer_name: string | null;
  template_id: string | null;
  template_snapshot: { id?: string; name?: string; fields: InterviewTemplateField[] } | null;
  values: Record<string, unknown>;
  meeting_at: string | null;
  meeting_tz: string | null;
  status: "scheduled" | "completed" | "cancelled" | "no_show";
  status_id: string | null;
  status_label: InterviewStatus | null;
  tech_stack: string | null;
  company_name: string | null;
  job_title: string | null;
  candidate_name: string | null;
  steps: InterviewStepRecord[];
  attachments: InterviewAttachment[];
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
  llm_single_request: boolean;
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
  llm_pool_mode: "static" | "dynamic";
  llm_pool_static_size: number;
  llm_pool_min: number;
  llm_pool_max: number;
  llm_grow_below_pct: number;
  llm_admit_above_pct: number;
  llm_shrink_above_pct: number;
  llm_shrink_below_pct: number;
  llm_scale_step: number;
  llm_scale_interval_s: number;
  llm_shrink_cooldown_s: number;
  [key: string]: unknown;
}

/** CONC-2: what the pool controller decided last, and why. */
export interface PoolState {
  mode?: "static" | "dynamic";
  /** Who runs the LLM stage: one prefork process per call, or the async runner. */
  executor?: "celery" | "runner";
  /** Runner only: the most parallel calls this server holds (LLM_RUNNER_MAX_CONCURRENCY). */
  hard_cap?: number | null;
  state: string;
  reason?: string;
  floor?: number;
  ceiling?: number;
  size?: number;
  budget?: number;
  active?: number;
  inflight?: number;
  backlog?: number;
  memory_used_pct?: number;
  memory_available_mb?: number;
  memory_total_mb?: number;
  cpu_pct?: number | null;
  updated_at?: number;
  applied?: boolean;
}

export interface PoolStatus {
  state: PoolState;
  settings: Settings;
  active: number;
  inflight: number;
  backlog: number;
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
