"""The baseline of the Tares 2.0 session store (TR-1816): every table of the cell, made at once.

Tares 2.0 starts its history here. The 1.x revisions (0001_base to 0027_drop_move_bookkeeping) are
gone, with the 1.x table names and the tables only 1.x used (scripts/migrate_to_2_input.json lists
them). A 1.x cell is brought to this revision by the one-time migration script (TR-1821), never by
an upgrade from here: a database at any other revision is left as it is (SessionStore.upgrade).

The columns are written out here, not imported from tables.py: a revision is a record of what ran.
tests/test_baseline.py fails when this and tables.py differ. JSON columns are TEXT on both engines
and every timestamp is TIMESTAMP WITH TIME ZONE (`_TS`).

Downgrade drops every table.

Revision ID: 0001_baseline
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)

TABLES = (
    "answers",
    "api_keys",
    "assumptions",
    "brain_calls",
    "brain_repo_folders",
    "brain_repo_updates",
    "brain_repos",
    "challenges",
    "crews",
    "decisions",
    "desk_items",
    "docs",
    "github_credentials",
    "grants",
    "guardrail_pairs",
    "guardrail_rules",
    "handover_overrides",
    "handovers",
    "integration_choices",
    "integration_connections",
    "integration_keys",
    "merge_queue",
    "merge_records",
    "milestone_acceptances",
    "milestone_check_results",
    "milestones",
    "model_usage",
    "pending_memory",
    "pending_writeback",
    "people",
    "pr_events",
    "project_docs",
    "project_integrations",
    "project_log",
    "project_members",
    "project_merge_method",
    "project_required_checks",
    "project_secrets",
    "project_sessions",
    "project_skills",
    "projects",
    "repo_map_history",
    "repo_maps",
    "report_texts",
    "report_weeks",
    "review_findings",
    "reviews",
    "rule_proposals",
    "session_archive_parts",
    "session_commands",
    "session_files",
    "session_lines",
    "session_messages",
    "session_meters",
    "session_rollup_state",
    "session_said",
    "session_states",
    "session_stations",
    "session_turns",
    "session_usage",
    "session_usage_hourly",
    "sessions",
    "settings",
    "shared_skills",
    "signoffs",
    "spec_questions",
    "ticket_checks",
    "ticket_history",
    "ticket_numbers",
    "tickets",
    "waiting_told",
)


def upgrade() -> None:
    op.create_table("answers",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("ask", sa.Text(), nullable=True),
        sa.Column("choice", sa.Text(), nullable=True),
        sa.Column("said", sa.Text(), nullable=True),
        sa.Column("via", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.Column("answered_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_index("idx_answers_at", "answers", ["answered_at"], unique=False)
    op.create_table("api_keys",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("prefix", sa.Text(), nullable=True),
        sa.Column("hash", sa.Text(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("last_used_at", _TS, nullable=True),
        sa.Column("revoked_at", _TS, nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("assumptions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("choice", sa.Text(), nullable=True),
        sa.Column("why", sa.Text(), nullable=True),
        sa.Column("affects", sa.Text(), nullable=True),
        sa.Column("tickets", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("words", sa.Text(), nullable=True),
        sa.Column("batch", sa.Text(), nullable=True),
        sa.Column("made_by", sa.Text(), nullable=True),
        sa.Column("made_role", sa.Text(), nullable=True),
        sa.Column("made_at", _TS, nullable=True),
        sa.Column("decided_at", _TS, nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("brain_calls",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("at", _TS, nullable=True),
        sa.Column("tool", sa.Text(), nullable=True),
        sa.Column("deep", sa.Integer(), server_default="0", nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("repos", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("station", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.Column("via", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("brain_repo_folders",
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("chosen_by", sa.Text(), nullable=True),
        sa.Column("chosen_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("repo", "path")
        )
    op.create_table("brain_repo_updates",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("at", _TS, nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("ok", sa.Integer(), nullable=True),
        sa.Column("indexed_commit", sa.Text(), nullable=True),
        sa.Column("files_changed", sa.Integer(), nullable=True),
        sa.Column("files_deleted", sa.Integer(), nullable=True),
        sa.Column("files_total", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_index("idx_brain_repo_updates_repo", "brain_repo_updates", ["repo", "at"], unique=False)
    op.create_table("brain_repos",
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("repo_source_id", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("files_done", sa.Integer(), server_default="0", nullable=True),
        sa.Column("files_total", sa.Integer(), server_default="0", nullable=True),
        sa.Column("indexed_commit", sa.Text(), nullable=True),
        sa.Column("last_synced", _TS, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("added_by", sa.Text(), nullable=True),
        sa.Column("added_at", _TS, nullable=True),
        sa.Column("place", sa.Integer(), nullable=True),
        sa.Column("job_started_at", _TS, nullable=True),
        sa.Column("sync_failures", sa.Integer(), server_default="0", nullable=True),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("repo")
        )
    op.create_table("challenges",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("key_value", sa.Text(), nullable=False),
        sa.Column("event_time", _TS, nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("tares_project", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("source", "key_value", "event_time", "event_type", "branch")
        )
    op.create_index("ix_challenges_branch", "challenges", ["source", "branch"], unique=False)
    op.create_table("crews",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.Column("machine", sa.Text(), nullable=True),
        sa.Column("first_seen", _TS, nullable=True),
        sa.Column("last_seen", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_id", "name", "machine", name="crews_person_id_name_machine_key")
        )
    op.create_table("decisions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("desk_item", sa.Text(), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("words", sa.Text(), nullable=True),
        sa.Column("choice", sa.Text(), nullable=True),
        sa.Column("standing", sa.Boolean(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("matched", sa.Boolean(), nullable=True),
        sa.Column("session", sa.Text(), nullable=True),
        sa.Column("granted", sa.Boolean(), nullable=True),
        sa.Column("tickets", sa.Text(), nullable=True),
        sa.Column("decided_at", _TS, nullable=True),
        sa.Column("via", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("desk_items",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("options", sa.Text(), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("why", sa.Text(), nullable=True),
        sa.Column("blocks", sa.Text(), nullable=True),
        sa.Column("tickets", sa.Text(), nullable=True),
        sa.Column("blocking", sa.Boolean(), nullable=True),
        sa.Column("asked_by", sa.Text(), nullable=True),
        sa.Column("assumptions", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("closed_at", _TS, nullable=True),
        sa.Column("station", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.Column("to_person_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("docs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("made_by", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("github_credentials",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("token", sa.Text(), nullable=True),
        sa.Column("api_url", sa.Text(), nullable=True),
        sa.Column("account", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("config", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("name")
        )
    op.create_table("grants",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("holder_person_id", sa.Text(), nullable=True),
        sa.Column("words", sa.Text(), nullable=True),
        sa.Column("given_by", sa.Text(), nullable=True),
        sa.Column("given_at", _TS, nullable=True),
        sa.Column("areas", sa.Text(), nullable=True),
        sa.Column("consents", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("needs_lead", sa.Boolean(), server_default=sa.false(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("guardrail_pairs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("project_uid", sa.Text(), nullable=True),
        sa.Column("hook", sa.Text(), nullable=True),
        sa.Column("integration", sa.Text(), nullable=True),
        sa.Column("settings", sa.Text(), nullable=True),
        sa.Column("on_fail", sa.Text(), server_default="block", nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("guardrail_rules",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("handover_overrides",
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("at", _TS, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.Column("handed_by", sa.Text(), nullable=True)
        )
    op.create_table("handovers",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("handed_by", sa.Text(), nullable=True),
        sa.Column("handed_at", _TS, nullable=True),
        sa.Column("repos", sa.Text(), nullable=True),
        sa.Column("autonomy", sa.Text(), nullable=True),
        sa.Column("finished_at", _TS, nullable=True),
        sa.Column("finished_words", sa.Text(), nullable=True),
        sa.Column("finished_decision", sa.Text(), nullable=True),
        sa.Column("finished_by", sa.Text(), nullable=True),
        sa.Column("taken_at", _TS, nullable=True),
        sa.Column("taken_words", sa.Text(), nullable=True),
        sa.Column("taken_by", sa.Text(), nullable=True),
        sa.Column("released_at", _TS, nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.Column("finished_person_id", sa.Text(), nullable=True),
        sa.Column("taken_person_id", sa.Text(), nullable=True),
        sa.Column("crew", sa.Text(), nullable=True),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column("override_at", _TS, nullable=True),
        sa.Column("override_person_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("project")
        )
    op.create_table("integration_choices",
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("settings", sa.Text(), nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("slot")
        )
    op.create_table("integration_connections",
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("settings", sa.Text(), nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("slot", "name")
        )
    op.create_table("integration_keys",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("integration", sa.Text(), nullable=False),
        sa.Column("credential", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("key")
        )
    op.create_index("integration_keys_credential", "integration_keys", ["integration", "credential"], unique=False)
    op.create_table("merge_queue",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("base", sa.Text(), nullable=True),
        sa.Column("pr", sa.Integer(), nullable=True),
        sa.Column("head", sa.Text(), nullable=True),
        sa.Column("ticket", sa.Text(), nullable=True),
        sa.Column("project_uid", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("desk_item", sa.Text(), nullable=True),
        sa.Column("approved_head", sa.Text(), nullable=True),
        sa.Column("entered_at", _TS, nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("before_files", sa.Text(), nullable=True),
        sa.Column("unknown_ticks", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_index("idx_merge_queue_repo", "merge_queue", ["repo", "base"], unique=False)
    op.create_table("merge_records",
        sa.Column("queue_row", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("ticket", sa.Text(), nullable=True),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("pr", sa.Integer(), nullable=True),
        sa.Column("level", sa.Text(), nullable=True),
        sa.Column("reviewed_sha", sa.Text(), nullable=True),
        sa.Column("merged_sha", sa.Text(), nullable=True),
        sa.Column("rounds", sa.Integer(), nullable=True),
        sa.Column("ci_state", sa.Text(), nullable=True),
        sa.Column("approver_id", sa.Text(), nullable=True),
        sa.Column("approved_at", _TS, nullable=True),
        sa.Column("merged_at", _TS, nullable=True),
        sa.Column("outside", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("queue_row")
        )
    op.create_index("merge_records_project_merged_at", "merge_records", ["project", "merged_at"], unique=False)
    op.create_table("milestone_acceptances",
        sa.Column("milestone", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("by_name", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.Column("accepted_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("milestone")
        )
    op.create_table("milestone_check_results",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("milestone", sa.Text(), nullable=True),
        sa.Column("idx", sa.Integer(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("by_kind", sa.Text(), nullable=True),
        sa.Column("by_name", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.Column("checked_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("milestones",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("checks", sa.Text(), nullable=True),
        sa.Column("position", sa.Double(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("model_usage",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("ts", _TS, nullable=True),
        sa.Column("surface", sa.Text(), nullable=True),
        sa.Column("agent", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("calls", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_creation_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_read_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Double(), nullable=True),
        sa.Column("key_source", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_index("ix_model_usage_ts", "model_usage", ["ts"], unique=False)
    op.create_table("pending_memory",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("line", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("tried_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("pending_writeback",
        sa.Column("ticket", sa.Text(), nullable=False),
        sa.Column("integration", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("ticket", "integration")
        )
    op.create_table("people",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("emails", sa.Text(), nullable=True),
        sa.Column("github", sa.Text(), nullable=True),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("created_via", sa.Text(), nullable=True),
        sa.Column("picture", sa.Text(), nullable=True),
        sa.Column("removed_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("pr_events",
        sa.Column("delivery_id", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("at", _TS, nullable=False),
        sa.PrimaryKeyConstraint("delivery_id")
        )
    op.create_index("pr_events_at", "pr_events", ["at"], unique=False)
    op.create_index("pr_events_repo_branch", "pr_events", ["repo", "branch"], unique=False)
    op.create_table("project_docs",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("doc", sa.Text(), nullable=False),
        sa.Column("created_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("project", "doc")
        )
    op.create_index("project_docs_doc", "project_docs", ["doc"], unique=False)
    op.create_table("project_integrations",
        sa.Column("project_uid", sa.Text(), nullable=False),
        sa.Column("slot", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("settings", sa.Text(), nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("project_uid", "slot")
        )
    op.create_table("project_log",
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("logged_at", _TS, nullable=True),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True)
        )
    op.create_index("project_log_project", "project_log", ["project", "logged_at"], unique=False)
    op.create_table("project_members",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("added_by", sa.Text(), nullable=True),
        sa.Column("added_at", _TS, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("project", "person_id")
        )
    op.create_table("project_merge_method",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("project")
        )
    op.create_table("project_required_checks",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("commands", sa.Text(), nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("project")
        )
    op.create_table("project_secrets",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("set_by", sa.Text(), nullable=True),
        sa.Column("set_at", _TS, nullable=True),
        sa.Column("used_by", sa.Text(), nullable=True),
        sa.Column("used_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("project", "name")
        )
    op.create_table("project_sessions",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("linked_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("project", "session")
        )
    op.create_index("project_sessions_session", "project_sessions", ["session"], unique=False)
    op.create_table("project_skills",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("skill", sa.Text(), nullable=False),
        sa.Column("created_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("project", "skill")
        )
    op.create_index("project_skills_skill", "project_skills", ["skill"], unique=False)
    op.create_table("projects",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("linear", sa.Text(), nullable=True),
        sa.Column("github_issues", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("lead_person_id", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("icon", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("repo_map_history",
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("changed_at", _TS, nullable=True),
        sa.Column("by_station", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True)
        )
    op.create_table("repo_maps",
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("repo")
        )
    op.create_table("report_texts",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("figures", sa.Text(), nullable=True),
        sa.Column("written_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("kind", "key")
        )
    op.create_table("report_weeks",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("week", sa.Date(), nullable=False),
        sa.Column("merged", sa.Integer(), nullable=True),
        sa.Column("start_to_merge", sa.Text(), nullable=True),
        sa.Column("stage_hours", sa.Text(), nullable=True),
        sa.Column("waited_hours", sa.Double(), nullable=True),
        sa.Column("milestones_accepted", sa.Integer(), nullable=True),
        sa.Column("reviewed", sa.Integer(), nullable=True),
        sa.Column("passed_first", sa.Integer(), nullable=True),
        sa.Column("rounds", sa.Text(), nullable=True),
        sa.Column("findings", sa.Text(), nullable=True),
        sa.Column("questions", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Double(), nullable=True),
        sa.Column("unpriced", sa.Integer(), nullable=True),
        sa.Column("cost_by_role", sa.Text(), nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("project", "week")
        )
    op.create_table("review_findings",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("ticket", sa.Text(), nullable=True),
        sa.Column("review", sa.Text(), nullable=True),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("severity", sa.Text(), nullable=True),
        sa.Column("file", sa.Text(), nullable=True),
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("blocking", sa.Boolean(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("fixed_in", sa.Text(), nullable=True),
        sa.Column("found_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("reviews",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("ticket", sa.Text(), nullable=True),
        sa.Column("round", sa.Integer(), nullable=True),
        sa.Column("pr", sa.Text(), nullable=True),
        sa.Column("head", sa.Text(), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=True),
        sa.Column("verified", sa.Text(), nullable=True),
        sa.Column("not_verified", sa.Text(), nullable=True),
        sa.Column("by_station", sa.Text(), nullable=True),
        sa.Column("reviewed_at", _TS, nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("rule_proposals",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("tickets", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("decided_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("session_archive_parts",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("part", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("first_seq", sa.Integer(), nullable=True),
        sa.Column("last_seq", sa.Integer(), nullable=True),
        sa.Column("lines", sa.Integer(), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column("md5", sa.Text(), nullable=True),
        sa.Column("key", sa.Text(), nullable=True),
        sa.Column("written_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("session", "part")
        )
    op.create_table("session_commands",
        sa.Column("tool_use_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("session", sa.Text(), nullable=True),
        sa.Column("at", _TS, nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("output_tail", sa.Text(), nullable=True),
        sa.Column("result_at", _TS, nullable=True),
        sa.Column("cwd", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("tool_use_id")
        )
    op.create_index("session_commands_session_at", "session_commands", ["session", "at"], unique=False)
    op.create_table("session_files",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("last_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("session", "path")
        )
    op.create_index("session_files_session_last_at", "session_files", ["session", "last_at"], unique=False)
    op.create_table("session_lines",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("line_key", sa.Text(), nullable=True),
        sa.Column("at", _TS, nullable=True),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("labels", sa.Text(), nullable=True),
        sa.Column("render", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session", "seq")
        )
    op.create_index("session_lines_session_at", "session_lines", ["session", "at"], unique=False)
    op.create_table("session_messages",
        sa.Column("tool_use_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("session", sa.Text(), nullable=True),
        sa.Column("at", _TS, nullable=True),
        sa.Column("to_station", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("tool_use_id")
        )
    op.create_index("session_messages_session_at", "session_messages", ["session", "at"], unique=False)
    op.create_table("session_meters",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("status_at", _TS, nullable=True),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("context_at", _TS, nullable=True),
        sa.Column("context_asked_at", _TS, nullable=True),
        sa.Column("status_person", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session")
        )
    op.create_table("session_rollup_state",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("ready_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("name")
        )
    op.create_table("session_said",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("line_key", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("at", _TS, nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session", "line_key")
        )
    op.create_index("session_said_session_at", "session_said", ["session", "at"], unique=False)
    op.create_table("session_states",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("state_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("session")
        )
    op.create_table("session_stations",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("station", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=True),
        sa.Column("parent", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("seen_at", _TS, nullable=True),
        sa.Column("crew_id", sa.Text(), nullable=True),
        sa.Column("runtime", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session")
        )
    op.create_table("session_turns",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("line_key", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("at", _TS, nullable=True),
        sa.Column("person", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session", "line_key")
        )
    op.create_index("session_turns_at", "session_turns", ["at"], unique=False)
    op.create_index("session_turns_session_at", "session_turns", ["session", "at"], unique=False)
    op.create_table("session_usage",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("replied_at", _TS, nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("subagent", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Double(), nullable=True),
        sa.Column("cache_write_1h_tokens", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("session", "message")
        )
    op.create_index("session_usage_replied_at", "session_usage", ["replied_at"], unique=False)
    op.create_table("session_usage_hourly",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("hour", _TS, nullable=False),
        sa.Column("project", sa.Text(), server_default="", nullable=False),
        sa.Column("replies", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Double(), nullable=True),
        sa.Column("unpriced", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("session", "hour", "project")
        )
    op.create_index("session_usage_hourly_hour", "session_usage_hourly", ["hour"], unique=False)
    op.create_table("sessions",
        sa.Column("session", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("started_at", _TS, nullable=True),
        sa.Column("last_line_at", _TS, nullable=True),
        sa.Column("line_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("coordination_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lines_complete", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("lines_origin", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session")
        )
    op.create_index("sessions_last_line_at", "sessions", ["last_line_at"], unique=False)
    op.create_table("settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("key")
        )
    op.create_table("shared_skills",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("made_by", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("name")
        )
    op.create_table("signoffs",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.Column("at", _TS, nullable=True),
        sa.Column("via", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("project", "repo")
        )
    op.create_table("spec_questions",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("to_person_id", sa.Text(), nullable=True),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("asked_by", sa.Text(), nullable=True),
        sa.Column("asked_at", _TS, nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_by", sa.Text(), nullable=True),
        sa.Column("answered_at", _TS, nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("via", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("project", "id")
        )
    op.create_table("ticket_checks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("ticket", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("commit_sha", sa.Text(), nullable=True),
        sa.Column("broke_test", sa.Text(), nullable=True),
        sa.Column("by_station", sa.Text(), nullable=True),
        sa.Column("by_session", sa.Text(), nullable=True),
        sa.Column("checked_at", _TS, nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_table("ticket_history",
        sa.Column("ticket", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("changed_at", _TS, nullable=True),
        sa.Column("field", sa.Text(), nullable=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("by_station", sa.Text(), nullable=True),
        sa.Column("by_role", sa.Text(), nullable=True),
        sa.Column("person_id", sa.Text(), nullable=True)
        )
    op.create_index("ticket_history_ticket", "ticket_history", ["ticket"], unique=False)
    op.create_table("ticket_numbers",
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("last", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("project")
        )
    op.create_table("tickets",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=True),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("identifier", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("position", sa.Double(), nullable=True),
        sa.Column("working_doc", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=True),
        sa.Column("updated_at", _TS, nullable=True),
        sa.Column("milestone", sa.Text(), nullable=True),
        sa.Column("depends_on", sa.Text(), nullable=True),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("holder", sa.Text(), nullable=True),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("stage_reason", sa.Text(), nullable=True),
        sa.Column("pr", sa.Text(), nullable=True),
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("assignee_person_id", sa.Text(), nullable=True),
        sa.Column("assignee_raw", sa.Text(), nullable=True),
        sa.Column("pr_repo", sa.Text(), nullable=True),
        sa.Column("pr_number", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id")
        )
    op.create_index("tickets_pr", "tickets", ["pr_repo", "pr_number"], unique=False)
    op.create_index("tickets_project", "tickets", ["project"], unique=False)
    op.create_table("waiting_told",
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("item", sa.Text(), nullable=False),
        sa.Column("told_at", _TS, nullable=True),
        sa.Column("sent", sa.Boolean(), nullable=True),
        sa.Column("ask", sa.Text(), nullable=True),
        sa.Column("channel", sa.Text(), nullable=True),
        sa.Column("ts", sa.Text(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=True),
        sa.Column("closed", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("person_id", "item")
        )


def downgrade() -> None:
    for name in reversed(TABLES):
        op.drop_table(name)
