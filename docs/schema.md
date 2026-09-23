# Database schema

Generated from the SQLAlchemy models (`backend/app/models.py`) by
`python manage.py schema-doc`; run that after any model change so this file
stays in sync with `alembic/versions/`.

Types are shown as SQLAlchemy portable types. On PostgreSQL the portable
`JSON`/`GUID`/`DATETIME` columns are created as `JSONB`/`UUID`/`timestamptz`
(see `app/models.py::JSONType`, `GUID`, `DateTimeTZ`), which is what production
runs; SQLite is only used for development and the test suite.

Relationships (`prod-requirements.md` Appendix B):

- `users 1-n jobs`, `jobs 1-1 doc_sets`, `jobs 1-n generations`
- `generations 1-n generation_attempts`, `generations 1-n files`
- `doc_sets 1-n interviews`, `interviews n-1 generations` (the pinned generation)
- `interviews 1-n feedback`, `interviews 1-n interview_events`
- `profiles 1-n prompt_versions`, `profiles 1-n profile_assignments n-1 users`
- `jobs.duplicate_of -> jobs.id` marks a duplicate JD inside the 7-day window
- `event_outbox` is the transactional outbox for SSE; `pipeline_events` is the
  per-build audit trail shown in the doc set drawer
- `worker_heartbeats` and `system_metrics` back the System status card

Integrity notes:

- `jobs` has a unique `(maker_id, submitted_date, seq_no)`: the gapless daily
  sequence (PIPE-11) is enforced by the database, not just by the lock.
- `generations` has a unique `(job_id, generation_no)` and is immutable once
  `status = 'ready'`.
- `generation_attempts` has a unique `(generation_id, stage, attempt_no)`.
- Immutable-then-expired rows (`files.expired_at`, `generations.expired_at`,
  `doc_sets.files_expired_at`) are tombstoned rather than deleted so the audit
  trail survives retention.

---


## `audit_log`

| column | type | null | key |
| --- | --- | --- | --- |
| `actor_id` | `VARCHAR(36)` | yes | FK -> users.id, index |
| `action` | `VARCHAR(120)` | no | index |
| `entity_type` | `VARCHAR(80)` | yes |  |
| `entity_id` | `VARCHAR(64)` | yes |  |
| `before` | `JSON` | yes |  |
| `after` | `JSON` | yes |  |
| `ip` | `VARCHAR(64)` | yes |  |
| `at` | `DATETIME` | no |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_audit_log_action`: `action`

- index `ix_audit_log_actor_id`: `actor_id`

## `daily_maker_stats`

| column | type | null | key |
| --- | --- | --- | --- |
| `maker_id` | `VARCHAR(36)` | no | FK -> users.id, index |
| `date` | `DATE` | no |  |
| `submitted` | `INTEGER` | no |  |
| `ready` | `INTEGER` | no |  |
| `skipped` | `INTEGER` | no |  |
| `selected` | `INTEGER` | no |  |
| `avg_ready_ms` | `INTEGER` | yes |  |
| `avg_llm_attempts` | `FLOAT` | yes |  |
| `tokens` | `INTEGER` | no |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `maker_id`, `date`

- index `ix_daily_maker_stats_maker_id`: `maker_id`

## `doc_sets`

| column | type | null | key |
| --- | --- | --- | --- |
| `job_id` | `VARCHAR(36)` | no | FK -> jobs.id |
| `candidate_name` | `VARCHAR(300)` | yes |  |
| `company_name` | `VARCHAR(300)` | yes | index |
| `job_title` | `VARCHAR(300)` | yes |  |
| `slug` | `VARCHAR(120)` | yes |  |
| `storage_dir` | `VARCHAR(500)` | yes |  |
| `docx_basename` | `VARCHAR(300)` | yes |  |
| `current_generation_id` | `VARCHAR(36)` | yes |  |
| `is_selected` | `BOOLEAN` | no | index |
| `selected_by` | `VARCHAR(36)` | yes | FK -> users.id |
| `selected_at` | `DATETIME` | yes |  |
| `keep` | `BOOLEAN` | no |  |
| `renamed_by` | `VARCHAR(36)` | yes | FK -> users.id |
| `files_expired_at` | `DATETIME` | yes |  |
| `downloaded_at` | `DATETIME` | yes | index |
| `search_tsv` | `TEXT` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `job_id`

- index `ix_doc_sets_company_name`: `company_name`

- index `ix_doc_sets_downloaded_at`: `downloaded_at`

- index `ix_doc_sets_is_selected`: `is_selected`

## `event_outbox`

| column | type | null | key |
| --- | --- | --- | --- |
| `audience` | `JSON` | no |  |
| `event_type` | `VARCHAR(80)` | no |  |
| `payload` | `JSON` | no |  |
| `published_at` | `DATETIME` | yes |  |
| `publish_attempts` | `INTEGER` | no |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_outbox_unpublished`: `published_at`

