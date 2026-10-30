import { test, expect, Page } from "@playwright/test";
import { overflow, plainVhRules, watchForErrors } from "./helpers";

const TOKEN_PATH = "/api/transcription/token/";

/**
 * Whether the page tries live captions comes from the server (Deepgram key and flags), which differs
 * between a laptop and CI. Each test sets it here so it never depends on .env.
 */
async function setLiveCaptions(page: Page, on: boolean) {
  // Set while the page is parsed, before record.js (deferred) reads it, keeping the real request and headers.
  await page.addInitScript((value) => {
    new MutationObserver((_, observer) => {
      const session = document.querySelector<HTMLElement>(".session");
      if (!session) return;
      session.dataset.liveCaptions = String(value);
      observer.disconnect();
    }).observe(document, { childList: true, subtree: true });
  }, on);
}

type Turn = { turn: number; event: string; text: string };

/**
 * Stands in for Deepgram Flux's live websocket: hands out a fake token and answers audio with scripted
 * TurnInfo messages. Like the real Flux, closing mid-turn sends nothing more: the last words only
 * arrive if the page sends ForceEndTurn first. Nothing reaches Deepgram.
 */
async function fakeFlux(page: Page, script: { afterChunk: number; turn: Turn }[], lastTurn: Turn) {
  const chunkSizes: number[] = [];
  const controls: string[] = [];
  await page.route(`**${TOKEN_PATH}`, (route) =>
    route.fulfill({ json: { url: "wss://api.deepgram.com/v2/listen?fake=1", protocols: ["bearer", "fake-jwt"] } }),
  );
  await page.routeWebSocket("wss://api.deepgram.com/**", (ws) => {
    const send = ({ turn, event, text }: Turn) =>
      ws.send(JSON.stringify({ type: "TurnInfo", event, turn_index: turn, transcript: text, words: [] }));
    ws.send(JSON.stringify({ type: "Connected", sequence_id: 0 }));
    ws.onMessage((message) => {
      if (typeof message === "string") {
        const { type } = JSON.parse(message);
        controls.push(type);
        if (type === "ForceEndTurn") send(lastTurn);
        if (type === "CloseStream") ws.close();
        return;
      }
      chunkSizes.push(message.length);
      script.filter((step) => step.afterChunk === chunkSizes.length).forEach((step) => send(step.turn));
    });
  });
  return { chunkSizes, controls };
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
      if (!("MediaRecorder" in window)) {
        (window as any).MediaRecorder = class {
          static isTypeSupported() {
            return false;
          }
        };
      }
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
      await setLiveCaptions(page, false);
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

  test("shows live captions while speaking, then the final transcript", async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "Playwright can only fake a microphone in Chromium");
    const assertNoErrors = watchForErrors(page);
    await setLiveCaptions(page, true);
    const { chunkSizes, controls } = await fakeFlux(
      page,
      [
        // A chunk is 80 ms of audio, so updates arrive about a second apart, as when speaking.
        { afterChunk: 2, turn: { turn: 0, event: "StartOfTurn", text: "" } },
        { afterChunk: 4, turn: { turn: 0, event: "Update", text: "Hola" } },
        { afterChunk: 16, turn: { turn: 0, event: "EndOfTurn", text: "Hola, ¿cómo estás?" } },
        { afterChunk: 18, turn: { turn: 1, event: "StartOfTurn", text: "" } },
        { afterChunk: 28, turn: { turn: 1, event: "Update", text: "and a little" } },
      ],
      { turn: 1, event: "EndOfTurn", text: "and a little English." },
    );
    await page.goto("/");

    await page.getByRole("button", { name: "Start session" }).click();
    const captions = page.locator("#captions");
    await expect(captions).toHaveText("Hola");
    await expect(captions).toHaveText("Hola, ¿cómo estás? and a little");
    await expect(page.locator(".session")).toHaveAttribute("data-phase", "listening");
    await expect(page.locator(".glow")).toHaveCSS("animation-name", "glow");

    const uploadDone = page.waitForRequest((req) => req.url().endsWith("/api/recordings/") && req.method() === "POST");
    await page.getByRole("button", { name: "Stop" }).click();
    const upload = await uploadDone;
    expect(upload.postDataBuffer()?.toString("latin1"), "server is told not to transcribe again").toContain(
      'name="live_transcript"',
    );

    await expect(page.locator("#transcript")).toHaveText("Hola, ¿cómo estás? and a little English.");
    await expect(captions).toBeHidden();
    // 80 ms of 16 kHz, 16-bit mono PCM per message: what the server told Flux to expect.
    expect(chunkSizes.length).toBeGreaterThanOrEqual(28);
    expect(new Set(chunkSizes)).toEqual(new Set([2560]));
    expect(controls, "the turn in progress is ended before closing").toEqual(["ForceEndTurn", "CloseStream"]);

    const deleteDone = page.waitForResponse((res) => res.request().method() === "DELETE");
    await page.getByRole("button", { name: "Play" }).click();
    expect((await deleteDone).status()).toBe(204);
    await expect(page.getByRole("status")).toHaveText(/Deleted/);
    await expect(page.getByRole("button", { name: "Record again" })).toBeVisible();
    assertNoErrors();
  });

  test("falls back to transcribing after Stop when live captions can't start", async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "Playwright can only fake a microphone in Chromium");
    const assertNoErrors = watchForErrors(page, { expected: [TOKEN_PATH] });
    const words = "Esto es una prueba de transcripción. ".repeat(12).trim();
    await setLiveCaptions(page, true);
    await page.route(`**${TOKEN_PATH}`, (route) => route.fulfill({ status: 503, json: { error: "unavailable" } }));
    // Server-side Deepgram can't be faked from here, so stand in its result on the real upload response.
    await page.route("**/api/recordings/", async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: { ...(await response.json()), transcript: words } });
    });
    await page.goto("/");

    await page.getByRole("button", { name: "Start session" }).click();
    await expect(page.getByRole("status")).toHaveText(/Listening… 0:01/);
    const uploadDone = page.waitForRequest((req) => req.url().endsWith("/api/recordings/") && req.method() === "POST");
    await page.getByRole("button", { name: "Stop" }).click();
    expect((await uploadDone).postDataBuffer()?.toString("latin1")).not.toContain('name="live_transcript"');

    await expect(page.locator("#transcript")).toHaveText(words);
    const { sideways } = await overflow(page);
    expect(sideways, "page is wider than the screen").toBeLessThanOrEqual(0);

    const deleteDone = page.waitForResponse((res) => res.request().method() === "DELETE");
    await page.getByRole("button", { name: "Play" }).click();
    await deleteDone;
    await expect(page.locator("#transcript"), "transcript stays after playback").toHaveText(words);
    assertNoErrors();
  });

  test("records, plays back, then deletes the recording", async ({ page, browserName }) => {
    test.skip(browserName !== "chromium", "Playwright can only fake a microphone in Chromium");
    const assertNoErrors = watchForErrors(page);
    await setLiveCaptions(page, false);
    await page.goto("/");

    await page.getByRole("button", { name: "Start session" }).click();
    await expect(page.getByRole("status")).toHaveText(/Listening… 0:01/);

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
