export interface Agent {
  key: string;
  display_name: string;
  enabled: boolean;
  supports_goal: boolean;
  supports_session: boolean;
  /** Selectable models/effort levels; empty array = no selector. */
  model_choices: string[];
  effort_choices: string[];
}

export type TaskMode = "task" | "goal" | "session";

export interface Project {
  id: string;
  name: string;
  slug: string;
  description: string;
  github_full_name: string;
  github_url: string;
  default_branch: string;
  /** Archived projects are hidden from the default list. */
  archived: boolean;
  /** ISO-8601 timestamp when the project was archived, or null. */
  archived_at: string | null;
  /** Per-project heartbeat opt-out (default true on the server). */
  heartbeat_enabled: boolean;
  /** ISO-8601 timestamp of the last heartbeat tick that touched this project. */
  last_heartbeat_at: string | null;
  /** Short status string: "" (never ticked) | "success" | "no_issues" |
   *  "cooldown" | "disabled" | "error" | "skipped". */
  last_heartbeat_status: string;
  /** One-line error message when last_heartbeat_status === "error". */
  last_heartbeat_error: string;
  created_at: string;
  updated_at: string;
  local_path?: string;
  clone_url?: string;
}

export type TaskStatus =
  | "queued"
  | "running"
  | "success"
  | "failed"
  | "error"
  | "interrupted"
  | "cancelled";

export interface Task {
  id: string;
  project_id: string;
  agent: string;
  prompt: string;
  mode: TaskMode;
  /** Selected model/effort ("" = agent default). */
  model: string;
  effort: string;
  /** Filenames of attached images (served via /api/tasks/{id}/images/{name}). */
  images: string[];
  /** Whether this task is an interactive session. */
  is_session: boolean;
  /** Chat history for session tasks (JSON list of {role, content, timestamp}). */
  chat_history: SessionMessage[];
  status: TaskStatus;
  exit_code: number | null;
  result_summary: string;
  error: string;
  branch: string;
  /** "" (n/a) | "merged" (landed on default branch) | "conflict" (branch kept for manual merge). */
  merge_state?: string;
  commit_hash: string;
  commit_message: string;
  commit_created: boolean;
  pushed: boolean;
  /** True when this task was auto-spawned by the dashboard heartbeat
   *  (vs hand-created by the user). UI shows a "🤖 Auto-Fix" badge. */
  heartbeat_spawned: boolean;
  /** GitHub issue number that triggered this heartbeat-spawned task, or null. */
  heartbeat_issue_number: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  output?: string;
}

/** A running/queued task enriched with its project's name/slug (start-page dashboard). */
export interface RunningTask extends Task {
  project_name: string;
  project_slug: string;
}

/** One entry in a project directory listing. */
export interface FileEntry {
  name: string;
  /** POSIX path relative to the project root. */
  path: string;
  is_dir: boolean;
  size: number;
}

export interface DirListing {
  /** The listed directory, relative to the project root ("" = root). */
  path: string;
  entries: FileEntry[];
}

export interface FileContent {
  path: string;
  size: number;
  is_binary: boolean;
  truncated: boolean;
  content: string;
}

/** One turn in a session chat history. */
export interface SessionMessage {
  role: "user" | "assistant";
  content: string;
  timestamp: string;
}

/** WebSocket message envelope for session mode. */
export type SessionWsMessage =
  | { type: "started"; task_id: string }
  | { type: "output"; data: string; offset?: number }
  | { type: "message"; role: "user" | "assistant"; content: string }
  | { type: "status"; status: string }
  | { type: "done"; task_id: string; status: string; summary?: string }
  | { type: "git"; data: string }
  | { type: "error"; message: string }
  | { type: string; [k: string]: unknown };

/** Upload payload for one image attached to a new task. */
export interface TaskImagePayload {
  name: string;
  /** Base64 content, data-URL form ("data:image/png;base64,...") is fine. */
  data: string;
}

/** One repo as returned by GET /api/projects/from-github. */
export interface GithubRepo {
  full_name: string;
  name: string;
  description: string;
  private: boolean;
  clone_url: string;
  html_url: string;
  default_branch: string;
  fork: boolean;
  archived: boolean;
  already_imported: boolean;
}

export interface ProjectCreatePayload {
  name: string;
  description?: string;
  mode: "create" | "import";
  private?: boolean;
  repo?: string;
}

// WebSocket message envelopes
export type WsMessage =
  | { type: "status"; status: TaskStatus }
  | { type: "output"; data: string }
  | { type: "git"; data: string }
  | { type: "done"; task: Task }
  | { type: "error"; message: string }
  | { type: string; [k: string]: unknown };

/** One project's heartbeat snapshot, as returned by GET /api/heartbeat. */
export interface HeartbeatProjectStatus {
  id: string;
  name: string;
  slug: string;
  enabled: boolean;
  github_full_name: string;
  last_heartbeat_at: string | null;
  last_issue_poll_at: string | null;
  last_heartbeat_status: string;
  last_heartbeat_error: string;
  open_issues_count: number;
  inflight_task_ids: string[];
}

/** Overall heartbeat state. */
export interface HeartbeatStatus {
  enabled: boolean;
  interval_seconds: number;
  agent_key: string;
  cooldown_minutes: number;
  last_tick_at: string | null;
  last_tick_summary: string | null;
  projects: HeartbeatProjectStatus[];
}

/** One row from the heartbeat_seen ledger (issue the dashboard has
 *  already considered dispatching for). */
export interface HeartbeatIssueSeen {
  project_id: string;
  issue_number: number;
  issue_title: string;
  issue_url: string;
  first_seen_at: string;
  dispatched_task_id: string | null;
}

/** One open GitHub issue as returned by GET /api/projects/{id}/heartbeat/open. */
export interface OpenGithubIssue {
  number: number;
  title: string;
  html_url: string;
  user: string;
  labels: string[];
  updated_at: string | null;
  created_at: string | null;
  body: string;
}
