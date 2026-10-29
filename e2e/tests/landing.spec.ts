import { test, expect } from "@playwright/test";
import { overflow, plainVhRules, watchForErrors } from "./helpers";

test.describe("landing page", () => {
  test("shows the name with styles applied", async ({ page }) => {
    const assertNoErrors = watchForErrors(page);
    await page.goto("/");

    await expect(page).toHaveTitle("Honest Ear");
    const heading = page.getByRole("heading", { level: 1, name: "Honest Ear" });
    await expect(heading).toBeVisible();
    await expect(heading).toHaveCSS("font-family", /Fraunces/);
    await expect(heading).toBeInViewport({ ratio: 1 });

    assertNoErrors();
  });

  test("fits the screen without scrolling", async ({ page }) => {
    await page.goto("/");
    const { sideways, downward } = await overflow(page);
    expect(sideways, "page is wider than the screen").toBeLessThanOrEqual(0);
    expect(downward, "page is taller than the screen").toBeLessThanOrEqual(0);
  });

  test("CSS uses mobile-safe viewport units", async ({ page }) => {
    await page.goto("/");
    expect(
      await plainVhRules(page),
      "use svh/dvh instead of vh (100vh overflows on phones with a browser toolbar)",
    ).toEqual([]);
  });

  test("has a mobile viewport meta tag", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator('meta[name="viewport"]')).toHaveAttribute("content", /width=device-width/);
  });

  test("looks the same as the approved screenshot", async ({ page }) => {
    await page.goto("/");
    await page.evaluate(() => document.fonts.ready);
    await expect(page).toHaveScreenshot("landing.png", { fullPage: true });
  });
});
