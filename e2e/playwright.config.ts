import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig, devices } from "@playwright/test";

// On the Mac, read the admin login from the project's .env (in Docker, Compose's env_file provides it).
const envFile = resolve(__dirname, "../.env");
if (existsSync(envFile)) process.loadEnvFile(envFile);

// Chromium can stand in a test tone for a real microphone; Playwright's WebKit can't.
const fakeMicrophone = {
  permissions: ["microphone"],
  launchOptions: { args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"] },
};

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  // Screenshot baselines are not committed: a missing one is written on first run, then compared against.
  updateSnapshots: "missing",
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:8000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // CI keeps a video of every test so a run can be watched afterwards; locally only failures are kept.
    video: process.env.CI ? "on" : "retain-on-failure",
  },
  expect: {
    toHaveScreenshot: { maxDiffPixelRatio: 0.01 },
  },
  projects: [
    { name: "desktop-chrome", use: { ...devices["Desktop Chrome"], ...fakeMicrophone } },
    { name: "iphone-15", use: { ...devices["iPhone 15"] } },
    { name: "iphone-se", use: { ...devices["iPhone SE"] } },
    { name: "pixel-7", use: { ...devices["Pixel 7"], ...fakeMicrophone } },
  ],
});
