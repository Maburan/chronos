import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { TopBar } from "./components/TopBar";
import { LoginPage } from "./pages/LoginPage";
import { RegisterPage } from "./pages/RegisterPage";
import { ProtectedRoute } from "./pages/ProtectedRoute";
import { LandingPage } from "./pages/LandingPage";
import { DashboardPage } from "./pages/DashboardPage";
import { WorkflowsListPage } from "./pages/WorkflowsListPage";
import { WorkflowNewPage } from "./pages/WorkflowNewPage";
import { WorkflowDetailPage } from "./pages/WorkflowDetailPage";
import { WorkflowBuilderPage } from "./pages/WorkflowBuilderPage";

function App() {
  const { user } = useAuth();

  return (
    <div className="app-shell">
      <TopBar />
      <main className="app-main">
        <Routes>
          <Route
            path="/login"
            element={user ? <Navigate to="/" replace /> : <LoginPage />}
          />
          <Route
            path="/register"
            element={user ? <Navigate to="/" replace /> : <RegisterPage />}
          />
          <Route path="/" element={<RootRoute />} />
          <Route
            path="/workflows"
            element={
              <ProtectedRoute>
                <WorkflowsListPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/workflows/new"
            element={
              <ProtectedRoute>
                <WorkflowNewPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/workflows/:id"
            element={
              <ProtectedRoute>
                <WorkflowDetailPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/workflows/:id/builder"
            element={
              <ProtectedRoute>
                <WorkflowBuilderPage />
              </ProtectedRoute>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

/** The root path is the landing page for visitors and the dashboard for
 * signed-in users. Gate on the initial auth check so a reload with a valid
 * token doesn't flash the landing page before the session resolves. */
function RootRoute() {
  const { user, loading } = useAuth();
  if (loading) {
    return (
      <div className="auth-wrap">
        <div className="empty">
          <div className="empty-icon">
            <span className="health-dot checking" style={{ width: 10, height: 10 }} />
          </div>
          <div className="empty-title">Loading Chronos…</div>
        </div>
      </div>
    );
  }
  return user ? <DashboardPage /> : <LandingPage />;
}

export default App;
