# Chrome extension — Bookmark Capture

> Install once (~1 min). Then ⌘D bookmarks trigger the local capture pipeline
> **in real time** — bypassing Chrome Sync's flaky flush of the on-disk
> `Bookmarks` file.

---

## Install (Chrome → developer mode → load unpacked)

**1.** Open `chrome://extensions` in Chrome.

**2.** Toggle on **Developer mode** (top-right corner).

**3.** Click **Load unpacked** (top-left).

**4.** Select this folder (`chrome-extension/` inside your clone of this repo).

**5.** You should see a card titled **Bookmark Capture**, status: enabled, no
red error text → it's working.

**6.** (Optional) hide the toolbar icon: click the puzzle icon → unpin.

---

## Verify

1. Visit any site → **⌘D** to bookmark it (confirm the popup).
2. Within 30 seconds the watcher should log it:

   ```bash
   tail -10 logs/watcher.log
   # expect: 📌 [Chrome扩展] 新书签: ...
   ```

3. Check your Notion DB — a new row should appear.

---

## How it works

```
You ⌘D
  → Chrome fires chrome.bookmarks.onCreated (ms-level)
  → this extension's service worker POSTs to http://localhost:7331/hook
  → watcher.py's embedded HTTP server picks it up → runs capture.py
  → Playwright fetches the page → Gemini analyzes → writes to Notion (+ optional Obsidian / wall)
```

Why an extension instead of just polling the file?
- The on-disk `Bookmarks` JSON is flushed lazily when Chrome Sync is on (sometimes hours).
- Decoding the `Sync Data/LevelDB/` binary is fragile across Chrome updates.
- The official extension API is stable and instant.

---

## Troubleshooting

**Card shows red error**
→ Probably a `manifest.json` parse error. Reload the extension in chrome://extensions.

**⌘D doesn't seem to trigger anything**

```bash
# 1. is watcher running?
ps aux | grep watcher.py | grep -v grep

# 2. is the HTTP receiver listening?
curl http://localhost:7331/
# should return: {"ok":true,"service":"bookmark-capture-receiver"}

# 3. recent watcher log?
tail -20 logs/watcher.log

# 4. capture pipeline log?
tail -30 logs/capture.log
```

**Privacy: does it read all my bookmarks?**
No. It only listens to **new** bookmark events (`bookmarks.onCreated`) — never
reads history. Data goes only to `localhost:7331`, never off-device.

**Multiple Chrome profiles?**
Install once per profile (Chrome isolates extensions by profile).
