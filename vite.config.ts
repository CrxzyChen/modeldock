import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vitest/config";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  root: fileURLToPath(new URL("./client", import.meta.url)),
  base: "./",
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./client/src", import.meta.url))
    }
  },
  build: {
    outDir: fileURLToPath(new URL("./client/dist", import.meta.url)),
    emptyOutDir: false,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/app.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: asset => asset.name?.endsWith(".css") ? "assets/app.css" : "assets/[name][extname]"
      }
    }
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.spec.ts"]
  }
});
