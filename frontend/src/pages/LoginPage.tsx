import { useState, type FormEvent } from "react";
import { useAuth } from "../auth";

export default function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState("alice@test.com");
  const [password, setPassword] = useState("test1234");
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
        <p className="login__eyebrow">China Travel Notes</p>
        <h1 className="login__title">지난 여행 지도</h1>
        <p className="login__lead">
          한국 여행자들이 모아 두는 지난(济南) 스팟 기록. 로그인 후 마커를 남기고
          다른 사람의 핀도 함께 보세요.
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

        <div className="login__accounts">
          <p>테스트 계정 (비밀번호 모두 <code>test1234</code>)</p>
          <ul>
            <li>alice@test.com · 앨리스</li>
            <li>bob@test.com · 밥</li>
            <li>carol@test.com · 캐롤</li>
          </ul>
        </div>
      </div>
    </div>
  );
}
