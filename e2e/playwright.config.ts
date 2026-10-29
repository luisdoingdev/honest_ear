import { defineConfig, devices } from "@playwright/test";

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
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:8000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
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
