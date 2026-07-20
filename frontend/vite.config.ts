import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";

// 기본은 HTTP. HTTPS가 필요할 때만: set VITE_DEV_HTTPS=1 && npm run dev
const useHttps = process.env.VITE_DEV_HTTPS === "1" || process.env.VITE_DEV_HTTPS === "true";

export default defineConfig({
  plugins: useHttps ? [react(), basicSsl()] : [react()],
  server: {
    port: 5173,
    host: true,
    https: useHttps,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
