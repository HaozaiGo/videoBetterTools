import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5175,
    proxy: {
      "/api": "http://localhost:8010",
      "/uploads": "http://localhost:8010",
    },
  },
});