## `feedback`

| column | type | null | key |
| --- | --- | --- | --- |
| `interview_id` | `VARCHAR(36)` | no | FK -> interviews.id, index |
| `author_id` | `VARCHAR(36)` | no | FK -> users.id |
| `outcome` | `VARCHAR(32)` | yes |  |
| `rating` | `INTEGER` | yes |  |
| `strengths` | `TEXT` | yes |  |
| `concerns` | `TEXT` | yes |  |
| `notes` | `TEXT` | yes |  |
| `version_no` | `INTEGER` | no |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `interview_id`, `version_no`

- index `ix_feedback_interview_id`: `interview_id`

## `files`

| column | type | null | key |
| --- | --- | --- | --- |
| `generation_id` | `VARCHAR(36)` | yes | FK -> generations.id, index |
| `doc_set_id` | `VARCHAR(36)` | yes | FK -> doc_sets.id, index |
| `kind` | `VARCHAR(32)` | no |  |
| `path` | `VARCHAR(1000)` | no |  |
| `filename` | `VARCHAR(300)` | no |  |
| `size_bytes` | `INTEGER` | no |  |
| `sha256` | `VARCHAR(64)` | yes |  |
| `expired_at` | `DATETIME` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_files_doc_set_id`: `doc_set_id`

- index `ix_files_generation_id`: `generation_id`

- index `ix_files_generation_kind`: `generation_id`, `kind`

## `generation_attempts`

| column | type | null | key |
| --- | --- | --- | --- |
| `generation_id` | `VARCHAR(36)` | no | FK -> generations.id, index |
| `stage` | `VARCHAR(32)` | no |  |
| `attempt_no` | `INTEGER` | no |  |
| `lease_token` | `VARCHAR(36)` | yes |  |
| `worker` | `VARCHAR(200)` | yes |  |
| `provider_id` | `VARCHAR(36)` | yes | FK -> llm_providers.id |
| `model` | `VARCHAR(200)` | yes |  |
| `started_at` | `DATETIME` | no |  |
| `finished_at` | `DATETIME` | yes |  |
| `outcome` | `VARCHAR(32)` | yes |  |
| `error_code` | `VARCHAR(100)` | yes |  |
| `error_message` | `VARCHAR(2000)` | yes |  |
| `tokens_in` | `INTEGER` | yes |  |
| `tokens_cached` | `INTEGER` | yes |  |
| `tokens_out` | `INTEGER` | yes |  |
| `latency_ms` | `INTEGER` | yes |  |
| `artifacts_dir` | `VARCHAR(500)` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `generation_id`, `stage`, `attempt_no`

- index `ix_generation_attempts_generation_id`: `generation_id`

## `generations`

| column | type | null | key |
| --- | --- | --- | --- |
| `job_id` | `VARCHAR(36)` | no | FK -> jobs.id, index |
| `generation_no` | `INTEGER` | no |  |
| `kind` | `VARCHAR(32)` | no |  |
| `status` | `VARCHAR(32)` | no | index |
| `stage` | `VARCHAR(32)` | no |  |
| `llm_attempts` | `INTEGER` | no |  |
| `render_attempts` | `INTEGER` | no |  |
| `retry_budget_reset_at` | `DATETIME` | no |  |
| `next_retry_at` | `DATETIME` | yes |  |
| `dispatch_state` | `VARCHAR(32)` | no |  |
| `dispatched_at` | `DATETIME` | yes |  |
| `claimed_by` | `VARCHAR(200)` | yes |  |
| `lease_token` | `VARCHAR(36)` | yes |  |
| `lease_expires_at` | `DATETIME` | yes |  |
| `consecutive_provider_errors` | `INTEGER` | no |  |
| `last_error_code` | `VARCHAR(100)` | yes |  |
| `last_error_message` | `VARCHAR(2000)` | yes |  |
| `prompt_version_id` | `VARCHAR(36)` | yes | FK -> prompt_versions.id |
| `provider_id` | `VARCHAR(36)` | yes | FK -> llm_providers.id |
| `model` | `VARCHAR(200)` | yes |  |
| `llm_params` | `JSON` | yes |  |
| `theme_id` | `VARCHAR(36)` | yes | FK -> themes.id |
| `theme_snapshot` | `JSON` | yes |  |
| `storage_dir` | `VARCHAR(500)` | yes |  |
| `ready_at` | `DATETIME` | yes |  |
| `created_by` | `VARCHAR(36)` | yes | FK -> users.id |
| `expired_at` | `DATETIME` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `job_id`, `generation_no`

- index `ix_generations_dispatch_pending`: `dispatch_state`, `status`

- index `ix_generations_job_id`: `job_id`

- index `ix_generations_lease`: `lease_expires_at`

- index `ix_generations_next_retry`: `next_retry_at`

- index `ix_generations_ready_initial`: `job_id`

- index `ix_generations_status`: `status`

## `interview_attachments`

| column | type | null | key |
| --- | --- | --- | --- |
| `interview_id` | `VARCHAR(36)` | yes | FK -> interviews.id, index |
| `kind` | `VARCHAR(32)` | no |  |
| `filename` | `VARCHAR(300)` | no |  |
| `path` | `VARCHAR(1000)` | no |  |
| `content_type` | `VARCHAR(200)` | yes |  |
| `size_bytes` | `INTEGER` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_interview_attachments_interview_id`: `interview_id`

