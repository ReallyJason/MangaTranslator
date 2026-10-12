/* Fukidashi content script: finds manga images near the viewport, sends them
 * for translation in document order and swaps in the translated result. */
(function () {
  "use strict";
  if (window.__fukidashiLoaded) return;
  window.__fukidashiLoaded = true;

  const F = globalThis.Fukidashi;
  const Pill = globalThis.FukidashiPill;
  const ROOT_MARGIN = "1500px 0px 1500px 0px";
  const MAX_RESETS = 3; // stop fighting sites that keep swapping our image back

  let settings = Object.assign({}, F.DEFAULTS);
  let enabled = false;
  let showingOriginals = false;
  let dead = false; // extension was reloaded/updated: this script can no longer talk to it

  /** img -> {status, src, translated, orig, resets, styledWidth, refresh, force} */
  const records = new Map();
  const queue = new Set();
  const near = new Set(); // images within ROOT_MARGIN of the viewport
  const observed = new WeakSet();
  let active = 0;

  // Counters for the current burst (shown in the pill) and for the whole page (popup).
  let burst = newBurst();
  const pageStats = { done: 0, failed: 0, notext: 0 };
  const notesShown = new Set();

  function newBurst() {
    return { total: 0, done: 0, failed: 0, notext: 0, errors: [], notes: [] };
  }

  // ---------------------------------------------------------------- eligibility

  function sourceUrl(img) {
    return img.currentSrc || img.src || "";
  }

  function isSupportedUrl(url) {
    if (!url) return false;
    if (/^data:image\/svg/i.test(url) || /\.svg(\?|#|$)/i.test(url)) return false;
    return /^(https?:|blob:|data:image\/)/i.test(url);
  }

  /** "wait" (not loaded yet), "no", or "yes". */
  function eligibility(img, force) {
    const url = sourceUrl(img);
    if (!isSupportedUrl(url)) return "no";
    if (!img.complete || !img.naturalWidth) return "wait";
    if (force) return "yes";
    const min = Number(settings.minNatural) || 0;
    if (img.naturalWidth < min || img.naturalHeight < min) return "no";
    const r = img.getBoundingClientRect();
    const minR = Number(settings.minRendered) || 0;
    if (r.width < minR || r.height < minR) return "no";
    return "yes";
  }

  // ---------------------------------------------------------------- observers

  const io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          near.add(e.target);
          consider(e.target);
        } else {
          near.delete(e.target);
        }
      }
    },
    { rootMargin: ROOT_MARGIN }
  );

  function observe(img) {
    if (observed.has(img)) return;
    observed.add(img);
    io.observe(img);
    img.addEventListener("load", () => consider(img));
  }

  function observeAll(rootNode) {
    if (rootNode.tagName === "IMG") observe(rootNode);
    else if (rootNode.querySelectorAll) rootNode.querySelectorAll("img").forEach(observe);
  }

  const mo = new MutationObserver(handleMutations);

  function handleMutations(list) {
    for (const r of list) {
      if (r.type === "childList") {
        r.addedNodes.forEach((n) => {
          if (n.nodeType !== 1) return;
          if (n.tagName === "SOURCE" && n.parentElement && n.parentElement.tagName === "PICTURE") {
            const img = n.parentElement.querySelector("img");
            if (img) sourceChanged(img);
          } else {
            observeAll(n);
          }
        });
      } else if (r.type === "attributes") {
        const t = r.target;
        if (t.tagName === "IMG") sourceChanged(t);
        else if (t.tagName === "SOURCE" && t.parentElement && t.parentElement.tagName === "PICTURE") {
          const img = t.parentElement.querySelector("img");
          if (img) sourceChanged(img);
        }
      }
    }
  }

  /** Run our own DOM changes without the MutationObserver seeing them. */
  function quietly(fn) {
    handleMutations(mo.takeRecords()); // don't lose the page's own pending changes
    try {
      return fn();
    } finally {
      mo.takeRecords();
    }
  }

  /** The page changed an image's src/srcset (lazy loading, reader swapping pages...). */
  function sourceChanged(img) {
    observe(img);
    const rec = records.get(img);
    if (!rec) {
      consider(img);
      return;
    }
    if (rec.status === "queued" || rec.status === "working") return; // process() re-checks the URL
    const resets = (rec.resets || 0) + 1;
    forget(img, rec);
    if (resets > MAX_RESETS) {
      records.set(img, { status: "contested", resets });
      note("This site keeps replacing its images, so some can't be translated.");
      return;
    }
    records.set(img, { status: "reset", resets });
    consider(img);
  }

  function forget(img, rec) {
    if (rec.translated && rec.translated.startsWith("blob:")) URL.revokeObjectURL(rec.translated);
    if (rec.styledWidth) img.style.removeProperty("width");
    delete img.dataset.fukidashi;
    records.delete(img);
    queue.delete(img);
  }

  // ---------------------------------------------------------------- queue

  function consider(img, opts) {
    const o = opts || {};
    if (dead) return;
    const rec = records.get(img);
    if (rec && rec.status !== "reset") return;
    if (!o.force && !(enabled && near.has(img))) return;
    const e = eligibility(img, o.force);
    if (e !== "yes") {
      if (e === "wait" && o.force) {
        img.addEventListener("load", () => consider(img, o), { once: true });
      }
      return;
    }
    enqueue(img, o);
  }

  function enqueue(img, o) {
    const prev = records.get(img);
    if (!active && !queue.size) burst = newBurst();
    records.set(img, { status: "queued", resets: prev ? prev.resets || 0 : 0, refresh: !!o.refresh, force: !!o.force });
    queue.add(img);
    burst.total++;
    updatePill();
    pump();
  }

  function docOrder(a, b) {
    if (a === b) return 0;
    return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
  }

  function pump() {
    const limit = settings.mode === "context" ? 1 : 2;
    while (!dead && active < limit && queue.size) {
      const next = Array.from(queue).sort(docOrder)[0];
      queue.delete(next);
      if (!next.isConnected) {
        records.delete(next);
        burst.total--;
        continue;
      }
      active++;
      process(next).finally(() => {
        active--;
        updatePill();
        pump();
      });
    }
    if (!active && !queue.size) prune();
  }

  async function process(img) {
    const rec = records.get(img);
    rec.status = "working";
    updatePill();
    const url = sourceUrl(img);
    rec.src = url;
    let resp;
    try {
      const msg = { type: "translate", pageUrl: location.href, refresh: rec.refresh };
      if (url.startsWith("blob:") || url.startsWith("data:")) msg.dataUrl = await readLocalImage(img, url);
      else msg.url = url;
      resp = await chrome.runtime.sendMessage(msg);
      if (!resp) throw new Error("No response from the extension");
    } catch (e) {
      resp = { ok: false, error: e && e.message ? e.message : String(e) };
      if (/context invalidated/i.test(resp.error)) {
        dead = true;
        Pill.show("Fukidashi was updated. Reload the page to continue.", { kind: "warn", timeout: 0 });
        return;
      }
    }

    if (records.get(img) !== rec) return; // reset while we were working
    if (sourceUrl(img) !== url) {
      // The page swapped the image meanwhile; start over with the new one.
      records.set(img, { status: "reset", resets: rec.resets });
      burst.total--;
      consider(img, { force: rec.force });
      return;
    }

    if (resp.headers && resp.headers.note) note(resp.headers.note);
    if (!resp.ok) {
      rec.status = "failed";
      rec.error = resp.error;
      burst.failed++;
      pageStats.failed++;
      burst.errors.push(resp.error);
      return;
    }
    if (resp.noText) {
      rec.status = "notext";
      burst.notext++;
      pageStats.notext++;
      return;
    }
    try {
      await applyResult(img, rec, resp.dataUrl);
      rec.status = "done";
      burst.done++;
      pageStats.done++;
    } catch (e) {
      rec.status = "failed";
      rec.error = e.message;
      burst.failed++;
      pageStats.failed++;
      burst.errors.push(e.message);
    }
  }

  /** blob:/data: images can't be fetched by the background worker; read them here. */
  async function readLocalImage(img, url) {
    if (url.startsWith("data:")) return url;
    try {
      const blob = await (await fetch(url)).blob();
      return await new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onload = () => resolve(fr.result);
        fr.onerror = () => reject(fr.error);
        fr.readAsDataURL(blob);
      });
    } catch (e) {
      // Fall back to drawing the (same-origin) image onto a canvas.
      const c = document.createElement("canvas");
      c.width = img.naturalWidth;
      c.height = img.naturalHeight;
      c.getContext("2d").drawImage(img, 0, 0);
      return c.toDataURL("image/png");
    }
  }

  // ---------------------------------------------------------------- swapping

  function dataUrlToBlob(dataUrl) {
    const comma = dataUrl.indexOf(",");
    const mime = (dataUrl.slice(5, comma).split(";")[0] || "image/png").trim();
    const bin = atob(dataUrl.slice(comma + 1));
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: mime });
  }

  function saveOriginal(img) {
    const picture = img.parentElement && img.parentElement.tagName === "PICTURE" ? img.parentElement : null;
    return {
      src: img.getAttribute("src"),
      srcset: img.getAttribute("srcset"),
      sizes: img.getAttribute("sizes"),
      sources: picture
        ? Array.from(picture.querySelectorAll("source")).map((el) => ({
            el,
            srcset: el.getAttribute("srcset"),
            sizes: el.getAttribute("sizes"),
          }))
        : [],
    };
  }

  function setAttr(el, name, value) {
    if (value === null || value === undefined) el.removeAttribute(name);
    else el.setAttribute(name, value);
  }

  /** Point the <img> (and its <picture> sources) at `url`; resolves true if it loads. */
  function showUrl(img, rec, url) {
    const width = img.getBoundingClientRect().width;
    return new Promise((resolve) => {
      let settled = false;
      const done = (ok) => {
        if (settled) return;
        settled = true;
        img.removeEventListener("load", onLoad);
        img.removeEventListener("error", onError);
        clearTimeout(timer);
        resolve(ok);
      };
      const onLoad = () => {
        // Dropping a density srcset (e.g. "2x") changes the intrinsic size; keep the layout.
        const now = img.getBoundingClientRect().width;
        if (width > 0 && Math.abs(now - width) > 2 && !img.style.width) {
          quietly(() => img.style.setProperty("width", width + "px"));
          rec.styledWidth = true;
        }
        done(true);
      };
      const onError = () => done(false);
      const timer = setTimeout(() => done(img.complete && img.naturalWidth > 0), 15000);
      img.addEventListener("load", onLoad);
      img.addEventListener("error", onError);
      quietly(() => {
        rec.orig.sources.forEach((s) => {
          s.el.removeAttribute("srcset");
          s.el.removeAttribute("sizes");
        });
        img.removeAttribute("srcset");
        img.removeAttribute("sizes");
        img.setAttribute("src", url);
        img.dataset.fukidashi = "translated";
      });
    });
  }

  function restoreOriginal(img, rec) {
    quietly(() => {
      if (rec.styledWidth) {
        img.style.removeProperty("width");
        rec.styledWidth = false;
      }
      rec.orig.sources.forEach((s) => {
        setAttr(s.el, "srcset", s.srcset);
        setAttr(s.el, "sizes", s.sizes);
      });
      setAttr(img, "srcset", rec.orig.srcset);
      setAttr(img, "sizes", rec.orig.sizes);
      setAttr(img, "src", rec.orig.src);
      img.dataset.fukidashi = "original";
    });
  }

  async function applyResult(img, rec, dataUrl) {
    rec.orig = rec.orig || saveOriginal(img);
    const blobUrl = URL.createObjectURL(dataUrlToBlob(dataUrl));
    if (await showUrl(img, rec, blobUrl)) {
      rec.translated = blobUrl;
    } else {
      // The page's Content-Security-Policy may forbid blob: images; try data:.
      URL.revokeObjectURL(blobUrl);
      if (await showUrl(img, rec, dataUrl)) {
        rec.translated = dataUrl;
      } else {
        restoreOriginal(img, rec);
        throw new Error("This page blocks replaced images (Content-Security-Policy)");
      }
    }
    if (showingOriginals) restoreOriginal(img, rec);
  }

  function setShowOriginals(value) {
    showingOriginals = !!value;
    for (const [img, rec] of records) {
      if (rec.status !== "done") continue;
      if (showingOriginals) restoreOriginal(img, rec);
      else showUrl(img, rec, rec.translated);
    }
  }

  function prune() {
    if (records.size < 300) return;
    for (const [img, rec] of records) {
      if (!img.isConnected) forget(img, rec);
    }
  }

  // ---------------------------------------------------------------- status pill

  function note(text) {
    if (notesShown.has(text)) return;
    notesShown.add(text);
    burst.notes.push(text);
  }

  function updatePill() {
    if (dead) return;
    const finished = burst.done + burst.failed + burst.notext;
    if (active || queue.size) {
      Pill.show(`Translating ${Math.min(finished + 1, burst.total)}/${burst.total}`, { kind: "busy" });
      return;
    }
    if (!burst.total) {
      if (Pill.lastText().startsWith("Translating")) Pill.hide();
      return;
    }
    const parts = [];
    if (burst.done) parts.push(`Translated ${burst.done} image${burst.done === 1 ? "" : "s"}`);
    else if (!burst.failed) parts.push(burst.notext ? "No text found" : "Nothing to translate");
    if (burst.failed) parts.push(burst.done ? `${burst.failed} failed` : `${burst.failed} image${burst.failed === 1 ? "" : "s"} failed`);
    let text = parts.join(" · ");
    let kind = burst.failed ? (burst.done ? "warn" : "error") : "ok";
    let timeout = 4000;
    if (burst.failed) {
      text += `: ${burst.errors[burst.errors.length - 1]}`;
      timeout = 10000;
    }
    if (burst.notes.length) {
      text += ` · ${burst.notes.join(" · ")}`;
      if (kind === "ok") kind = "warn";
      timeout = Math.max(timeout, 9000);
    }
    Pill.show(text, { kind, timeout });
    burst = newBurst();
  }

  // ---------------------------------------------------------------- commands

  function allImages() {
    return Array.from(document.querySelectorAll("img"));
  }

  function translateAll() {
    let n = 0;
    for (const img of allImages()) {
      observe(img);
      const rec = records.get(img);
      if (rec && rec.status !== "reset" && rec.status !== "failed") continue;
      if (rec) records.delete(img);
      if (eligibility(img, false) === "yes") {
        enqueue(img, { force: true });
        n++;
      }
    }
    if (!n && !active && !queue.size) Pill.show("No untranslated manga images found on this page", { kind: "warn", timeout: 3500 });
    return n;
  }

  function retranslate() {
    const redo = [];
    for (const [img, rec] of Array.from(records)) {
      if (rec.status === "queued" || rec.status === "working") continue;
      if (rec.status === "done") restoreOriginal(img, rec);
      if (["done", "failed", "notext"].includes(rec.status)) redo.push(img);
      forget(img, rec);
    }
    showingOriginals = false;
    redo.sort(docOrder).forEach((img) => enqueue(img, { force: true, refresh: true }));
    // Also pick up anything near the viewport that was never processed.
    if (enabled) near.forEach((img) => consider(img));
    return redo.length;
  }

  function translateImage(srcUrl) {
    const img = allImages().find((i) => {
      const rec = records.get(i);
      if (sourceUrl(i) === srcUrl || i.src === srcUrl) return true;
      // Also match the URL it had before we swapped it (e.g. right-click while showing originals).
      return !!rec && (rec.translated === srcUrl || rec.src === srcUrl || (rec.orig && rec.orig.src === srcUrl));
    });
    if (!img) {
      Pill.show("Couldn't find that image on the page", { kind: "warn", timeout: 3500 });
      return false;
    }
    observe(img);
    const rec = records.get(img);
    if (rec && (rec.status === "queued" || rec.status === "working")) return true;
    if (rec) {
      if (rec.status === "done") restoreOriginal(img, rec);
      forget(img, rec);
    }
    if (img.complete && img.naturalWidth) enqueue(img, { force: true, refresh: !!rec });
    else consider(img, { force: true, refresh: !!rec });
    return true;
  }

  function state() {
    let queued = 0;
    let working = 0;
    for (const rec of records.values()) {
      if (rec.status === "queued") queued++;
      if (rec.status === "working") working++;
    }
    return {
      enabled,
      autoAll: settings.autoAll,
      host: location.hostname,
      showingOriginals,
      stats: Object.assign({ queued, working }, pageStats),
    };
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || dead) return false;
    switch (msg.type) {
      case "getState":
        sendResponse(state());
        break;
      case "translateAll":
        sendResponse({ count: translateAll() });
        break;
      case "retranslate":
        sendResponse({ count: retranslate() });
        break;
      case "showOriginals":
        setShowOriginals(msg.value);
        sendResponse(state());
        break;
      case "translateImage":
        sendResponse({ found: translateImage(msg.srcUrl) });
        break;
      case "toast":
        Pill.show(msg.text, { kind: "ok", timeout: 2500 });
        sendResponse({ ok: true });
        break;
      default:
        return false;
    }
    return false;
  });

  // ---------------------------------------------------------------- startup

  function applySettings(s) {
    settings = s;
    const was = enabled;
    enabled = F.isAutoOn(settings, location.href);
    if (enabled && !was) near.forEach((img) => consider(img));
    if (!enabled && was) {
      // Stop pending work; keep what is already translated.
      for (const img of queue) records.delete(img);
      burst.total -= queue.size;
      queue.clear();
      updatePill();
    }
  }

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || dead) return;
    F.getSettings()
      .then(applySettings)
      .catch(() => {});
  });

  F.getSettings().then((s) => {
    applySettings(s);
    observeAll(document);
    mo.observe(document.documentElement, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["src", "srcset"],
    });
  });
})();
