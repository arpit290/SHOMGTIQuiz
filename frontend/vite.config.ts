import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],

  server: {
    host: "0.0.0.0",
    port: 5173,

    // Required for the Cloudflare trycloudflare.com hostname
    allowedHosts: [".trycloudflare.com"],

    proxy: {
      // Browser request:
      //   https://your-tunnel.trycloudflare.com/api/...
      //
      // Vite forwards it internally to:
      //   http://127.0.0.1:8000/api/...
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },

      // Browser request:
      //   wss://your-tunnel.trycloudflare.com/ws/...
      //
      // Vite forwards it to:
      //   ws://127.0.0.1:8000/ws/...
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});