## `interview_events`

| column | type | null | key |
| --- | --- | --- | --- |
| `interview_id` | `VARCHAR(36)` | no | FK -> interviews.id, index |
| `type` | `VARCHAR(80)` | no |  |
| `actor_id` | `VARCHAR(36)` | yes | FK -> users.id |
| `details` | `JSON` | yes |  |
| `at` | `DATETIME` | no |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_interview_events_interview_id`: `interview_id`

## `interview_statuses`

| column | type | null | key |
| --- | --- | --- | --- |
| `name` | `VARCHAR(120)` | no |  |
| `color` | `VARCHAR(32)` | yes |  |
| `position` | `INTEGER` | no | index |
| `is_active` | `BOOLEAN` | no |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `name`

- index `ix_interview_statuses_position`: `position`

## `interview_step_records`

| column | type | null | key |
| --- | --- | --- | --- |
| `interview_id` | `VARCHAR(36)` | no | FK -> interviews.id, index |
| `step_id` | `VARCHAR(36)` | yes | FK -> interview_steps.id |
| `position` | `INTEGER` | no |  |
| `reviewer_id` | `VARCHAR(36)` | yes | FK -> users.id, index |
| `done` | `BOOLEAN` | no |  |
| `rejected` | `BOOLEAN` | no |  |
| `note` | `TEXT` | yes |  |
| `done_at` | `DATETIME` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_interview_step_records_interview_id`: `interview_id`

- index `ix_interview_step_records_reviewer_id`: `reviewer_id`

## `interview_steps`

| column | type | null | key |
| --- | --- | --- | --- |
| `name` | `VARCHAR(120)` | no |  |
| `color` | `VARCHAR(32)` | yes |  |
| `position` | `INTEGER` | no | index |
| `is_active` | `BOOLEAN` | no |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `name`

- index `ix_interview_steps_position`: `position`

## `interview_templates`

| column | type | null | key |
| --- | --- | --- | --- |
| `name` | `VARCHAR(200)` | no |  |
| `fields` | `JSON` | no |  |
| `is_default` | `BOOLEAN` | no |  |
| `created_by` | `VARCHAR(36)` | yes | FK -> users.id |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

## `interviews`

