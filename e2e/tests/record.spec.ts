import { test, expect, Page, Response } from "@playwright/test";
import { overflow, plainVhRules, watchForErrors } from "./helpers";

const SOCKET = "**/ws/record/";
// pcm-worklet.js sends 80 ms of 16 kHz 16-bit mono PCM per message.
const CHUNK_BYTES = 1280 * 2;

// 204 deleted; 200 {"kept": true} while the keep_audio flag (eval mode) is on in the shared dev database.
// Returns whether the audio was actually deleted.
async function expectDeletedOrKept(page: Page, deleted: Response) {
  expect([200, 204]).toContain(deleted.status());
  if (deleted.status() === 200) expect(await deleted.json()).toEqual({ kept: true });
  await expect(page.getByRole("status")).toHaveText(deleted.status() === 204 ? /Deleted/ : /Kept for evaluation/);
  return deleted.status() === 204;
}

/**
 * Passes the page's socket through to the real server (which saves the recording), but replaces the
 * results it pushes: the e2e server runs without Whisper and the scorer, so on its own they're null.
 */
async function replaceResults(page: Page, results: Record<string, object>) {
  const sent = { chunks: [] as number[], texts: [] as string[] };
  await page.routeWebSocket(SOCKET, (ws) => {
    const server = ws.connectToServer();
    ws.onMessage((message) => {
      if (typeof message === "string") sent.texts.push(message);
      else sent.chunks.push(message.length);
      server.send(message);
    });
    server.onMessage((message) => {
      const data = JSON.parse(message as string);
      ws.send(JSON.stringify({ ...data, ...(results[data.type] ?? {}) }));
    });
    server.onClose((code, reason) => ws.close({ code, reason }));
  });
  return sent;
}

async function recordFor(page: Page, seconds = 1) {
  await page.getByRole("button", { name: "Start session" }).click();
  await expect(page.getByRole("status")).toHaveText(new RegExp(`Listening… 0:0${seconds}`));
  await page.getByRole("button", { name: "Stop" }).click();
}

