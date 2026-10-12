/* Fukidashi background service worker: downloads images (with the page as
 * Referer, so hotlink-protected CDNs work), sends them to the local server and
 * hands the result back to the content script as a data URL. */
importScripts("defaults.js");

const F = self.Fukidashi;
const MENU_ID = "fukidashi-translate-image";
const SERVER_TIMEOUT_MS = 5 * 60 * 1000;
const HEADER_NAMES = ["mode", "engine", "note", "blocks", "cache", "time", "error"];

// Session rules survive service-worker restarts, so clear leftovers once on start.
let nextRuleId = 1;
const rulesReady = chrome.declarativeNetRequest
  .getSessionRules()
  .then((rules) =>
    rules.length ? chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: rules.map((r) => r.id) }) : null
  )
  .catch((e) => console.warn("Fukidashi: could not clear session rules", e));

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: MENU_ID, title: "Translate this image", contexts: ["image"] });
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== MENU_ID || !tab || tab.id === undefined) return;
  chrome.tabs.sendMessage(tab.id, { type: "translateImage", srcUrl: info.srcUrl }, { frameId: 0 }).catch(() => {
    console.warn("Fukidashi: content script not available in this tab (reload the page)");
  });
});

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== "toggle-auto") return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.url) return;
  const host = F.hostOf(tab.url);
  if (!host) return;
  const on = await toggleSite(host);
  chrome.tabs
    .sendMessage(tab.id, { type: "toast", text: `Auto-translate ${on ? "on" : "off"} for ${host}` }, { frameId: 0 })
    .catch(() => {});
});

async function toggleSite(host, force) {
  const s = await F.getSettings();
  const sites = new Set(s.autoSites);
  const on = force === undefined ? !sites.has(host) : !!force;
  if (on) sites.add(host);
  else sites.delete(host);
  await F.setSettings({ autoSites: Array.from(sites) });
  return on;
}

function serverBase(settings) {
  return (settings.serverUrl || F.DEFAULTS.serverUrl).trim().replace(/\/+$/, "");
}

function offlineMessage(settings) {
  return `Fukidashi server is not running at ${serverBase(settings)}. Start it with start.bat (Windows) or start.sh (macOS/Linux).`;
}

/** Download an image as the page would: with cookies and the page URL as Referer. */
async function fetchImage(url, pageUrl) {
  if (url.startsWith("data:")) return (await fetch(url)).blob();
  await rulesReady;
  const ruleId = nextRuleId++;
  let ruleAdded = false;
  if (pageUrl && /^https?:/.test(pageUrl)) {
    try {
      await chrome.declarativeNetRequest.updateSessionRules({
        addRules: [
          {
            id: ruleId,
            priority: 1,
            action: {
              type: "modifyHeaders",
              requestHeaders: [{ header: "Referer", operation: "set", value: pageUrl }],
            },
            condition: {
              urlFilter: "|" + url + "|",
              tabIds: [chrome.tabs.TAB_ID_NONE], // only this worker's requests, never the page's
              resourceTypes: ["xmlhttprequest", "other"],
            },
          },
        ],
      });
      ruleAdded = true;
    } catch (e) {
      console.warn("Fukidashi: could not add Referer rule", e);
    }
  }
  try {
    let resp;
    try {
      resp = await fetch(url, { credentials: "include" });
    } catch (e) {
      throw new Error(`Could not download the image (${e.message})`);
    }
    if (!resp.ok) throw new Error(`Could not download the image (HTTP ${resp.status})`);
    return await resp.blob();
  } finally {
    if (ruleAdded) chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [ruleId] }).catch(() => {});
  }
}

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

function pickHeaders(resp) {
  const out = {};
  for (const name of HEADER_NAMES) {
    const v = resp.headers.get("x-fukidashi-" + name);
    if (v !== null) out[name] = v;
  }
  return out;
}

async function translate(msg) {
  const settings = await F.getSettings();
  const image = msg.dataUrl ? await (await fetch(msg.dataUrl)).blob() : await fetchImage(msg.url, msg.pageUrl);
  if (!image.size) throw new Error("The image is empty");

  const form = new FormData();
  form.append("image", image, "image");
  form.append("options", JSON.stringify(F.translateOptions(settings, msg.pageUrl, msg.refresh ? { refresh: true } : null)));

  let resp;
  try {
    resp = await fetch(serverBase(settings) + "/translate", {
      method: "POST",
      body: form,
      signal: AbortSignal.timeout(SERVER_TIMEOUT_MS),
    });
  } catch (e) {
    if (e.name === "TimeoutError") throw new Error("The server took too long to respond");
    throw new Error(offlineMessage(settings));
  }
  const headers = pickHeaders(resp);
  if (resp.status === 204) return { ok: true, noText: true, headers };
  if (!resp.ok) {
    let detail = headers.error || "";
    try {
      detail = (await resp.json()).error || detail;
    } catch (e) {
      /* not JSON */
    }
    throw new Error(detail || `Server error (HTTP ${resp.status})`);
  }
  const buffer = await resp.arrayBuffer();
  const type = resp.headers.get("content-type") || "image/png";
  return { ok: true, dataUrl: `data:${type};base64,${toBase64(buffer)}`, headers };
}

async function health() {
  const settings = await F.getSettings();
  try {
    const resp = await fetch(serverBase(settings) + "/health", { signal: AbortSignal.timeout(4000) });
    if (!resp.ok) return { ok: false, error: `Server responded HTTP ${resp.status}` };
    return { ok: true, health: await resp.json() };
  } catch (e) {
    return { ok: false, error: offlineMessage(settings) };
  }
}

async function resetContext(pageUrl) {
  const settings = await F.getSettings();
  try {
    const resp = await fetch(serverBase(settings) + "/context/reset", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session: pageUrl }),
      signal: AbortSignal.timeout(8000),
    });
    const body = await resp.json();
    if (!resp.ok) return { ok: false, error: body.error || `HTTP ${resp.status}` };
    return { ok: true, series: body.series, existed: body.existed };
  } catch (e) {
    return { ok: false, error: offlineMessage(settings) };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  const handlers = {
    translate: () => translate(msg),
    health: () => health(),
    resetContext: () => resetContext(msg.pageUrl),
    toggleSite: () => toggleSite(msg.host, msg.on).then((on) => ({ ok: true, on })),
  };
  const handler = handlers[msg && msg.type];
  if (!handler) return false;
  handler()
    .then(sendResponse)
    .catch((e) => sendResponse({ ok: false, error: e && e.message ? e.message : String(e) }));
  return true; // async response
});
