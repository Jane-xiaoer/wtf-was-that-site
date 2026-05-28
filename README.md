# wtf-was-that-site

> One-click bookmark capture for people who hoard tools and can never remember
> their names. ⌘D in any Chromium browser → screenshot + AI analysis →
> written to your Notion database.
>
> Optional sync targets: Obsidian vault, your own Next.js front-end.

```
You hit ⌘D in any Chromium browser
   ↓ (FSEvents on the Bookmarks file  ‖  optional Chrome extension)
watcher.py
   ↓
capture.py
   ├─ Playwright opens the page → screenshot
   ├─ Gemini 2.5 Flash analyzes screenshot + DOM → structured JSON
   │  (name, headline, category, subcategory, capabilities, use cases, …)
   ├─ writes a new page to your Notion DB
   ├─ (optional) writes a Markdown note to your Obsidian vault
   └─ (optional) writes the cover into your tools-wall repo + triggers
       `vercel --prod` so a new card appears on your public site
```

What this repo gives you: the **capture pipeline**. Not a hosted website.
Bring your own Notion workspace, Gemini key, and (optionally) Vercel project.

---

## 5-minute install (macOS)

### 1. Get the code

```bash
git clone https://github.com/Jane-xiaoer/wtf-was-that-site.git
cd wtf-was-that-site
```

### 2. Python deps

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium      # downloads the headless browser
```

### 3. Make a Notion database

Open Notion → new page → `/database` → full page database. Add these fields:

| Property name      | Type        | Notes                                           |
|--------------------|-------------|-------------------------------------------------|
| `Name`             | Title       | site name                                       |
| `URL`              | URL         |                                                 |
| `Headline`         | Text        | one-line description                            |
| `Category`         | Select      | top-level bucket (12 categories — see below)    |
| `Subcategory`      | Select      | second-level bucket                             |
| `Tags`             | Multi-select|                                                 |
| `Capabilities`     | Multi-select|                                                 |
| `Use cases`        | Text        |                                                 |
| `Tech highlights`  | Text        |                                                 |
| `Cover`            | Files       | screenshot                                      |
| `Visit count`      | Number      | merged from your browser history                |
| `Status`           | Select      | 🆕 new / 🔍 occasional / 📦 regular / ⭐ frequent  |
| `Last visited`     | Date        |                                                 |
| `Added`            | Date        |                                                 |
| `网站介绍`           | Text        | long-form intro from URL Context (optional)     |
| `My Notes`         | Text        | YOUR manual notes — preserved across re-captures|

> **Faster way**: install Notion's official CLI and let it create the schema for you.
>
> ```bash
> curl -fsSL https://ntn.dev | bash
> ntn db create --schema schema/notion-db.json   # see schema/ for the JSON
> ```

Once the DB exists, share it with your Notion integration:
> Notion DB → **•••** menu → **Connections** → enable your integration.

The 12-category taxonomy used by the LLM lives in `capture.py:TAXONOMY` — feel
free to edit it to your own buckets before first run.

### 4. Configure

```bash
cp .env.example .env
$EDITOR .env
```

Required fields:

| Variable          | Where to get it                                                     |
|-------------------|---------------------------------------------------------------------|
| `NOTION_TOKEN`    | <https://www.notion.so/profile/integrations> → create internal integration |
| `NOTION_DB_ID`    | from the Notion DB URL: `notion.so/<workspace>/<DB_ID>?v=...`       |
| `GEMINI_API_KEY`  | <https://aistudio.google.com/apikey> (free tier is generous)        |

Everything else (Obsidian / tools wall / Feishu) is optional — leave blank to skip.

### 5. First run

```bash
python3 capture.py "https://example.com"
```

If that creates a row in your Notion DB, you're set. Move on to autostart.

### 6. Autostart on every ⌘D

Two listeners are available — you can run either or both.

**Path A — FSEvents (no extension needed)**

The watcher monitors `~/Library/Application Support/<browser>/<profile>/Bookmarks`
for every installed Chromium browser. Works for Chrome, Edge, Brave, Arc,
Tabbit, Dia, Vivaldi, Opera, etc. Lag is typically 0–30s depending on Chrome's
flush frequency.

```bash
python3 watcher.py
```

To run on boot, install the supplied launchd plist:

```bash
mkdir -p ~/Library/LaunchAgents
cp scripts/com.bookmark-watcher.plist.example ~/Library/LaunchAgents/com.bookmark-watcher.plist
# Edit the plist: replace {{PROJECT_DIR}} with the absolute path to this repo
launchctl load ~/Library/LaunchAgents/com.bookmark-watcher.plist
```

**Path B — Chrome extension (instant, recommended if Chrome Sync is on)**

See [`chrome-extension/README.md`](chrome-extension/README.md). Real-time
trigger via `chrome.bookmarks.onCreated`; works alongside Path A.

---

## Optional: your own tools wall

The pipeline can also push to your Next.js front-end (deploys via Vercel)
whenever a new tool is captured. That's a separate repo you build yourself —
see `.env.example` for the four variables (`TOOLS_WALL_DIR`, `TOOLS_WALL_URL`,
`WALL_REVALIDATE_SECRET`, `VERCEL_TOKEN`).

If you skip this entirely, captures still go to Notion — they just don't
auto-deploy anywhere.

---

## Optional: Obsidian sync

Set `OBSIDIAN_VAULT=/path/to/vault` in `.env`. Each capture writes a Markdown
note with the screenshot to `<vault>/<OBSIDIAN_SUBFOLDER>/`. Your manual notes
under the `## 📝 My Notes` heading are preserved across re-captures.