| column | type | null | key |
| --- | --- | --- | --- |
| `doc_set_id` | `VARCHAR(36)` | yes | FK -> doc_sets.id, index |
| `generation_id` | `VARCHAR(36)` | yes | FK -> generations.id |
| `reviewer_id` | `VARCHAR(36)` | yes | FK -> users.id, index |
| `template_id` | `VARCHAR(36)` | yes | FK -> interview_templates.id |
| `template_snapshot` | `JSON` | yes |  |
| `values` | `JSON` | yes |  |
| `meeting_at` | `DATETIME` | yes | index |
| `meeting_tz` | `VARCHAR(100)` | yes |  |
| `status` | `VARCHAR(32)` | no | index |
| `tech_stack` | `VARCHAR(500)` | yes |  |
| `status_id` | `VARCHAR(36)` | yes | FK -> interview_statuses.id, index |
| `company_name` | `VARCHAR(300)` | yes | index |
| `job_title` | `VARCHAR(300)` | yes |  |
| `candidate_name` | `VARCHAR(300)` | yes |  |
| `created_by` | `VARCHAR(36)` | yes | FK -> users.id |
| `cancelled_at` | `DATETIME` | yes |  |
| `seen_by_reviewer_at` | `DATETIME` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_interviews_company_name`: `company_name`

- index `ix_interviews_doc_set_id`: `doc_set_id`

- index `ix_interviews_meeting_at`: `meeting_at`

- index `ix_interviews_reviewer_id`: `reviewer_id`

- index `ix_interviews_status`: `status`

- index `ix_interviews_status_id`: `status_id`

## `jobs`

| column | type | null | key |
| --- | --- | --- | --- |
| `maker_id` | `VARCHAR(36)` | no | FK -> users.id, index |
| `profile_id` | `VARCHAR(36)` | yes | FK -> profiles.id |
| `seq_no` | `INTEGER` | no |  |
| `submitted_date` | `DATE` | no | index |
| `submitted_at` | `DATETIME` | no |  |
| `idempotency_key` | `VARCHAR(200)` | yes |  |
| `jd_text` | `TEXT` | no |  |
| `jd_hash` | `VARCHAR(64)` | no | index |
| `duplicate_of` | `VARCHAR(36)` | yes | FK -> jobs.id |
| `delivery_status` | `VARCHAR(32)` | no | index |
| `released_at` | `DATETIME` | yes |  |
| `released_late` | `BOOLEAN` | no |  |
| `skipped_at` | `DATETIME` | yes |  |
| `skipped_by` | `VARCHAR(36)` | yes | FK -> users.id |
| `initial_generation_id` | `VARCHAR(36)` | yes |  |
| `jd_tsv` | `TEXT` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `maker_id`, `idempotency_key`

- unique: `maker_id`, `submitted_date`, `seq_no`

- index `ix_jobs_delivery_status`: `delivery_status`

- index `ix_jobs_jd_hash`: `jd_hash`

- index `ix_jobs_maker_id`: `maker_id`

- index `ix_jobs_pending_cursor`: `maker_id`, `submitted_date`, `seq_no`

- index `ix_jobs_submitted_date`: `submitted_date`

## `llm_providers`

| column | type | null | key |
| --- | --- | --- | --- |
| `type` | `VARCHAR(32)` | no |  |
| `display_name` | `VARCHAR(200)` | no |  |
| `api_key_enc` | `BLOB` | yes |  |
| `base_url` | `VARCHAR(1000)` | yes |  |
| `default_model` | `VARCHAR(200)` | yes |  |
| `max_concurrency` | `INTEGER` | no |  |
| `rpm` | `INTEGER` | no |  |
| `timeout_s` | `INTEGER` | no |  |
| `is_enabled` | `BOOLEAN` | no |  |
| `is_default` | `BOOLEAN` | no |  |
| `is_fallback` | `BOOLEAN` | no |  |
| `last_used_at` | `DATETIME` | yes |  |
| `last_status` | `VARCHAR(50)` | yes |  |
| `last_error` | `VARCHAR(1000)` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `display_name`

## `pipeline_events`

| column | type | null | key |
| --- | --- | --- | --- |
| `job_id` | `VARCHAR(36)` | yes | FK -> jobs.id, index |
| `generation_id` | `VARCHAR(36)` | yes | FK -> generations.id, index |
| `from_state` | `VARCHAR(50)` | yes |  |
| `to_state` | `VARCHAR(50)` | yes |  |
| `stage` | `VARCHAR(20)` | yes |  |
| `attempt_no` | `INTEGER` | yes |  |
| `actor` | `VARCHAR(100)` | yes |  |
| `details` | `JSON` | yes |  |
| `at` | `DATETIME` | no |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_pipeline_events_generation_id`: `generation_id`

- index `ix_pipeline_events_job_id`: `job_id`

## `profile_assignments`

