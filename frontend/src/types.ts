export interface Agent {
  key: string;
  display_name: string;
  enabled: boolean;
  supports_goal: boolean;
}

export type TaskMode = "task" | "goal";

export interface Project {
  id: string;
  name: string;
  slug: string;
  description: string;
  github_full_name: string;
  github_url: string;
  default_branch: string;
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
  status: TaskStatus;
  exit_code: number | null;
  result_summary: string;
  error: string;
  branch: string;
  commit_hash: string;
  commit_message: string;
  commit_created: boolean;
  pushed: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  output?: string;
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
