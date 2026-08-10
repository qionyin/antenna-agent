import { defineConfig, devices } from "@playwright/test";

// 默认使用本机 Edge 渠道，也允许 CI 或本地环境覆盖浏览器渠道。
const browserChannel = process.env.PLAYWRIGHT_BROWSER_CHANNEL || "msedge";

// 前端 smoke 测试配置：启动 Vite 服务，并覆盖桌面和移动视口。
export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "on-first-retry"
  },
  projects: [
    {
      name: "chromium-desktop",
      use: {
        ...devices["Desktop Chrome"],
        channel: browserChannel,
        viewport: { width: 1366, height: 900 }
      }
    },
    {
      name: "chromium-mobile",
      use: {
        ...devices["Pixel 5"],
        channel: browserChannel
      }
    }
  ],
  webServer: {
    command: "npm run dev -- --strictPort",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000
  }
});
