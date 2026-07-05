import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import Layout from "./components/Layout";
import { Spinner } from "./components/ui";
import AgentWindowPage from "./pages/AgentWindowPage";
import Heartbeat from "./pages/Heartbeat";
import Login from "./pages/Login";
import Projects from "./pages/Projects";
import ProjectDetail from "./pages/ProjectDetail";
import SessionPage from "./pages/SessionPage";
import { ProjectProvider } from "./projectContext";

function Splash() {
  return (
    <div className="flex h-full items-center justify-center text-slate-400">
      <Spinner className="h-6 w-6" />
    </div>
  );
}

function Protected() {
  const { token, ready, authRequired } = useAuth();
  if (!ready) return <Splash />;
  if (authRequired && !token) return <Navigate to="/login" replace />;
  return <Layout />;
}

export default function App() {
  const { token, ready, authRequired } = useAuth();
  return (
    <ProjectProvider>
      <Routes>
        <Route
          path="/login"
          element={
            !ready ? (
              <Splash />
            ) : token || !authRequired ? (
              <Navigate to="/" replace />
            ) : (
              <Login />
            )
          }
        />
        <Route element={<Protected />}>
          <Route path="/" element={<Projects />} />
          <Route path="/projects/:id" element={<ProjectDetail />} />
          <Route path="/projects/:id/sessions/:taskId" element={<SessionPage />} />
          <Route path="/heartbeat" element={<Heartbeat />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>

        {/* Standalone agent windows — rendered OUTSIDE the dashboard Layout
            so they fill their own browser tab without the header / main
            width constraint. Still gated on auth via a tiny ProtectedInline
            wrapper so a missing token sends the user to /login. */}
        <Route
          path="/windows/task/:taskId"
          element={
            <RequireAuthInline>
              <AgentWindowPage kind="task" />
            </RequireAuthInline>
          }
        />
        <Route
          path="/windows/session/:taskId"
          element={
            <RequireAuthInline>
              <AgentWindowPage kind="session" />
            </RequireAuthInline>
          }
        />
      </Routes>
    </ProjectProvider>
  );
}

/** Tiny auth guard for routes that intentionally don't render the dashboard
 *  Layout (popup tabs). Same logic as Protected but renders a minimal splash
 *  / login link instead of nesting inside Layout — that way the popup's
 *  body fills its viewport, not a flex container with the dashboard header. */
function RequireAuthInline({ children }: { children: React.ReactNode }) {
  const { token, ready, authRequired } = useAuth();
  if (!ready) return <Splash />;
  if (authRequired && !token) return <Navigate to="/login" replace />;
  return <>{children}</>;
}