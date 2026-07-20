import { useAuth } from "./auth";
import LoginPage from "./pages/LoginPage";
import MapPage from "./pages/MapPage";

export default function App() {
  const { user, loading } = useAuth();

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

  return user ? <MapPage /> : <LoginPage />;
}
