import { useState, type FormEvent } from "react";
import { useAuth } from "../auth";
import BrandMark from "../components/BrandMark";

export default function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(email.trim(), password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "로그인 실패");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <div className="login__bg" aria-hidden />
      <div className="login__card">
        <BrandMark />
        <p className="login__eyebrow">CHINA TRAVEL, CURATED TOGETHER</p>
        <p className="login__lead">
          지도, 장소 기록, 여행 일정과 현지 조사를 한곳에. 먼 곳에서 온 친구처럼 도시를 천천히 알아갑니다.
        </p>

        <form className="login__form" onSubmit={onSubmit}>
          <label>
            이메일
            <input
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label>
            비밀번호
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error ? <p className="login__error">{error}</p> : null}
          <button type="submit" disabled={busy}>
            {busy ? "로그인 중…" : "로그인"}
          </button>
        </form>
      </div>
    </div>
  );
}
