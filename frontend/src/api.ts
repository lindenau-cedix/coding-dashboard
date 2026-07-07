import type {
  Agent,
  DirListing,
  FileContent,
  GithubRepo,
  HeartbeatIssueSeen,
  HeartbeatStatus,
  OpenGithubIssue,
  Project,
  ProjectCreatePayload,
  RunningTask,
  SessionMessage,
  Task,
  TaskImagePayload,
  TaskMode,
} from "./types";

const API_BASE_KEY = "cd_api_base";
const TOKEN_KEY = "cd_token";
const BUILT_API_BASE = normalizeBase(import.meta.env.VITE_API_BASE);
const CF_ACCESS_CLIENT_ID = import.meta.env.VITE_CF_ACCESS_CLIENT_ID?.trim() || "";
const CF_ACCESS_CLIENT_SECRET =
  import.meta.env.VITE_CF_ACCESS_CLIENT_SECRET?.trim() || "";

let cloudflareBootstrapBase = "";
let cloudflareBootstrapPromise: Promise<void> | null = null;

function normalizeBase(value: string | undefined | null): string {
  return (value ?? "").trim().replace(/\/+$/, "");
}

function getCloudflareAccessHeaders(): Record<string, string> {
  const apiBase = getApiBase();
  if (!apiBase || apiBase !== BUILT_API_BASE) return {};
  if (!CF_ACCESS_CLIENT_ID || !CF_ACCESS_CLIENT_SECRET) return {};
  return {
    "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
    "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
  };
}

export function getApiBase(): string {
  const stored = localStorage.getItem(API_BASE_KEY);
  if (stored) return normalizeBase(stored);
  if (BUILT_API_BASE) return BUILT_API_BASE;
  return ""; // same origin
}

