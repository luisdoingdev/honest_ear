import { test, expect } from "@playwright/test";

test("database and redis are healthy", async ({ request }) => {
  const res = await request.get("/health/");
  expect(res.status()).toBe(200);
  expect(await res.json()).toEqual({ database: "ok", redis: "ok" });
});