test.describe("home page recorder", () => {
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
      if (!("AudioWorkletNode" in window)) (window as any).AudioWorkletNode = class {};
    });
  });

  test("loads with styles and fits the screen", async ({ page }) => {
    const assertNoErrors = watchForErrors(page);
    await page.goto("/");

    await expect(page).toHaveTitle("Honest Ear");
    await expect(page.getByRole("heading", { level: 1, name: "Honest Ear" })).toHaveCSS("font-family", /Fraunces/);
    await expect(page.locator('meta[name="viewport"]')).toHaveAttribute("content", /width=device-width/);
    await expect(page.getByRole("button", { name: "Start session" })).toBeVisible();
    const { sideways, downward } = await overflow(page);
    expect(sideways, "page is wider than the screen").toBeLessThanOrEqual(0);
    expect(downward, "page is taller than the screen").toBeLessThanOrEqual(0);
    expect(await plainVhRules(page), "use svh/dvh instead of vh").toEqual([]);

    assertNoErrors();
  });

  test("looks the same as the approved screenshot", async ({ page }) => {
    await page.goto("/");
    await page.evaluate(() => document.fonts.ready);
    await expect(page).toHaveScreenshot("record.png", { fullPage: true, animations: "disabled" });
  });

  test.describe("for a visitor in Colombia", () => {
    test.use({ locale: "es-CO", timezoneId: "America/Bogota" });

    test("shows Spanish", async ({ page }) => {
      const assertNoErrors = watchForErrors(page);
      await page.goto("/");
      await expect(page.locator("html")).toHaveAttribute("lang", "es");
      await expect(page.getByRole("button", { name: "Empezar" })).toBeVisible();

      // Starting runs record.js, whose messages come from the Spanish JavaScript catalog.
      await page.getByRole("button", { name: "Empezar" }).click();
      await expect(page.getByRole("status")).toHaveText(/Escuchando|micrófono/);

      const { sideways, downward } = await overflow(page);
      expect(sideways, "page is wider than the screen").toBeLessThanOrEqual(0);
      expect(downward, "page is taller than the screen").toBeLessThanOrEqual(0);
      assertNoErrors();
    });
  });

  test("streams PCM over the socket and shows each result as the server pushes it", async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "Playwright can only fake a microphone in Chromium");
    const assertNoErrors = watchForErrors(page);
    const alignment = [
      { word: "hola", ops: [["o", "o"], ["l", "l"], ["a", "a"]].map(([e, h]) => ({ op: "match", expected: e, heard: h })) },
      {
        word: "mesa",
        ops: [
          { op: "match", expected: "m", heard: "m" },
          { op: "sub", expected: "e", heard: "i" },
          { op: "match", expected: "s", heard: "s" },
          { op: "del", expected: "a", heard: null },
          { op: "ins", expected: null, heard: "o" },
        ],
      },
    ];
    const sent = await replaceResults(page, {
      phonemes: { phonemes: "o l a m i s o" },
      transcript: { transcript: "Hola mesa" },
      pairs: { alignment },
    });
    await page.goto("/");

    await recordFor(page);

    await expect(page.locator("#transcript")).toHaveText("Hola mesa");
    const words = page.locator(".pair-word");
    await expect(words).toHaveCount(2);
    await expect(words.nth(1).locator(".pair-label")).toHaveText("mesa");
    await expect(words.nth(1).locator(".pair-exp")).toHaveText(["m", "e", "s", "a", "–"]);
    await expect(words.nth(1).locator(".pair-heard")).toHaveText(["m", "i", "s", "–", "o"]);
    await expect(words.nth(1).locator(".pair-op:not(.pair-match)")).toHaveCount(3);
    await expect(page.locator("#phonemes-drift")).toHaveText("3 changes across 7 expected sounds.");
    await expect(page.locator("#phonemes-text"), "the plain line gives way to the pairs").toBeHidden();

    // About a second of audio went up as 80 ms PCM chunks, then Stop.
    expect(sent.chunks.length).toBeGreaterThanOrEqual(10);
    expect(new Set(sent.chunks)).toEqual(new Set([CHUNK_BYTES]));
    expect(sent.texts).toEqual([JSON.stringify({ type: "stop" })]);
    const { sideways } = await overflow(page);
    expect(sideways, "page is wider than the screen").toBeLessThanOrEqual(0);
    assertNoErrors();
  });

  test("records, plays back, then deletes the recording", async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "Playwright can only fake a microphone in Chromium");
    const assertNoErrors = watchForErrors(page);
    await page.goto("/");
    const socket = page.waitForEvent("websocket");
    const saved = new Promise<{ audio_url: string }>((resolve) =>
      socket.then((ws) =>
        ws.on("framereceived", ({ payload }) => {
          const data = JSON.parse(payload as string);
          if (data.type === "recording") resolve(data);
        }),
      ),
    );

    await recordFor(page);
    const { audio_url } = await saved;
    // Without Whisper and the scorer, the results come back empty and their sections stay hidden.
    await expect(page.getByRole("button", { name: "Play" })).toBeVisible();
    await expect(page.locator("#phonemes")).toBeHidden();
    await expect(page.locator("#transcript")).toBeHidden();

    const deleteDone = page.waitForResponse((res) => res.request().method() === "DELETE");
    await page.getByRole("button", { name: "Play" }).click();
    const gone = await expectDeletedOrKept(page, await deleteDone);
    await expect(page.getByRole("button", { name: "Record again" })).toBeVisible();
    expect((await page.request.get(audio_url)).status(), "recording should be gone, unless kept").toBe(gone ? 404 : 200);
    assertNoErrors();
  });

  test("says why when the server refuses the recording", async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "Playwright can only fake a microphone in Chromium");
    const assertNoErrors = watchForErrors(page);
    // The server's code for "the recording feature flag is off".
    await page.routeWebSocket(SOCKET, (ws) => ws.close({ code: 4503 }));
    await page.goto("/");

    await page.getByRole("button", { name: "Start session" }).click();

    await expect(page.getByRole("status")).toHaveText("Recording is turned off right now.");
    await expect(page.getByRole("button", { name: "Record again" })).toBeEnabled();
    assertNoErrors();
  });
});
