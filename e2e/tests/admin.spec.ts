import { test, expect } from "@playwright/test";
import { overflow, watchForErrors } from "./helpers";

test.describe("admin", () => {
  test("login page loads with styles and fits sideways", async ({ page }) => {
    const assertNoErrors = watchForErrors(page);
    await page.goto("/admin/login/");

    await expect(page.getByRole("button", { name: "Log in" })).toBeVisible();
    expect((await overflow(page)).sideways, "page is wider than the screen").toBeLessThanOrEqual(0);

    assertNoErrors();
  });

  test("superuser can log in", async ({ page }) => {
    const username = process.env.DJANGO_SUPERUSER_USERNAME;
    const password = process.env.DJANGO_SUPERUSER_PASSWORD;
    test.skip(!username || !password, "DJANGO_SUPERUSER_* not set");

    await page.goto("/admin/login/");
    await page.getByLabel("Username").fill(username!);
    await page.getByLabel("Password").fill(password!);
    await page.getByRole("button", { name: "Log in" }).click();

    await expect(page.getByRole("heading", { name: "Site administration" })).toBeVisible();
  });
});
