import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AuthProvider } from "./auth";
import RuntimeEnvironmentBadge from "./components/RuntimeEnvironmentBadge";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RuntimeEnvironmentBadge />
    <AuthProvider>
      <App />
    </AuthProvider>
  </StrictMode>,
);
