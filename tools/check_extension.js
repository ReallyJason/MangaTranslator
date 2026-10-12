/* Static checks for the extension: manifest shape, referenced files, icon sizes,
 * and `node --check` on every script.  Run: node tools/check_extension.js */
"use strict";

const fs = require("fs");
const path = require("path");
const { execFileSync } = require("child_process");

const EXT = path.resolve(__dirname, "../extension");
const errors = [];
const ok = (cond, msg) => {
  if (!cond) errors.push(msg);
};

let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(path.join(EXT, "manifest.json"), "utf8"));
} catch (e) {
  console.error("manifest.json is not valid JSON:", e.message);
  process.exit(1);
}

ok(manifest.manifest_version === 3, "manifest_version must be 3");
for (const k of ["name", "version", "description"]) ok(typeof manifest[k] === "string" && manifest[k], `missing ${k}`);
ok(/^\d+(\.\d+){0,3}$/.test(manifest.version), "version must be 1-4 dot-separated integers");
for (const p of ["storage", "contextMenus", "declarativeNetRequestWithHostAccess"]) {
  ok((manifest.permissions || []).includes(p), `missing permission ${p}`);
}
ok((manifest.host_permissions || []).includes("<all_urls>"), "host_permissions must include <all_urls>");
ok(manifest.background && manifest.background.service_worker, "missing background.service_worker");
ok(!manifest.background || !manifest.background.type, "service worker must be a classic script (uses importScripts)");
ok(manifest.commands && manifest.commands["toggle-auto"], "missing toggle-auto command");

const files = new Set();
files.add(manifest.background.service_worker);
for (const cs of manifest.content_scripts || []) (cs.js || []).forEach((f) => files.add(f));
files.add(manifest.action.default_popup);
const icons = Object.assign({}, manifest.icons, manifest.action.default_icon);
Object.values(icons).forEach((f) => files.add(f));

// Files referenced from the popup HTML.
const popup = fs.readFileSync(path.join(EXT, manifest.action.default_popup), "utf8");
for (const m of popup.matchAll(/(?:src|href)="([^"]+)"/g)) files.add(m[1]);
// importScripts in the worker.
const sw = fs.readFileSync(path.join(EXT, manifest.background.service_worker), "utf8");
for (const m of sw.matchAll(/importScripts\(([^)]+)\)/g)) {
  m[1].split(",").forEach((s) => files.add(s.trim().replace(/^["']|["']$/g, "")));
}

for (const f of files) ok(fs.existsSync(path.join(EXT, f)), `referenced file missing: ${f}`);

// PNG icon dimensions match their declared size.
for (const [size, f] of Object.entries(icons)) {
  const p = path.join(EXT, f);
  if (!fs.existsSync(p)) continue;
  const b = fs.readFileSync(p);
  const isPng = b.slice(1, 4).toString() === "PNG";
  ok(isPng, `${f} is not a PNG`);
  if (isPng) {
    const w = b.readUInt32BE(16);
    const h = b.readUInt32BE(20);
    ok(w === Number(size) && h === Number(size), `${f} is ${w}x${h}, expected ${size}x${size}`);
  }
}

// Syntax check every script.
for (const f of fs.readdirSync(EXT).filter((f) => f.endsWith(".js"))) {
  try {
    execFileSync(process.execPath, ["--check", path.join(EXT, f)], { stdio: "pipe" });
  } catch (e) {
    errors.push(`syntax error in ${f}: ${e.stderr}`);
  }
}

if (errors.length) {
  console.error("Extension check FAILED:\n - " + errors.join("\n - "));
  process.exit(1);
}
console.log(`Extension OK: manifest v3, ${files.size} referenced files present, icons sized correctly, scripts parse.`);
