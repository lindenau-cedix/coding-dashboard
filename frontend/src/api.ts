import type {
  Agent,
  Project,
  ProjectCreatePayload,
  Task,
} from "./types";

const API_BASE_KEY = "cd_api_base";
const TOKEN_KEY = "cd_token";

export function getApiBase(): string {
  const stored = localStorage.getItem(API_BASE_KEY);
  if (stored) return stored.replace(/\/+$/, "");
  const env = import.meta.env.VITE_API_BASE;
  if (env) return env.replace(/\/+$/, "");
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

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let res: Response;
  try {
    res = await fetch(`${getApiBase()}/api${path}`, {
      method,
      headers,
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
  agents: () => request<Agent[]>("GET", "/agents"),

  listProjects: () => request<Project[]>("GET", "/projects"),
  createProject: (data: ProjectCreatePayload) =>
    request<Project>("POST", "/projects", data),
  getProject: (id: string) => request<Project>("GET", `/projects/${id}`),
  deleteProject: (id: string, deleteRemote: boolean) =>
    request<void>("DELETE", `/projects/${id}?delete_remote=${deleteRemote}`),
  agentsMd: (id: string) =>
    request<{ exists: boolean; content: string }>(
      "GET",
      `/projects/${id}/agents-md`,
    ),

  listTasks: (projectId: string) =>
    request<Task[]>("GET", `/projects/${projectId}/tasks`),
  createTask: (projectId: string, agent: string, prompt: string) =>
    request<Task>("POST", `/projects/${projectId}/tasks`, { agent, prompt }),
  getTask: (id: string) => request<Task>("GET", `/tasks/${id}`),
  stopTask: (id: string) =>
    request<{ stopped: boolean }>("POST", `/tasks/${id}/stop`),
};

export function commitUrl(project: Project, hash: string): string | null {
  if (!project.github_url || !hash) return null;
  return `${project.github_url}/commit/${hash}`;
}
