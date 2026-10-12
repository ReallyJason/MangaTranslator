# Fukidashi

Fukidashi (吹き出し, "speech bubble") translates the text inside every manga, manhwa and webtoon
image on a page, automatically, as you scroll. A Chrome extension finds the images and a small
server running on your own computer reads, erases and re-letters them.

- **Context mode (LLM):** the whole page is translated in one go, with a running story summary
  and a character-name glossary remembered per series, so names and tone stay consistent across
  chapters. Works with Anthropic, OpenAI, or any OpenAI-compatible endpoint (Ollama, LM Studio,
  OpenRouter).
- **Fast mode (no LLM):** each text block is translated on its own with DeepL, Google, or a fully
  offline engine (Argos Translate).
- Speech bubbles are cleaned and re-lettered; text over artwork is removed with the LaMa
  inpainting model. Long webtoon strips are handled in tiles.
- API keys stay in `server/.env` on your machine. The extension never sees them.

Works in Chrome, Edge, Brave and other Chromium browsers.

```
 browser tab ──► extension ──image──► Fukidashi server (127.0.0.1:5003)
     ▲                                  1. find text  (EasyOCR detector)
     │                                  2. read text  (manga-ocr / EasyOCR)
     └──── translated image ◄────────   3. translate  (LLM, or DeepL / Google / offline)
                                        4. erase      (bubble fill / LaMa)
                                        5. letter     (Pillow, auto-fit)
```

---

## 1. Install the server

You need **Python 3.10 or newer** ([python.org](https://www.python.org/downloads/); on Windows tick
"Add python.exe to PATH"). Python 3.11 or 3.12 is the safest choice for the ML packages.

**Windows:** double-click `start.bat` (or run it from a terminal).

**macOS / Linux:**

```bash
./start.sh
```

The first run creates `server/.venv`, installs everything (a few GB with PyTorch and the OCR
models, so it takes a while), copies `server/.env.example` to `server/.env`, and starts the
server on `http://127.0.0.1:5003`. Later runs start in seconds. Keep the window open while you
read; close it (or press Ctrl+C) to stop the server.

Options for both scripts:

| Option        | What it does |
|---------------|--------------|
| `--reinstall` | Re-run the install, e.g. after pulling an update. |
| `--cpu`       | Never install the CUDA build of PyTorch. |
| `--core`      | Lightweight install without OCR / LaMa / offline engine. The server starts but can't find text, so this is only useful for development. |

Some models download the first time they're needed (EasyOCR ~100 MB, manga-ocr ~450 MB,
LaMa ~200 MB, offline language packs ~100 MB each), so the very first page is slow.

<details>
<summary>Manual install</summary>

```bash
cd server
python -m venv .venv
.venv/bin/pip install -r requirements-full.txt     # Windows: .venv\Scripts\pip ...
cp .env.example .env
.venv/bin/python -m uvicorn fukidashi.app:app --host 127.0.0.1 --port 5003
```
</details>

### GPU (NVIDIA / CUDA)

A GPU makes OCR and inpainting several times faster, but it's optional: everything runs on the
CPU too (roughly 5–15 s per page on a recent laptop; typically 1–2 s with a GPU).

- The start scripts install the CUDA build of PyTorch automatically when `nvidia-smi` is found.
  The default is CUDA 12.8 wheels; with an older driver, pick another build before installing, e.g.
  `set TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126` (Windows) or
  `TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126 ./start.sh --reinstall`.
- Check the popup or `http://127.0.0.1:5003/health`: it shows `GPU: <your card>` when CUDA works.
- `USE_GPU=false` in `server/.env` forces the CPU. Apple Silicon and AMD GPUs are not used
  (it runs on the CPU there).

## 2. Load the extension

