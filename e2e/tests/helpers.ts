import { expect, Page } from "@playwright/test";

/** Fails the test on console errors or broken same-origin requests (CSS, JS, images). */
export function watchForErrors(page: Page) {
  const appOrigin = new URL(process.env.BASE_URL ?? "http://localhost:8000").origin;
  const problems: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") problems.push(`console error: ${msg.text()}`);
  });
  page.on("pageerror", (err) => problems.push(`page error: ${err.message}`));
  page.on("response", (res) => {
    const url = new URL(res.url());
    if (url.origin === appOrigin && res.status() >= 400 && url.pathname !== "/favicon.ico") {
      problems.push(`HTTP ${res.status()} for ${url.pathname}`);
    }
  });
  page.on("requestfailed", (req) => problems.push(`request failed: ${req.url()}`));
  return () => expect(problems, problems.join("\n")).toEqual([]);
}

/** How many pixels the page sticks out past the screen (0 or less means it fits). */
export function overflow(page: Page) {
  return page.evaluate(() => ({
    sideways: document.documentElement.scrollWidth - window.innerWidth,
    downward: document.documentElement.scrollHeight - window.innerHeight,
  }));
}

/**
 * Plain `vh` units in our own CSS. On phones 100vh is the height with the browser
 * toolbar hidden, so it's taller than the visible screen and makes pages scroll.
 * Emulated devices don't have a toolbar, so this has to be checked in the CSS itself.
 */
export function plainVhRules(page: Page) {
  return page.evaluate(() => {
    const found: string[] = [];
    const walk = (rules: CSSRuleList, href: string) => {
      for (const rule of Array.from(rules)) {
        if (rule instanceof CSSStyleRule) {
          for (const prop of Array.from(rule.style)) {
            const value = rule.style.getPropertyValue(prop);
            if (/\dvh\b/.test(value)) found.push(`${href}: ${rule.selectorText} { ${prop}: ${value} }`);
          }
        }
        if ("cssRules" in rule) walk((rule as CSSGroupingRule).cssRules, href);
      }
    };
    for (const sheet of Array.from(document.styleSheets)) {
      const href = sheet.href ?? "inline <style>";
      if (href.includes("/static/admin/")) continue;
      try {
        walk(sheet.cssRules, href);
      } catch {
        // Cross-origin stylesheets (Google Fonts) can't be read; they aren't ours anyway.
      }
    }
    return found;
  });
}
