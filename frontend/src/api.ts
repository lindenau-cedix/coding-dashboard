import type {
  Agent,
  Project,
  ProjectCreatePayload,
  Task,
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
  createTask: (
    projectId: string,
    agent: string,
    prompt: string,
    mode: TaskMode = "task",
  ) => request<Task>("POST", `/projects/${projectId}/tasks`, { agent, prompt, mode }),
  getTask: (id: string) => request<Task>("GET", `/tasks/${id}`),
  stopTask: (id: string) =>
    request<{ stopped: boolean }>("POST", `/tasks/${id}/stop`),
  pullProject: (id: string) =>
    request<{ ok: boolean; branch: string; output: string }>("POST", `/projects/${id}/pull`),
};

export function commitUrl(project: Project, hash: string): string | null {
  if (!project.github_url || !hash) return null;
  return `${project.github_url}/commit/${hash}`;
}
