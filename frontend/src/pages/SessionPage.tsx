import { Navigate, useParams } from "react-router-dom";

/**
 * Legacy /projects/:id/sessions/:taskId route — kept as an alias for older
 * links / bookmarks. Now that sessions live in their own browser tab we
 * forward into the dedicated agent window route, which renders the focused
 * view without the dashboard chrome.
 */
export default function SessionPage() {
  const { taskId = "" } = useParams();
  if (!taskId) return <Navigate to="/" replace />;
  return <Navigate to={`/windows/session/${taskId}`} replace />;
}