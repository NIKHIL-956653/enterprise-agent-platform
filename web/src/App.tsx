import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { useAuth } from "./auth";
import { Agents } from "./pages/Agents";
import { Documents } from "./pages/Documents";
import { Login } from "./pages/Login";
import { RunDetailPage } from "./pages/RunDetail";
import { Runs } from "./pages/Runs";

export function App() {
  const { me, loading } = useAuth();

  if (loading) return <div className="center muted">Loading…</div>;
  if (!me) return <Login />;

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Navigate to="/agents" replace />} />
        <Route path="/agents" element={<Agents />} />
        <Route path="/runs" element={<Runs />} />
        <Route path="/runs/:id" element={<RunDetailPage />} />
        <Route path="/documents" element={<Documents />} />
        <Route path="*" element={<Navigate to="/agents" replace />} />
      </Routes>
    </Layout>
  );
}
