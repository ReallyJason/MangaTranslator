/* End-to-end test: loads the unpacked extension in headless Chromium and drives
 * the local test site. Needs, already running:
 *   - the Fukidashi server on 127.0.0.1:5003 (any translation backend)
 *   - python tools/test_page/serve.py
 * Run:  node tools/test_page/run_e2e.js      (requires `npm i playwright` or a global install)
 * Screenshots go to tools/test_page/output/. */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { chromium } = require("playwright");

const EXT = path.resolve(__dirname, "../../extension");
const OUT = path.join(__dirname, "output");
const READER = "http://localhost:8080";
const results = [];

function check(name, ok, detail) {
  results.push({ name, ok: !!ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
}

async function waitFor(page, fn, arg, timeout = 120000) {
  try {
    await page.waitForFunction(fn, arg, { timeout, polling: 250 });
    return true;
  } catch (e) {
    return false;
  }
}

const imgInfo = (page, id) =>
  page.evaluate((id) => {
    const img = document.getElementById(id);
    if (!img) return null;
    const r = img.getBoundingClientRect();
    return {
      src: img.getAttribute("src") || "",
      srcset: img.getAttribute("srcset"),
      width: Math.round(r.width),
      natural: img.naturalWidth,
    };
  }, id);

const pillText = (page) =>
  page.evaluate(() => {
    const host = document.querySelector("fukidashi-status");
    const pill = host && host.shadowRoot.querySelector(".pill");
    return pill && pill.classList.contains("show") ? pill.textContent : "";
  });

// Translated = marked by the content script and showing a blob:/data: result that has loaded.
const isBlob = (id) => {
  const img = document.getElementById(id);
  return (
    !!img &&
    img.dataset.fukidashi === "translated" &&
    /^(blob|data):/.test(img.getAttribute("src") || "") &&
    img.complete &&
    img.naturalWidth > 0
  );
};

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), "fukidashi-e2e-"));
  const ctx = await chromium.launchPersistentContext(userDir, {
    channel: "chromium",
    headless: true,
    viewport: { width: 1000, height: 900 },
    args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
    executablePath: process.env.CHROMIUM_PATH || undefined,
  });
  try {
    let [sw] = ctx.serviceWorkers();
    if (!sw) sw = await ctx.waitForEvent("serviceworker", { timeout: 15000 });
    const extId = sw.url().split("/")[2];
    check("extension service worker started", !!extId, extId);

    await sw.evaluate(() => chrome.storage.local.set({ autoSites: ["localhost"], mode: "context" }));

    // --- auto-translate as the page loads ------------------------------------
    const page = await ctx.newPage();
    // Record the order images get translated, from inside the page (no polling races).
    await page.addInitScript(() => {
      window.__order = [];
      new MutationObserver((list) => {
        for (const r of list) {
          const el = r.target;
          if (el.dataset.fukidashi === "translated" && el.id && !window.__order.includes(el.id)) window.__order.push(el.id);
        }
      }).observe(document, { subtree: true, attributes: true, attributeFilter: ["data-fukidashi"] });
    });
    await page.goto(READER + "/");

    check("pill shows progress", await waitFor(page, () => {
      const h = document.querySelector("fukidashi-status");
      return h && /Translating \d+\/\d+/.test(h.shadowRoot.textContent);
    }, null, 20000), await pillText(page));

    check("hotlink-protected image translated (Referer rule)", await waitFor(page, isBlob, "p1"));
    check("unprotected image translated", await waitFor(page, isBlob, "p2"));
    check("<picture>/srcset image translated", await waitFor(page, isBlob, "p3"));
    const p3 = await imgInfo(page, "p3");
    const p3src = await page.evaluate(() => document.getElementById("p3-source").getAttribute("srcset"));
    check("srcset stripped from <img> and <source>", p3.srcset === null && p3src === null);
    check("2x-density image keeps its on-screen width", Math.abs(p3.width - 450) <= 2, `width ${p3.width}px`);
    const icon = await imgInfo(page, "icon");
    check("tiny logo skipped", icon.src.includes("icon.jpg"));
    const order = await page.evaluate(() => window.__order);
    check("initial images processed in document order", order.slice(0, 3).join(",") === "p1,p2,p3", order.join(","));

    // Scroll down step by step like a reader.
    for (let y = 0; ; y += 600) {
      const bottom = await page.evaluate((y) => {
        window.scrollTo(0, y);
        return y >= document.documentElement.scrollHeight - window.innerHeight;
      }, y);
      await page.waitForTimeout(400);
      if (bottom) break;
    }
    check("blob: image translated", await waitFor(page, isBlob, "p4"));
    check("lazy-loaded image translated after src swap", await waitFor(page, isBlob, "p5"));
    check("infinite-scroll image translated", await waitFor(page, isBlob, "p8"));
    check("webtoon strip translated", await waitFor(page, isBlob, "strip", 180000));
    // p6 is swapped by the page to page7.jpg at 6 s; it must be re-queued and translated again.
    const p6ok = await waitFor(page, isBlob, "p6", 180000);
    check("page-swapped image re-translated", p6ok);
    check("pill summary after idle", await waitFor(page, () => {
      const h = document.querySelector("fukidashi-status");
      return h && /Translated \d+ image/.test(h.shadowRoot.textContent);
    }, null, 60000), await pillText(page));

    const translated = await page.evaluate(() => Array.from(document.images).filter((i) => i.dataset.fukidashi === "translated").length);
    check("all 8 manga images translated", translated === 8, `${translated} translated images`);

    const refs = await (await fetch("http://127.0.0.1:8081/referers")).text();
    check("CDN saw the page URL as Referer", refs.includes("/img/page1.jpg http://localhost:8080/"));

    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(OUT, "page_translated.png") });

    // --- popup -----------------------------------------------------------------
    const tabId = await sw.evaluate(async () => (await chrome.tabs.query({ url: "http://localhost:8080/*" }))[0].id);
    const popup = await ctx.newPage();
    await popup.setViewportSize({ width: 340, height: 640 });
    await popup.goto(`chrome-extension://${extId}/popup.html?tab=${tabId}`);
    check("popup shows server online", await waitFor(popup, () => document.getElementById("server-label").textContent === "Online", null, 10000));
    check("popup reflects auto-translate for this site", await popup.evaluate(() => document.getElementById("auto-site").checked));
    check("popup shows page stats", await waitFor(popup, () => /\d+ translated/.test(document.getElementById("page-status").textContent), null, 5000),
      await popup.evaluate(() => document.getElementById("page-status").textContent));
    await popup.screenshot({ path: path.join(OUT, "popup.png"), fullPage: true });

    await popup.click("label:has(#show-originals)");
    check("show originals restores original URLs", await waitFor(page, () => document.getElementById("p1").getAttribute("src").includes("page1.jpg"), null, 5000));
    const p3orig = await imgInfo(page, "p3");
    check("show originals restores srcset", p3orig.srcset !== null);
    await popup.click("label:has(#show-originals)");
    check("toggling back shows translations", await waitFor(page, isBlob, "p1", 5000));

    await popup.click("#mode-fast");
    check("mode switch saved", (await sw.evaluate(() => chrome.storage.local.get("mode"))).mode === "fast");
    await popup.click("#mode-context");

    await popup.click("#reset-memory");
    check("reset story memory", await waitFor(popup, () => /story memory/i.test(document.getElementById("series-key").textContent), null, 5000),
      await popup.evaluate(() => document.getElementById("series-key").textContent));

    // --- context menu path ("Translate this image") ---------------------------
    const before = (await imgInfo(page, "p2")).src;
    // A real right-click on a translated image reports its current (blob:) URL.
    await sw.evaluate(([tabId, srcUrl]) => chrome.tabs.sendMessage(tabId, { type: "translateImage", srcUrl }, { frameId: 0 }), [tabId, before]);
    check("context-menu translate re-translates an image", await waitFor(page, (b) => {
      const s = document.getElementById("p2").getAttribute("src");
      return s.startsWith("blob:") && s !== b && document.getElementById("p2").complete;
    }, before, 120000));

    const p5before = (await imgInfo(page, "p5")).src;
    await sw.evaluate((tabId) => chrome.tabs.sendMessage(tabId, { type: "translateImage", srcUrl: "http://127.0.0.1:8081/img/page5.jpg" }, { frameId: 0 }), tabId);
    check("context-menu translate also matches the original URL", await waitFor(page, (b) => {
      const s = document.getElementById("p5").getAttribute("src");
      return s !== b && document.getElementById("p5").dataset.fukidashi === "translated" && document.getElementById("p5").complete;
    }, p5before, 120000));

    // --- keyboard shortcut handler (toggleSite) --------------------------------
    const off = await sw.evaluate(() => toggleSite("localhost"));
    const on = await sw.evaluate(() => toggleSite("localhost"));
    check("Alt+Shift+T handler toggles the site", off === false && on === true);

    // --- re-translate page -----------------------------------------------------
    const oldP1 = (await imgInfo(page, "p1")).src;
    await popup.click("#retranslate");
    check("re-translate page replaces results", await waitFor(page, (o) => {
      const s = document.getElementById("p1").getAttribute("src");
      return s.startsWith("blob:") && s !== o && document.getElementById("p1").complete;
    }, oldP1, 180000));
    check("re-translate finishes for the whole page", await waitFor(page, () => {
      const h = document.querySelector("fukidashi-status");
      return h && /Translated \d+ image/.test(h.shadowRoot.textContent) && !/failed/.test(h.shadowRoot.textContent);
    }, null, 600000), await pillText(page));
    await popup.close();

    // --- server offline ----------------------------------------------------------
    await sw.evaluate(() => chrome.storage.local.set({ serverUrl: "http://127.0.0.1:5099" }));
    await sw.evaluate((tabId) => chrome.tabs.sendMessage(tabId, { type: "translateImage", srcUrl: "http://127.0.0.1:8081/img/page1.jpg" }, { frameId: 0 }), tabId);
    check("offline server error shown in pill", await waitFor(page, () => {
      const h = document.querySelector("fukidashi-status");
      return h && /not running/.test(h.shadowRoot.textContent);
    }, null, 20000), await pillText(page));
    await page.screenshot({ path: path.join(OUT, "page_offline_pill.png") });
    await sw.evaluate(() => chrome.storage.local.set({ serverUrl: "http://127.0.0.1:5003" }));

    // --- CSP that blocks blob: images ---------------------------------------------
    const csp = await ctx.newPage();
    await csp.goto(READER + "/csp.html");
    // Chrome lets the extension's blob: image through this CSP; if a browser blocks it,
    // the content script falls back to a data: URL. Either way the translation must show.
    check("translated image shown on a page whose CSP excludes blob:", await waitFor(csp, isBlob, "c1", 120000),
      await csp.evaluate(() => document.getElementById("c1").getAttribute("src").slice(0, 10)));
    await csp.screenshot({ path: path.join(OUT, "csp_page.png") });
  } finally {
    await ctx.close();
  }
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(2);
});
