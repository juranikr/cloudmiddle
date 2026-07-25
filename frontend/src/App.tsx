import { useAuth } from "./auth";
import AdminPage from "./pages/AdminPage";
import LoginPage from "./pages/LoginPage";
import MapPage from "./pages/MapPage";

function pathIsAdmin(): boolean {
  const p = window.location.pathname.replace(/\/+$/, "") || "/";
  return p === "/admin";
}

export default function App() {
  const { user, loading } = useAuth();
  const adminRoute = pathIsAdmin();

  if (loading) {
    return (
      <div className="login">
        <div className="login__bg" aria-hidden />
        <div className="login__card">
          <p className="login__eyebrow">China Travel Notes</p>
          <h1 className="login__title">지난 여행 지도</h1>
          <p className="login__lead">불러오는 중…</p>
        </div>
      </div>
    );
  }

  if (!user) {
    return <LoginPage />;
  }

  if (adminRoute) {
    return <AdminPage />;
  }

  return <MapPage />;
}
