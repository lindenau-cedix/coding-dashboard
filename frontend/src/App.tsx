import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import Layout from "./components/Layout";
import { Spinner } from "./components/ui";
import Login from "./pages/Login";
import Projects from "./pages/Projects";
import ProjectDetail from "./pages/ProjectDetail";

function Splash() {
  return (
    <div className="flex h-full items-center justify-center text-slate-400">
      <Spinner className="h-6 w-6" />
    </div>
  );
}

function Protected() {
  const { token, ready } = useAuth();
  if (!ready) return <Splash />;
  if (!token) return <Navigate to="/login" replace />;
  return <Layout />;
}

export default function App() {
  const { token, ready } = useAuth();
  return (
    <Routes>
      <Route
        path="/login"
        element={!ready ? <Splash /> : token ? <Navigate to="/" replace /> : <Login />}
      />
      <Route element={<Protected />}>
        <Route path="/" element={<Projects />} />
        <Route path="/projects/:id" element={<ProjectDetail />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
