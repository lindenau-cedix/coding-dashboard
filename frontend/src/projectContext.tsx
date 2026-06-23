import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { api } from "./api";
import type { Agent, Project } from "./types";

/** Shared context for cross-cutting concerns that aren't tied to a single
 *  page: the agents list (so the floating WindowManager can render session
 *  tabs) and the currently-focused Project (so a freshly-opened window
 *  doesn't refetch one we already have).  Pages that have richer state
 *  continue to manage it locally — this is a fallback only. */
interface ProjectContextValue {
  agents: Agent[];
  project: Project | null;
  setProject: (p: Project | null) => void;
  setAgents: (a: Agent[]) => void;
}

const ProjectContext = createContext<ProjectContextValue>({
  agents: [],
  project: null,
  setProject: () => undefined,
  setAgents: () => undefined,
});

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [project, setProject] = useState<Project | null>(null);

  // Hydrate the agent list once at the app root so every page (and the
  // floating WindowManager) doesn't repeat the request.
  useEffect(() => {
    let active = true;
    api
      .agents()
      .then((a) => {
        if (active) setAgents(a);
      })
      .catch(() => {
        /* cosmetic */
      });
    return () => {
      active = false;
    };
  }, []);

  const value = useMemo(
    () => ({ agents, project, setProject, setAgents }),
    [agents, project],
  );
  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProject(): ProjectContextValue {
  return useContext(ProjectContext);
}