| column | type | null | key |
| --- | --- | --- | --- |
| `profile_id` | `VARCHAR(36)` | no | FK -> profiles.id, index |
| `maker_id` | `VARCHAR(36)` | no | FK -> users.id, index |
| `assigned_by` | `VARCHAR(36)` | yes | FK -> users.id |
| `started_at` | `DATETIME` | no |  |
| `ended_at` | `DATETIME` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_profile_assignments_active_maker`: `maker_id`

- index `ix_profile_assignments_maker_id`: `maker_id`

- index `ix_profile_assignments_profile_id`: `profile_id`

## `profiles`

| column | type | null | key |
| --- | --- | --- | --- |
| `name` | `VARCHAR(200)` | no |  |
| `url` | `VARCHAR(1000)` | yes |  |
| `description` | `TEXT` | yes |  |
| `start_date` | `DATE` | yes |  |
| `end_date` | `DATE` | yes |  |
| `theme_id` | `VARCHAR(36)` | yes | FK -> themes.id |
| `provider_id` | `VARCHAR(36)` | yes | FK -> llm_providers.id |
| `model` | `VARCHAR(200)` | yes |  |
| `temperature` | `FLOAT` | yes |  |
| `max_tokens` | `INTEGER` | yes |  |
| `tags` | `JSON` | yes |  |
| `custom_fields` | `JSON` | yes |  |
| `shared_fields` | `JSON` | yes |  |
| `status` | `VARCHAR(32)` | no |  |
| `active_prompt_version_id` | `VARCHAR(36)` | yes | FK -> prompt_versions.id |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `name`

## `prompt_versions`

| column | type | null | key |
| --- | --- | --- | --- |
| `profile_id` | `VARCHAR(36)` | no | FK -> profiles.id, index |
| `version_no` | `INTEGER` | no |  |
| `body` | `TEXT` | no |  |
| `change_note` | `VARCHAR(500)` | yes |  |
| `created_by` | `VARCHAR(36)` | yes | FK -> users.id |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `profile_id`, `version_no`

- index `ix_prompt_versions_profile_id`: `profile_id`

## `refresh_tokens`

| column | type | null | key |
| --- | --- | --- | --- |
| `user_id` | `VARCHAR(36)` | no | FK -> users.id, index |
| `token_hash` | `VARCHAR(64)` | no |  |
| `family_id` | `VARCHAR(36)` | no | index |
| `expires_at` | `DATETIME` | no |  |
| `revoked_at` | `DATETIME` | yes |  |
| `user_agent` | `VARCHAR(400)` | yes |  |
| `ip` | `VARCHAR(64)` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `token_hash`

- index `ix_refresh_tokens_family_id`: `family_id`

- index `ix_refresh_tokens_user_id`: `user_id`

## `settings`

| column | type | null | key |
| --- | --- | --- | --- |
| `key` | `VARCHAR(100)` | no | index |
| `value` | `JSON` | no |  |
| `updated_by` | `VARCHAR(36)` | yes | FK -> users.id |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_settings_key`: `key`

## `system_metrics`

| column | type | null | key |
| --- | --- | --- | --- |
| `key` | `VARCHAR(160)` | no | index |
| `value_int` | `INTEGER` | no |  |
| `value_text` | `TEXT` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_system_metrics_key`: `key`

## `themes`

| column | type | null | key |
| --- | --- | --- | --- |
| `name` | `VARCHAR(200)` | no |  |
| `description` | `TEXT` | yes |  |
| `params` | `JSON` | no |  |
| `status` | `VARCHAR(32)` | no |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `name`

## `users`

| column | type | null | key |
| --- | --- | --- | --- |
| `email` | `VARCHAR(320)` | no | index |
| `name` | `VARCHAR(200)` | no |  |
| `role` | `VARCHAR(32)` | no |  |
| `password_hash` | `VARCHAR(255)` | no |  |
| `is_active` | `BOOLEAN` | no |  |
| `must_change_password` | `BOOLEAN` | no |  |
| `daily_limit` | `INTEGER` | yes |  |
| `info` | `TEXT` | yes |  |
| `last_login_at` | `DATETIME` | yes |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- index `ix_users_email`: `email`

## `worker_heartbeats`

| column | type | null | key |
| --- | --- | --- | --- |
| `name` | `VARCHAR(200)` | no |  |
| `queues` | `JSON` | yes |  |
| `concurrency` | `INTEGER` | yes |  |
| `pid` | `INTEGER` | yes |  |
| `started_at` | `DATETIME` | yes |  |
| `last_heartbeat_at` | `DATETIME` | no |  |
| `id` | `VARCHAR(36)` | no | PK |
| `created_at` | `DATETIME` | no |  |
| `updated_at` | `DATETIME` | no |  |

- unique: `name`