---

## File map

| File | What it does |
|---|---|
| `watcher.py` | FSEvents listener + embedded HTTP receiver for the Chrome ext + healthcheck + feedback loop |
| `capture.py` | Main pipeline: screenshot → Gemini → Notion (+ optional Obsidian / Wall / Feishu) |
| `feedback_collector.py` | Every 30 min, scans Notion for category edits you made; learns to classify like you |
| `reclassify.py` | One-off: re-classify the entire DB with the current `TAXONOMY` |
| `backfill_intros.py` | One-off: generate long-form intros (`网站介绍` field) for every site |
| `capture-from-frontmost.sh` | Hotkey helper — grab URL from the frontmost browser window, no bookmark needed |
| `chrome-extension/` | Optional MV3 extension for real-time ⌘D trigger |

State files (gitignored, created at runtime):
- `.classification_log.jsonl` — initial classification per capture
- `.classification_corrections.jsonl` — your manual edits, learned as fewshots
- `.feedback_cursor.json` — feedback collector's incremental cursor
- `.bookmark-baseline.json` / `.last-processed.json` — watcher dedup state
- `logs/` — watcher / capture / deploy / feedback logs

---

## How the AI learns your taste

After running for a while, you'll re-categorize tools by hand in Notion ("this
isn't 🎨 Visual, it's 🌟 Inspiration"). The feedback loop spots those edits
and injects them into the next Gemini prompt as fewshot examples — so the
classifier drifts toward your judgment over time.

The taxonomy is in `capture.py:TAXONOMY` — edit it freely. Just remember to
update the same dict in `reclassify.py` (and your front-end if you have one).

---

## Troubleshooting

| Symptom | Look at |
|---|---|
| watcher doesn't trigger | `tail logs/watcher.log` — search `⏭ 跳过重复` for dedup or `📌` for triggers |
| Notion writes fail | check `NOTION_TOKEN` is shared with the DB (Connections menu) |
| Cards show black/white covers | `is_uniform_image()` rejects same-color shots; some sites just look like that — set the Notion cover manually |
| Gemini 503 errors | the pipeline retries with backoff; if persistent, the free tier may be rate-limiting |
| Vercel deploy fails | `tail logs/deploy.log`; rotate `VERCEL_TOKEN` if it shows `token is not valid` |

---

## License

MIT. See [LICENSE](LICENSE).

Made by [xiaoer](https://github.com/Jane-xiaoer) because I could never
remember the names of the cool tools I found.

---

## 📱 关注作者 / Follow Me

如果这个仓库对你有帮助,欢迎关注我。后面我会持续更新更多 AI Skill、设计方法、网站美学和创意工作流。

If this repo helped you, follow me for more AI skills, design systems, web aesthetics, and creative workflows.

- X (Twitter): [@xiaoerzhan](https://x.com/xiaoerzhan)
- 微信公众号 / WeChat Official Account: 扫码关注 / Scan to follow

<p align="center">
  <img src="./follow-wechat-qrcode.jpg" alt="Jane WeChat Official Account QR code" width="300" />
</p>

<p align="center"><strong>中文:</strong>欢迎关注我的公众号,一起研究 AI Skill、设计原则、网站表达和创意工作流。</p>

<p align="center"><strong>English:</strong> Follow my WeChat Official Account for more AI skills, design principles, web aesthetics, and creative workflows.</p>
