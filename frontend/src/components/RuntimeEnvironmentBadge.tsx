import { useEffect, useState } from "react";

type Health = {
  status?: string;
  db_mode?: string;
};

export default function RuntimeEnvironmentBadge() {
  const [dbMode, setDbMode] = useState("");
  const configuredLabel = (import.meta.env.VITE_RUNTIME_LABEL || "").trim();

  useEffect(() => {
    const apiBase = (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");
    void fetch(`${apiBase}/api/health`)
      .then((response) => (response.ok ? response.json() as Promise<Health> : null))
      .then((health) => setDbMode(health?.db_mode || ""))
      .catch(() => undefined);
  }, []);

  const modeLabel = dbMode === "production_readonly"
    ? "PRODUCTION · READ ONLY"
    : dbMode === "local"
      ? "LOCAL INTEGRATION"
      : "";
  const label = configuredLabel || modeLabel;
  if (!label) return null;

  return (
    <div
      className={`runtime-environment-badge runtime-environment-badge--${dbMode || "configured"}`}
      role="status"
      aria-label={`실행 환경: ${label}`}
    >
      {label}{dbMode ? ` · DB ${dbMode}` : ""}
    </div>
  );
}