export function setApiBase(value: string): void {
  const v = value.trim().replace(/\/+$/, "");
  if (v) localStorage.setItem(API_BASE_KEY, v);
  else localStorage.removeItem(API_BASE_KEY);
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

/** Build a WebSocket URL for an /api path, honouring the configured base. */
export function wsUrl(path: string): string {
  const base = getApiBase();
  if (base) return base.replace(/^http/, "ws") + path;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}

export function wsSessionUrl(taskId: string, offset = 0): string {
  const token = getToken() ?? "";
  return wsUrl(
    `/api/ws/sessions/${taskId}?token=${encodeURIComponent(token)}&offset=${Math.max(0, offset)}`,
  );
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function ensureCloudflareAccess(): Promise<void> {
  const apiBase = getApiBase();
  const headers = getCloudflareAccessHeaders();
  if (!apiBase || !Object.keys(headers).length) return;

  if (cloudflareBootstrapPromise && cloudflareBootstrapBase === apiBase) {
    await cloudflareBootstrapPromise;
    return;
  }

  cloudflareBootstrapBase = apiBase;
  cloudflareBootstrapPromise = (async () => {
    try {
      await fetch(`${apiBase}/api/auth/me`, {
        method: "GET",
        headers,
        credentials: "include",
      });
    } catch {
      throw new ApiError(0, "Netzwerkfehler – Backend nicht erreichbar.");
    }
  })();

  try {
    await cloudflareBootstrapPromise;
  } catch (error) {
    cloudflareBootstrapBase = "";
    cloudflareBootstrapPromise = null;
    throw error;
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = getCloudflareAccessHeaders();
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${getApiBase()}/api${path}`, {
      method,
      headers,
      credentials: "include",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiError(0, "Netzwerkfehler – Backend nicht erreichbar.");
  }

  if (res.status === 401) {
    setToken(null);
    window.dispatchEvent(new Event("cd-unauthorized"));
    throw new ApiError(401, "Nicht authentifiziert.");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  login: (username: string, password: string) =>
    request<{ access_token: string; username: string }>("POST", "/auth/login", {
      username,
      password,
    }),
  me: () => request<{ username: string }>("GET", "/auth/me"),
  authStatus: () =>
    request<{ auth_required: boolean }>("GET", "/auth/status"),
  agents: () => request<Agent[]>("GET", "/agents"),

  listProjects: (archived: "all" | "true" | "false" = "false") =>
    request<Project[]>("GET", `/projects?archived=${archived}`),
  createProject: (data: ProjectCreatePayload) =>
    request<Project>("POST", "/projects", data),
  getProject: (id: string) => request<Project>("GET", `/projects/${id}`),
  deleteProject: (id: string, deleteRemote: boolean) =>
    request<void>("DELETE", `/projects/${id}?delete_remote=${deleteRemote}`),
  archiveProject: (id: string) =>
    request<Project>("POST", `/projects/${id}/archive`),
  unarchiveProject: (id: string) =>
    request<Project>("POST", `/projects/${id}/unarchive`),
  agentsMd: (id: string) =>
    request<{ exists: boolean; content: string }>(
      "GET",
      `/projects/${id}/agents-md`,
    ),

  listTasks: (projectId: string) =>
    request<Task[]>("GET", `/projects/${projectId}/tasks`),
  createTask: (
    projectId: string,
    agent: string,
    prompt: string,
    mode: TaskMode = "task",
    model = "",
    effort = "",
    images: TaskImagePayload[] = [],
  ) =>
    request<Task>("POST", `/projects/${projectId}/tasks`, {
      agent,
      prompt,
      mode,
      model,
      effort,
      images,
    }),
  getTask: (id: string) => request<Task>("GET", `/tasks/${id}`),
  stopTask: (id: string) =>
    request<{ stopped: boolean }>("POST", `/tasks/${id}/stop`),
  pullProject: (id: string) =>
    request<{ ok: boolean; branch: string; output: string }>("POST", `/projects/${id}/pull`),

  // Cross-project dashboard of currently running/queued agents.
  listRunning: () => request<RunningTask[]>("GET", "/running"),

  // GitHub browse + bulk import ("autoclone all of the user's repos").
  listFromGithub: () =>
    request<{
      repos: GithubRepo[];
      user: string;
    }>("GET", "/projects/from-github"),
  syncFromGithub: (body: {
    full_names?: string[];
    include_forks?: boolean;
    include_archived?: boolean;
  }) =>
    request<{
      results: { full_name: string; status: "imported" | "skipped" | "failed"; detail: string; project_id: string }[];
      imported: number;
      skipped: number;
      failed: number;
    }>("POST", "/projects/sync-from-github", {
      full_names: body.full_names ?? [],
      include_forks: body.include_forks ?? true,
      include_archived: body.include_archived ?? true,
    }),

  // File browser
  listFiles: (projectId: string, path = "") =>
    request<DirListing>(
      "GET",
      `/projects/${projectId}/files?path=${encodeURIComponent(path)}`,
    ),
  readFile: (projectId: string, path: string) =>
    request<FileContent>(
      "GET",
      `/projects/${projectId}/file?path=${encodeURIComponent(path)}`,
    ),

  // Session mode
  createSession: (
    projectId: string,
    agent: string,
    model = "",
    effort = "",
    startArgs = "",
  ) =>
    request<{ task_id: string; status: string }>("POST", "/sessions", {
      project_id: projectId,
      agent,
      model,
      effort,
      start_args: startArgs,
    }),
  getSession: (taskId: string) =>
    request<{
      id: string;
      project_id: string;
      agent: string;
      model: string;
      effort: string;
      start_args: string;
      workdir?: string;
      chat_history: SessionMessage[];
      status: string;
      result_summary: string;
      output: string;
      is_session: boolean;
    }>("GET", `/sessions/${taskId}`),
  endSession: (taskId: string, commitMessage = "") =>
    request<{ status: string; summary: string; commit_hash: string; pushed: boolean }>(
      "POST",
      `/sessions/${taskId}/end`,
      { commit_message: commitMessage },
    ),

  // Heartbeat — auto-poll GitHub issues + auto-spawn Claude Code tasks.
  getHeartbeat: () => request<HeartbeatStatus>("GET", "/heartbeat"),
  setHeartbeatEnabled: (enabled: boolean) =>
    request<{ enabled: boolean }>(
      "POST",
      enabled ? "/heartbeat/enable" : "/heartbeat/disable",
    ),
  triggerHeartbeat: () =>
    request<{ triggered: boolean }>("POST", "/heartbeat/trigger"),
  setProjectHeartbeatEnabled: (projectId: string, enabled: boolean) =>
    request<{ id: string; heartbeat_enabled: boolean }>(
      "POST",
      `/projects/${projectId}/heartbeat/${enabled ? "enable" : "disable"}`,
    ),
  listHeartbeatIssues: (projectId: string) =>
    request<HeartbeatIssueSeen[]>(
      "GET",
      `/projects/${projectId}/heartbeat/issues`,
    ),
  listProjectOpenIssues: (projectId: string) =>
    request<{ issues: OpenGithubIssue[]; note?: string }>(
      "GET",
      `/projects/${projectId}/heartbeat/open`,
    ),
  /** Force a fresh dashboard status comment on a heartbeat-handled issue. */
  commentAgainOnHeartbeatIssue: (projectId: string, issueNumber: number) =>
    request<{ comment_id: number; comment_url: string; error: string }>(
      "POST",
      `/projects/${projectId}/heartbeat/issues/${issueNumber}/comment-again`,
    ),
  /** Manually close a heartbeat-handled GitHub issue. */
  closeHeartbeatIssue: (projectId: string, issueNumber: number) =>
    request<{ state: string; error: string }>(
      "POST",
      `/projects/${projectId}/heartbeat/issues/${issueNumber}/close`,
    ),
  /** Inverse of closeHeartbeatIssue — useful for "let the heartbeat retry this". */
  reopenHeartbeatIssue: (projectId: string, issueNumber: number) =>
    request<{ state: string; error: string }>(
      "POST",
      `/projects/${projectId}/heartbeat/issues/${issueNumber}/reopen`,
    ),
};

/**
 * Fetch a task image with auth headers (plain <img src> cannot send the
 * Bearer token) and return an object URL. Caller revokes it when done.
 */
export async function fetchTaskImage(
  taskId: string,
  name: string,
): Promise<string> {
  const headers: Record<string, string> = getCloudflareAccessHeaders();
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let res: Response;
  try {
    res = await fetch(
      `${getApiBase()}/api/tasks/${taskId}/images/${encodeURIComponent(name)}`,
      { headers, credentials: "include" },
    );
  } catch {
    throw new ApiError(0, "Netzwerkfehler – Backend nicht erreichbar.");
  }
  if (!res.ok) throw new ApiError(res.status, "Bild konnte nicht geladen werden.");
  return URL.createObjectURL(await res.blob());
}

export function commitUrl(project: Project, hash: string): string | null {
  if (!project.github_url || !hash) return null;
  return `${project.github_url}/commit/${hash}`;
}
