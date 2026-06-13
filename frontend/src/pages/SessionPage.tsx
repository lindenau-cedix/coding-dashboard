import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import SessionTerminalModal from "../components/SessionTerminalModal";
import { ErrorText, Spinner } from "../components/ui";
import type { Agent, Project } from "../types";

export default function SessionPage() {
  const { id: projectId = "", taskId = "" } = useParams();
  const navigate = useNavigate();
  const [project, setProject] = useState<Project | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const sess = await api.getSession(taskId);
        const [p, ag] = await Promise.all([
          api.getProject(sess.project_id || projectId),
          api.agents(),
        ]);
        if (!active) return;
        setProject(p);
        setAgents(ag);
      } catch (err) {
        if (active) setError(err instanceof Error ? err.message : "Session konnte nicht geladen werden");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [projectId, taskId]);

  if (loading) {
    return (
      <div className="flex justify-center py-16 text-slate-500">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }
  if (!project) {
    return <ErrorText>{error || "Session nicht gefunden."}</ErrorText>;
  }

  return (
    <SessionTerminalModal
      project={project}
      agents={agents}
      taskId={taskId}
      onClose={() => navigate(`/projects/${project.id}`)}
      onEnded={() => undefined}
    />
  );
}
