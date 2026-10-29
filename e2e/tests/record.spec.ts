import { test, expect } from "@playwright/test";
import { overflow, plainVhRules, watchForErrors } from "./helpers";

test.describe("record page", () => {
  test.beforeEach(async ({ page, browserName }) => {
    if (browserName !== "webkit") return;
    // Playwright's Linux WebKit is built without recording APIs that real iOS Safari has.
    // Stand in inert versions so the page renders as it does on an iPhone.
    await page.addInitScript(() => {
      if (!navigator.mediaDevices) {
        Object.defineProperty(navigator, "mediaDevices", {
          value: { getUserMedia: () => Promise.reject(new Error("no microphone in tests")) },
        });
      }
      if (!("MediaRecorder" in window)) {
        (window as any).MediaRecorder = class {
          static isTypeSupported() {
            return false;
          }
        };
      }
    });
  });

  test("loads and fits the screen", async ({ page }) => {
    const assertNoErrors = watchForErrors(page);
    await page.goto("/record/");

    await expect(page.getByRole("button", { name: "Start session" })).toBeVisible();
    const { sideways, downward } = await overflow(page);
    expect(sideways, "page is wider than the screen").toBeLessThanOrEqual(0);
    expect(downward, "page is taller than the screen").toBeLessThanOrEqual(0);
    expect(await plainVhRules(page), "use svh/dvh instead of vh").toEqual([]);

    assertNoErrors();
  });

  test("looks the same as the approved screenshot", async ({ page }) => {
    await page.goto("/record/");
    await page.evaluate(() => document.fonts.ready);
    await expect(page).toHaveScreenshot("record.png", { fullPage: true });
  });

  test("records, plays back, then deletes the recording", async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "Playwright can only fake a microphone in Chromium");
    const assertNoErrors = watchForErrors(page);
    await page.goto("/record/");

    await page.getByRole("button", { name: "Start session" }).click();
    await page.getByRole("button", { name: "Record", exact: true }).click();
    await expect(page.locator("#timer")).toHaveText("0:01");

    const uploadDone = page.waitForResponse(
      (res) => res.url().endsWith("/api/recordings/") && res.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Stop" }).click();
    const upload = await uploadDone;
    expect(upload.status()).toBe(201);
    const { audio_url } = await upload.json();

    const deleteDone = page.waitForResponse((res) => res.request().method() === "DELETE");
    await page.getByRole("button", { name: "Play" }).click();
    expect((await deleteDone).status()).toBe(204);

    await expect(page.getByRole("status")).toHaveText(/Deleted/);
    await expect(page.getByRole("button", { name: "Record again" })).toBeVisible();
    expect((await page.request.get(audio_url)).status(), "recording should be gone").toBe(404);

    assertNoErrors();
  });
});