1. Open `chrome://extensions` (Edge: `edge://extensions`, Brave: `brave://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and select the `extension` folder of this repository.
4. Pin Fukidashi to the toolbar. The dot in its popup turns green when the server is running.

After updating the code, click the reload icon on the extension's card and reload your manga tabs.

## 3. Use it

Open a chapter, click the Fukidashi icon and turn on **Auto-translate this site**. Images are
translated a little ahead of where you are reading, top to bottom, and swapped in place. A small
pill in the bottom-right corner shows progress ("Translating 3/12") and any problems.

| Control | What it does |
|---|---|
| Auto-translate *this site* / *all sites* | Translate images automatically as you scroll. |
| **Alt+Shift+T** | Toggle auto-translate for the current site (change it at `chrome://extensions/shortcuts`). |
| Right-click an image → **Translate this image** | Translate one image (any size), or redo it. |
| Translate all now | Translate every image on the page now, even far below. |
| Re-translate page | Redo the page, skipping the cache (e.g. after changing language or mode). |
| Show originals | Flip between original and translated images. |
| Context / Fast | Choose the translation mode (below). |
| Fast engine | Engine for fast mode, and for the fallback when the LLM isn't available. |
| Source → target language | Source: Japanese, Korean, Chinese, English and more; target: any listed language. |
| Min image size | Images smaller than this (in either dimension) are ignored, which skips logos and avatars. |
| Reset story memory for this series | Forget the summary and glossary for this series. |
| Advanced | Server URL, font, minimum on-screen size. |

## 4. Choosing a mode

### Context mode (LLM)

Best quality. All text on a page goes to the LLM in one request together with a short story
summary and a glossary of names. The LLM returns the translations plus an updated summary and
glossary, which are saved per **series**. A series is the site plus the URL path without its last
part, so `site.com/manga/some-title/chapter-12` and `.../chapter-13` share memory.

Configure it in `server/.env` (then restart the server):

```ini
# Anthropic (default)
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
# LLM_MODEL=claude-haiku-5-5      (default)

# OpenAI
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini

# Ollama: free and fully local (run `ollama pull qwen2.5:14b` first)
LLM_PROVIDER=ollama
OPENAI_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:14b

# LM Studio / OpenRouter / anything OpenAI-compatible
LLM_PROVIDER=lmstudio            # or openrouter
OPENAI_BASE_URL=http://localhost:1234/v1
LLM_MODEL=<model name>
OPENAI_API_KEY=                  # if the endpoint needs one
```

If the LLM isn't configured or a request fails, that page is translated in fast mode instead and
the pill tells you why (e.g. *Context mode unavailable: ANTHROPIC_API_KEY is not set*). Pages that
needed a fallback aren't cached, so they're retried with the LLM next time.

### Fast mode (no LLM)

Each text block is translated on its own: quick and cheap, but without story context.

| Engine | Setup | Notes |
|---|---|---|
| DeepL | `DEEPL_API_KEY=...` | Free keys (ending in `:fx`) use the free API automatically. |
| Google | `GOOGLE_API_KEY=...` | Cloud Translation v2 (Basic). |
| Offline | nothing | Argos Translate; downloads language packs on first use and goes through English when there's no direct pair (e.g. Japanese → German). Lower quality, fully private. |
| Auto | — | DeepL if configured, else Google, else offline. |

If the chosen engine fails, the others are tried and the pill says which one was used.

## 5. Fonts

The server uses, in order:

1. Fonts you put in `server/fonts/` (`.ttf`, `.otf`, `.ttc`). With several, type one's file name
   (without extension) in **Advanced → Font**; otherwise the first alphabetically is used.
2. Common bold sans-serif system fonts (Arial, Segoe UI, DejaVu Sans, Liberation Sans, Noto Sans…).
3. Pillow's built-in font.

A comic-lettering font makes a big difference. Use one whose license allows it; for example
*Comic Neue* is free under the SIL Open Font License.

For **Japanese, Chinese or Korean target languages**, a font is only used if it actually contains
those characters. Otherwise a CJK system font is used (Yu Gothic / Meiryo / Microsoft YaHei /
Malgun Gothic on Windows, Hiragino / PingFang / Apple SD Gothic on macOS, Noto Sans CJK on
Linux).

## 6. Troubleshooting

**The popup says "Offline" / the pill says "Fukidashi server is not running".**
Start the server with `start.bat` / `start.sh` and keep its window open. If you changed `PORT` in
`server/.env`, set the same URL in the popup under **Advanced → Server URL**. Check
<http://127.0.0.1:5003/health> in a browser tab: it should show a page of JSON.

**Nothing happens on a page.**
Turn on auto-translate for the site, or use **Translate all now**. Tabs that were open before
the extension was installed or reloaded need a page reload. Very small images are skipped on
purpose (see *Min image size*). If the popup says "EasyOCR missing", run `start.bat --reinstall`
/ `./start.sh --reinstall` and watch for install errors.

**"Could not download the image (HTTP 403)".**
The site's image server refused the download. Fukidashi sends your cookies and the page address
like the page itself does, which satisfies most hotlink protection. Make sure you're logged in on
the site if it requires it.

**The site keeps swapping images back / translations disappear.**
Some readers rebuild their images constantly. Fukidashi re-translates a swapped image up to three
times, then stops and says so in the pill. Canvas-based readers (the page is drawn, not an
`<img>`) and CSS background images aren't supported.

**The page blocks replaced images.**
A strict Content-Security-Policy can forbid `blob:` images; Fukidashi then falls back to a
`data:` URL. If both are blocked, the pill says "This page blocks replaced images" and the original
stays.

**Squares (□□□) instead of Japanese / Chinese / Korean text.**
No font with those characters was found. Install a CJK font (e.g. Noto Sans CJK, free) or drop
one into `server/fonts/`, then use **Re-translate page**.

**Old results keep coming back / clearing the cache.**
Results are cached by image and settings, so revisiting a chapter is instant. **Re-translate
page** skips the cache for that page. To clear everything, stop the server and delete
`server/data/cache/`, or while it's running:

```bash
curl -X POST http://127.0.0.1:5003/cache/clear      # Windows PowerShell: curl.exe -X POST ...
```

The cache is limited by `CACHE_MAX_MB` (default 1024). Story memory lives in `server/data/series/`.

**Slow translations.** Without a GPU, each page takes several seconds; the first page also loads
the models. In context mode pages go one at a time to keep the story in order; fast mode does two
at a time.

**Japanese text read badly.** Make sure manga-ocr installed (`"manga_ocr": true` on the `/health`
page). Without it, EasyOCR is used, which is weaker on manga.

## 7. Privacy

Images are processed on your computer. Only the **recognised text** is sent out: to your LLM
provider in context mode, or to DeepL / Google in fast mode. With Ollama/LM Studio or the offline
engine nothing leaves your machine. The server only listens on `127.0.0.1` and refuses requests from
web pages, so other sites can't use it (or your API keys).

## 8. Development

```bash
# Server tests (no API keys or models needed)
cd server && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest

# Extension: manifest, referenced files, icon sizes, syntax
node tools/check_extension.js

# End-to-end: headless Chromium with the unpacked extension against a local test site.
# Needs the Fukidashi server running (./start.sh) with any translation backend configured.
server/.venv/bin/python tools/test_page/serve.py &     # test site on :8080, image "CDN" on :8081
npm install playwright && npx playwright install chromium
node tools/test_page/run_e2e.js                        # screenshots in tools/test_page/output/
```

`tools/make_icons.py` regenerates the toolbar icons. The server API:

| Endpoint | |
|---|---|
| `GET /health` | Engines and LLM configured, GPU, OCR/inpainting status, user fonts. |
| `POST /translate` | Multipart `image` + `options` JSON `{mode, engine, source_lang, target_lang, session, font}`. Returns a PNG, or `204` if no text was found. `X-Fukidashi-*` headers report mode, engine, notes and cache status. |
| `POST /context/reset` | `{"session": "<page url>"}` forgets that series' story memory (`{"all": true}` for all). |
| `GET /context?session=<url>` | Shows the stored summary and glossary. |
| `POST /cache/clear` | Empties the result cache. |

Not supported (yet): translating regular page text, CSS background images, canvas-based readers,
images inside iframes, Firefox, PDF upload, in-page editing of translations.
