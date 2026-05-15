#!/usr/bin/env python3
"""
Website Capture: URL → Notion 网站收藏库
Usage: capture.py <URL>
"""
import sys, os, json, re, sqlite3
# Fix stale SSL_CERT_FILE pointing to non-existent /tmp/*.pem (Mac quirk)
_ssl_file = os.environ.get("SSL_CERT_FILE")
if _ssl_file and not os.path.exists(_ssl_file):
    os.environ.pop("SSL_CERT_FILE", None)
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types
import requests

# Chromium browser history paths (all use same SQLite schema)
BROWSER_HISTORIES = {
    "Chrome": Path.home() / "Library/Application Support/Google/Chrome/Default/History",
    "Tabbit": Path.home() / "Library/Application Support/Tabbit/Default/History",
    "Edge": Path.home() / "Library/Application Support/Microsoft Edge/Default/History",
    "Brave": Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser/Default/History",
    "Arc": Path.home() / "Library/Application Support/Arc/User Data/Default/History",
}

def query_browser_history(domain: str):
    """Aggregate visit_count + last_visit_time across all Chromium browsers.
    Chrome stores time as microseconds since 1601-01-01 UTC."""
    total_visits = 0
    latest_time = 0
    breakdown = {}
    for browser, db_path in BROWSER_HISTORIES.items():
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True, timeout=2)
            cur = conn.cursor()
            # Count actual visit events from visits table (more accurate than urls.visit_count)
            cur.execute("""
                SELECT COUNT(*), COALESCE(MAX(v.visit_time), 0)
                FROM visits v JOIN urls u ON v.url = u.id
                WHERE u.url LIKE ? OR u.url LIKE ?
            """, (f"%//{domain}/%", f"%//{domain}"))
            vc, lt = cur.fetchone()
            conn.close()
            if vc:
                total_visits += vc
                breakdown[browser] = vc
            if lt and lt > latest_time:
                latest_time = lt
        except Exception:
            pass
    last_iso = None
    if latest_time:
        epoch_seconds = (latest_time / 1_000_000) - 11644473600
        try:
            last_iso = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date().isoformat()
        except Exception:
            last_iso = None
    return {"visit_count": int(total_visits), "last_visited": last_iso, "breakdown": breakdown}

def visit_count_to_status(n: int) -> str:
    if n > 30: return "⭐ 高频"
    if n >= 11: return "📦 常用"
    if n >= 3: return "🔍 偶尔"
    return "🆕 待试"

# ---------- GitHub-aware ----------
GITHUB_REPO_RE = re.compile(r"^https?://github\.com/([^/?#]+)/([^/?#]+)(?:/.*)?/?$")

def parse_github_repo(url: str):
    """如果是 github 仓库 URL,返回 (owner, repo);否则 None。"""
    m = GITHUB_REPO_RE.match(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).rstrip("/")
    if owner in ("settings", "marketplace", "topics", "explore", "trending", "issues"):
        return None
    return owner, repo

def fetch_github_metadata(owner: str, repo: str):
    """调 GitHub API 拿真实 metadata (公开 repo, 无需 token, 60req/h limit)."""
    try:
        r = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        d = r.json()
        return {
            "stars": d.get("stargazers_count", 0),
            "forks": d.get("forks_count", 0),
            "language": d.get("language") or "",
            "topics": d.get("topics") or [],
            "description": d.get("description") or "",
            "license": (d.get("license") or {}).get("name", ""),
            "default_branch": d.get("default_branch", "main"),
            "homepage": d.get("homepage") or "",
            "archived": d.get("archived", False),
            "size_kb": d.get("size", 0),
        }
    except Exception:
        return None

# ---------- Config ----------
import shutil

PROJECT_ROOT = Path(__file__).parent
ENV_FILE = PROJECT_ROOT / ".env"
SCREENSHOT_DIR = Path(os.path.expanduser(
    os.environ.get("SCREENSHOT_DIR", "~/Pictures/bookmark-captures")
))
LOG_DIR = PROJECT_ROOT / "logs"
NOTION_VERSION = "2022-06-28"

# State files (gitignored — produced at runtime)
CLASSIFY_LOG = PROJECT_ROOT / ".classification_log.jsonl"
CORRECTIONS_FILE = PROJECT_ROOT / ".classification_corrections.jsonl"

# 两层分类树。一级 + 二级。capture.py / reclassify.py / 工具墙 lib/types.ts 三处必须保持同步。
TAXONOMY = {
    "🎨 视觉创作": ["图像生成", "图像处理", "视频生成", "设计资源", "3D / 动效"],
    "✍️ 文字写作": ["写作助手", "内容平台", "翻译润色"],
    "🌐 网页与代码": ["AI 编程助手", "部署 / 建站", "组件 / UI 库", "开发工具", "CLI 工具"],
    "🔊 声音": ["音乐生成", "语音 / TTS", "音频处理"],
    "🌟 灵感与审美": ["设计灵感", "字体 / 排版", "配色 / 渐变", "艺术创意编程"],
    "📚 知识与学习": ["学习平台", "电子书", "工具书 / 词典"],
    "🛠️ 办公与效率": ["PPT 演示", "笔记 / Notion", "自动化 / 无代码", "浏览器扩展", "其他效率"],
    "🎮 兴趣娱乐": ["游戏", "趣味"],
    "📦 资源集合": ["Awesome 合集", "工具导航", "素材集"],
    "🌍 出海与基建": ["API 中转", "VPN / 网络", "跨境支付"],
    "🤖 AI 大模型": ["提示词工程", "大模型对话", "多模态 / Agent"],
    "🔬 其他": ["其他"],
}
CATEGORIES = list(TAXONOMY.keys())
TAXONOMY_TREE_STR = "\n".join(f"- {c}: {', '.join(subs)}" for c, subs in TAXONOMY.items())

CATEGORY_HINTS = """
分类边界提示 (避免归错):
- 🤖 AI 大模型: ChatGPT/Claude/Gemini/豆包/Perplexity/秘塔 等通用对话模型 (不要混进其他类)
- 🌍 出海与基建: VPN/代理/外币卡/港卡/海外银行/API 中转/海外 SIM/海外注册/合规工具
- 🌐 网页与代码: 编程工具/IDE/代码生成/前端模板/部署工具/网站搭建
- 🛠️ 办公与效率: PPT/笔记/文档/PDF/翻译插件/会议工具
- 🎨 视觉创作: 图像生成/抠图/修图/视频生成/3D/插画
- 🔊 声音: 音乐生成/AI 作曲/语音合成 TTS/音频处理。Suno / Udio / MusicGen / ElevenLabs / Whisper / 任何「Suno API 反代」「音乐生成 API」「TTS 工具」即便是 GitHub TypeScript repo 也归 🔊 声音 而非 🌐 网页与代码——判断「这工具的输出物是什么」,输出是音频/音乐就归 🔊
"""

# ---------- Env loading ----------
def load_env():
    """Read project-local .env. Required keys missing → exit with friendly hint."""
    env = {}
    if not ENV_FILE.exists():
        raise SystemExit(
            f"❌ 找不到 .env (expected at: {ENV_FILE})\n"
            f"   cp .env.example .env  然后填 NOTION_TOKEN / NOTION_DB_ID / GEMINI_API_KEY"
        )
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()

# Required
NOTION_TOKEN = ENV.get("NOTION_TOKEN", "")
GEMINI_API_KEY = ENV.get("GEMINI_API_KEY", "")
DATABASE_ID = ENV.get("NOTION_DB_ID", "")
if not (NOTION_TOKEN and GEMINI_API_KEY and DATABASE_ID):
    raise SystemExit("❌ .env 缺必需字段: NOTION_TOKEN / GEMINI_API_KEY / NOTION_DB_ID")

# Notion data source id (only relevant for 2025-09-03+ API; otherwise same as DB id)
DATA_SOURCE_ID = ENV.get("NOTION_DATA_SOURCE_ID", DATABASE_ID)

# Optional: Obsidian sync (skip if not set)
OBSIDIAN_VAULT = Path(os.path.expanduser(ENV["OBSIDIAN_VAULT"])) if ENV.get("OBSIDIAN_VAULT") else None
OBSIDIAN_SUBFOLDER = ENV.get("OBSIDIAN_SUBFOLDER", "🌐 网站收藏库")
OBSIDIAN_DIR = (OBSIDIAN_VAULT / OBSIDIAN_SUBFOLDER) if OBSIDIAN_VAULT else None
OBSIDIAN_ATTACHMENTS_DIR = (OBSIDIAN_DIR / "attachments") if OBSIDIAN_DIR else None

# Optional: tools wall (Next.js front-end). 没填就跳过 cover 同步 + revalidate + deploy
TOOLS_WALL_DIR = Path(os.path.expanduser(ENV["TOOLS_WALL_DIR"])) if ENV.get("TOOLS_WALL_DIR") else None
TOOLS_WALL_URL = ENV.get("TOOLS_WALL_URL", "")
WALL_URL = TOOLS_WALL_URL  # backwards-compat alias
WALL_REVALIDATE_SECRET = ENV.get("WALL_REVALIDATE_SECRET", "")
VERCEL_TOKEN = ENV.get("VERCEL_TOKEN", "")

# Optional: Feishu / Lark bitable sync
ENABLE_FEISHU = ENV.get("ENABLE_FEISHU", "").lower() in ("true", "1", "yes")
FEISHU_BASE_TOKEN = ENV.get("FEISHU_BASE_TOKEN", "")
FEISHU_TABLE_ID = ENV.get("FEISHU_TABLE_ID", "")
LARK_CLI = ENV.get("LARK_CLI") or shutil.which("lark-cli") or ""
FEISHU_AS = ENV.get("FEISHU_AS", "user")

# Notion property name for the user's personal notes (preserved across re-captures).
# Default "My Notes" matches schema/notion-db.json. If your DB uses a different
# name, set NOTES_FIELD_NAME in .env to match it exactly.
NOTES_FIELD_NAME = ENV.get("NOTES_FIELD_NAME", "My Notes")

# ---------- Helpers ----------
def log(msg):
    print(msg, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "capture.log", "a") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")

def notify(title, msg):
    safe_title = title.replace('"', "'")
    safe_msg = msg.replace('"', "'")
    os.system(f'osascript -e \'display notification "{safe_msg}" with title "{safe_title}" sound name "Glass"\'')

# ---------- Cover 质量检查 ----------
def is_uniform_image(jpg_bytes: bytes, threshold: float = 0.85) -> bool:
    """检测截图是否是同色画面(黑/白/某 BG 色铺满)。
    threshold: 同色像素比例超过这个值就认为 cover 没用。
    用稀疏采样 (每 8x8 取 1 个 pixel), 不读全部像素。"""
    if not jpg_bytes or len(jpg_bytes) < 1000:
        return True  # 太小八成有问题
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(jpg_bytes)).convert("RGB")
        w, h = img.size
        # 稀疏采样
        samples = []
        for y in range(0, h, 8):
            for x in range(0, w, 8):
                samples.append(img.getpixel((x, y)))
        if not samples:
            return True
        # 找主色 (按 RGB 16-step 量化 + 投票)
        from collections import Counter
        quantized = [(r // 16, g // 16, b // 16) for r, g, b in samples]
        most_common, count = Counter(quantized).most_common(1)[0]
        ratio = count / len(quantized)
        return ratio > threshold
    except Exception as e:
        log(f"  ⚠ is_uniform_image 出错(放过): {e}")
        return False

# ---------- Step 1: scrape ----------
def fetch_page(url: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"),
        )
        page = ctx.new_page()
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        except Exception as e:
            log(f"⚠ page.goto warning: {e}")
        title = page.title() or ""

        # 截图: 不等字体加载 (animations='disabled' + 短 timeout), 失败给小占位图
        def safe_screenshot(full_page=False, quality=85):
            try:
                return page.screenshot(
                    full_page=full_page,
                    type="jpeg",
                    quality=quality,
                    timeout=10000,
                    animations="disabled",
                )
            except Exception as e:
                log(f"  ⚠ 截图失败({'full' if full_page else 'fold'}): {str(e)[:120]}")
                return None

        screenshot = safe_screenshot(full_page=False)

        # cover 质量检查: 检测同色画面 (黑屏/白屏/单色 canvas 未渲染)
        # 如果是 → 再等 4s 让 webgl/canvas 动画稳定后重抓
        if screenshot and is_uniform_image(screenshot, threshold=0.85):
            log(f"  ⚠ 首抓 cover 检测同色像素 > 85% (可能 canvas/webgl 未稳定), 等 4s 重抓")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(4000)
            retry = safe_screenshot(full_page=False)
            if retry and not is_uniform_image(retry, threshold=0.85):
                log("  ✓ 重抓正常, 用新 cover")
                screenshot = retry
            elif retry:
                log("  ⚠ 重抓还是同色, 后面 create_page 会落回 og:image")
                screenshot = retry  # 仍保留作为 fallback,但 create_page 会评判

        full_screenshot = safe_screenshot(full_page=True, quality=78) or screenshot
        # 都失败的话给一个透明 1x1 占位 jpg, 让后续流程不崩
        if not screenshot:
            log("  ⚠ 两次截图都失败, 用占位图继续 AI 分析")
            screenshot = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd0\xff\xd9"
            full_screenshot = screenshot
        og_image = page.evaluate("""() => {
            const m = document.querySelector('meta[property="og:image"]')
                  || document.querySelector('meta[name="twitter:image"]');
            return m ? m.content : null;
        }""")
        og_desc = page.evaluate("""() => {
            const m = document.querySelector('meta[property="og:description"]')
                  || document.querySelector('meta[name="description"]');
            return m ? m.content : '';
        }""") or ""
        page_text = page.evaluate("""() => (document.body.innerText || '').slice(0, 6000)""")
        browser.close()
        return {
            "title": title,
            "screenshot": screenshot,
            "full_screenshot": full_screenshot,
            "og_image": og_image,
            "og_desc": og_desc,
            "page_text": page_text,
        }

# ---------- Classification feedback (B 任务): 加载 the user 历史更正作为 fewshot ----------
def load_correction_fewshots(max_count=15):
    """读 .classification_corrections.jsonl,挑出最近的 N 条,format 成 prompt fewshot 字符串。
    每条记录 schema: {url,name,headline,was:{cat,sub},is:{cat,sub},corrected_at}
    返回空串说明还没积累到反馈数据。"""
    if not CORRECTIONS_FILE.exists():
        return ""
    try:
        lines = CORRECTIONS_FILE.read_text().strip().splitlines()
    except Exception:
        return ""
    recent = []
    for ln in lines[-200:]:  # 只看最近 200 条
        try:
            recent.append(json.loads(ln))
        except Exception:
            continue
    if not recent:
        return ""
    # 取最新 max_count 条
    picks = recent[-max_count:]
    items = []
    for c in picks:
        was = c.get("was") or {}
        is_ = c.get("is") or {}
        line = f"- {c.get('name','?')} ({c.get('url','?')}): the user changed 它从 [{was.get('cat','?')} / {was.get('sub','—') or '—'}] 改成 [{is_.get('cat','?')} / {is_.get('sub','—') or '—'}]"
        if c.get('headline'):
            line += f" — '{c['headline'][:50]}'"
        items.append(line)
    return (
        "\n\n# the user's 历史分类偏好 (她手工改过的,要学这些规律)\n"
        + "\n".join(items)
        + "\n请参照这些案例,推断她对当前工具的偏好。"
    )

def append_classify_log(page_id, url, name, headline, cat, sub):
    """capture 写完 Notion 后追加,供 feedback_collector 后续对比手工修改。"""
    try:
        rec = {
            "page_id": page_id, "url": url, "name": name,
            "headline": headline[:120], "cat": cat, "sub": sub,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        with open(CLASSIFY_LOG, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"  ⚠ classify log 写入失败: {e}")

# ---------- Step 2: AI analyze ----------
def analyze(url, pd):
    client = genai.Client(api_key=GEMINI_API_KEY)

    # GitHub-aware: 如果是 GitHub repo, 先拉真实 metadata 注入 prompt
    gh_meta = None
    gh = parse_github_repo(url)
    if gh:
        log(f"  🐙 检测到 GitHub repo: {gh[0]}/{gh[1]}, 拉 GitHub API metadata")
        gh_meta = fetch_github_metadata(gh[0], gh[1])
        if gh_meta:
            log(f"     ⭐ {gh_meta.get('stars',0)} | 🔤 {gh_meta.get('language','?')} | 🏷️ {gh_meta.get('topics',[])[:5]}")

    github_section = ""
    if gh_meta:
        github_section = f"""
[GitHub 真实 metadata - 已从 API 拉取]
Owner/Repo: {gh[0]}/{gh[1]}
Stars: {gh_meta['stars']}
Language: {gh_meta['language']}
Topics: {', '.join(gh_meta['topics'])}
GitHub Description: {gh_meta['description']}
License: {gh_meta['license']}
Homepage: {gh_meta['homepage']}
Archived: {gh_meta['archived']}

【★ 因为这是 GitHub 仓库,额外输出 `github` 字段】:
"github": {{
  "repo_type": "10 选 1: cli / library / app / skill / agent / mcp / dataset / awesome 合集 / 其他",
  "language": "主编程语言 (e.g. Python / TypeScript / JavaScript / Go / Rust / Shell / Lua / Swift / C++ / Java / Markdown / HTML)。如果不确定就空字符串",
  "install_cmd": "如何安装/使用,15-50 字。示例: 'pip install u-2-net' / 'npm install -g xxx' / 'brew install foo' / 'git clone xxx && python main.py'。如果是 awesome 合集就写 '直接浏览,无需安装'。"
}}
"""

    prompt = f"""你是 the user's 工具检索助手。看一个工具时,你的任务**不是描述它**——而是 **逆向模拟搜索者**:
"未来 the user 要找它/类似它的工具时,会怎么问? 该工具的 metadata 里要有什么词,才能命中?"

按这个心智产出 JSON (不要 markdown 代码块、不要解释):

{{
  "name": "工具/网站名 (2-12 字,英文原名优先)",
  "headline": "一句话定位,15-30 字。要含 2-3 个高频检索词。例: '轻量级 JavaScript 动画引擎,专注 SVG 与 DOM 动效'",
  "intro": "1-2 段背景介绍,100-200 字。'这是 [名字] 的官方网站。它是 ...' 开头。不要带'the user'/'用户'。",
  "category": "从下面 12 个一级类里选 1 个。",
  "subcategory": "从所选 category 对应的二级类里选 1 个 (见下方分类树)。若实在不属于任何二级,留空字符串。",
  "tags": ["5-8 个 flat 标签 (不加维度前缀!),从下面 controlled vocabulary 优先挑,覆盖 4-5 个维度: [输出物]图像/视频/文字/音频/代码/网页/3D/PPT/PDF [任务]抠图/修图/配色/翻译/排版/配音/生成/剪辑/检测/总结/搜索/部署/提取/转换/修复/编辑 [商业]免费/付费/开源/试用 [语言]中文/英文/双语 [属性]在线工具/桌面app/浏览器扩展/API/模板/Awesome合集/SaaS [特殊]AI生成/无需登录/需VPN/中转/国产"],
  "capabilities": ["5-8 个 '动词+具体对象' 短语,每条 5-15 字。每条都是 query 的候选答案。✅ '把图片抠成透明背景' / '生成 React 组件代码'。❌ '支持图片处理' (太抽象)"],
  "scenarios": ["3-6 个 '在做 X 时' 表达,**同一意图换 2-3 种说法** (query expansion 提高召回)。例: '做品牌 logo 时' + '想给产品做 vi 系统时' + '自由职业要做品牌识别时' (三种说法对应同一意图)"],
  "search_keywords": ["6-12 个用户可能用的搜索词,**模糊回忆查询的命脉**。覆盖: 中文同义词 + 英文别名 + 行业黑话 + 口语化表达。例 (anime.js): ['JS动画','网页动画库','前端动效','SVG动画','scroll trigger','时间轴动画','网页动起来','做交互']"],
  "alternatives": {{
    "replaces": ["此工具替代了什么传统流程,0-3 项。例: '传统手写 CSS 动画','jQuery animate'"],
    "similar_to": ["同类工具 (含外部知名),3-6 项。例: 'GSAP','Motion One','Velocity.js'"],
    "pairs_with": ["经常搭配使用,0-3 项。例: 'v0','Framer Motion'"]
  }},
  "tech_highlights": ["可选 0-4 个技术亮点。例: '体积 27KB','原生 WAAPI 支持'"]
}}

# 一级 + 二级 分类树 (category 必须从一级里选,subcategory 必须从对应一级下的二级里选)
{TAXONOMY_TREE_STR}

{CATEGORY_HINTS}

判断 category 时优先看「输出物本质」(图像/视频/音频/文字/代码/网页/...) 和「主要任务」,而非工具的实现语言或承载形式。GitHub TypeScript repo 不等于 🌐 网页与代码。
{load_correction_fewshots()}
{github_section}

URL: {url}
页面标题: {pd['title']}
og:description: {pd['og_desc']}
页面文字 (节选):
{pd['page_text']}
"""
    img_part = types.Part.from_bytes(data=pd["screenshot"], mime_type="image/jpeg")
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[img_part, prompt],
    )
    text = (resp.text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text)
    # GitHub stars 数据是真实从 API 拉的, 直接附在 analysis 里
    if gh_meta:
        parsed["_stars"] = gh_meta.get("stars", 0)
        parsed.setdefault("github", {})
        if not parsed["github"].get("language") and gh_meta.get("language"):
            parsed["github"]["language"] = gh_meta["language"]

    # 二审: 挑刺角色, 能推翻一级+二级, 必须给 verdict
    parsed = classification_audit(client, url, parsed)

    return parsed


# ---------- Classification audit (二审) ----------
CLASSIFY_AUDIT_LOG = PROJECT_ROOT / ".classification_audit.jsonl"

def classification_audit(client, url, analysis):
    """挑刺角色二审一级+二级分类。必须给 verdict (ok / swap_sub / swap_both)。
    任何修改写到 .classification_audit.jsonl 供 the user 后期分析"哪类最易错"。"""
    cat1 = analysis.get("category", "")
    sub1 = analysis.get("subcategory", "") or ""
    name = analysis.get("name", "")
    headline = analysis.get("headline", "")
    capabilities = analysis.get("capabilities", []) or []
    scenarios = analysis.get("scenarios", []) or []

    if cat1 not in TAXONOMY:
        return analysis  # 一级压根不在 TAXONOMY,跳过二审 (上游兜底已处理)

    fewshot = load_correction_fewshots(max_count=20)

    audit_prompt = f"""你是 the user's 工具分类「挑刺专家」。一审已经给出分类,你的任务是**质疑**它,而不是附和。

一审分类:
- 一级 (category): {cat1}
- 二级 (subcategory): {sub1 or '(空)'}

工具信息:
- URL: {url}
- 名字: {name}
- 一句话: {headline}
- 能做什么: {chr(10).join('  - ' + str(c) for c in capabilities[:8])}
- 适合场景: {' / '.join(str(s) for s in scenarios[:5])}

# 你必须从下面 12 个一级里选 1 个,并从对应二级里选 1 个 (二级必填,不允许空)
{TAXONOMY_TREE_STR}

{CATEGORY_HINTS}
{fewshot}

# 挑刺规则
1. 优先看「输出物本质」(图像/视频/音频/文字/代码/网页/...) 和「主要任务」
2. **不要被 GitHub repo / TypeScript 语言 / SaaS 形式带偏** — 这些是承载形式,不是本质分类依据
3. **艺术家 / 设计师个人作品集 / Showcase 网站** → 🌟 灵感与审美,不是 🌐 网页与代码
4. **音乐生成 / 音频处理 / TTS** → 🔊 声音,即便是 GitHub TS repo
5. **PPT/幻灯片相关** → 🛠️ 办公与效率 / PPT 演示,即便是 web 生成器

# 输出严格 JSON (不要 markdown 代码块)
{{
  "verdict": "ok" | "swap_sub" | "swap_both",
  "cat": "最终一级 (必填,即使 verdict=ok 也填一审的)",
  "sub": "最终二级 (必填,二审必须给一个,不允许空)",
  "reason": "1 句话解释你的判断 (15-40 字)"
}}

verdict 含义:
- ok: 一审分类没问题
- swap_sub: 一级对,二级换一个
- swap_both: 一级和二级都换
"""

    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[audit_prompt],
        )
        raw = (resp.text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        audit = json.loads(raw)

        verdict = audit.get("verdict", "ok")
        cat2 = audit.get("cat", cat1)
        sub2 = audit.get("sub", sub1)
        reason = audit.get("reason", "")

        # 二级必须在 TAXONOMY[cat2] 下,否则视为乱编 → 强制 ok
        if cat2 not in TAXONOMY or sub2 not in TAXONOMY.get(cat2, []):
            log(f"  ⚠ 二审给的 ({cat2}/{sub2}) 非法,忽略,保留一审")
            verdict = "ok"
            cat2, sub2 = cat1, sub1 or (TAXONOMY[cat1][0] if TAXONOMY.get(cat1) else "")

        # 一审 sub 为空也算需要二审填上 (audit 必须给一个)
        if not sub1 and sub2:
            verdict = verdict if verdict != "ok" else "swap_sub"  # 一审没填,二审补上算修改

        # 应用二审决定
        if verdict != "ok":
            log(f"  🔍 二审 [{verdict}]: ({cat1}/{sub1 or '空'}) → ({cat2}/{sub2}) — {reason}")
            analysis["category"] = cat2
            analysis["subcategory"] = sub2
        else:
            log(f"  ✓ 二审 [ok]: 保留 ({cat1}/{sub1})")
            analysis["subcategory"] = sub1 or sub2  # 一审若 sub 空也用二审填

        # 写审计日志
        try:
            rec = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "url": url,
                "name": name,
                "headline": headline[:80],
                "first_pass": {"cat": cat1, "sub": sub1},
                "audit": {"verdict": verdict, "cat": cat2, "sub": sub2, "reason": reason},
            }
            with open(CLASSIFY_AUDIT_LOG, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            log(f"  ⚠ 二审日志写入失败: {e}")

    except Exception as e:
        log(f"  ⚠ 二审失败,保留一审 ({cat1}/{sub1}): {e}")

    return analysis

# ---------- Dedupe: find/archive existing page with same URL ----------
def find_existing_page(url):
    """Returns (page_ids, my_notes_text) — preserves the user's manual notes across re-captures."""
    r = requests.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json={"filter": {"property": "URL", "url": {"equals": url}}, "page_size": 5},
        timeout=30,
    )
    if r.status_code >= 300:
        return [], ""
    results = r.json().get("results", [])
    page_ids = [p["id"] for p in results]
    # 读最新一条的「My Notes」字段，保留下来
    my_notes = ""
    if results:
        notes_prop = results[0].get("properties", {}).get(NOTES_FIELD_NAME, {})
        rich = notes_prop.get("rich_text", []) or []
        my_notes = "".join(r.get("plain_text", "") for r in rich)
    return page_ids, my_notes

def archive_page(page_id):
    requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json={"archived": True},
        timeout=30,
    )

# ---------- Step 3: upload screenshot to Notion ----------
def upload_to_notion(image_bytes, filename="screenshot.jpg"):
    r = requests.post(
        "https://api.notion.com/v1/file_uploads",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json={"filename": filename, "content_type": "image/jpeg"},
        timeout=30,
    )
    r.raise_for_status()
    fu = r.json()
    fu_id = fu["id"]
    upload_url = fu["upload_url"]
    r2 = requests.post(
        upload_url,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
        },
        files={"file": (filename, image_bytes, "image/jpeg")},
        timeout=60,
    )
    r2.raise_for_status()
    return fu_id

# ---------- Step 4: create Notion page ----------
def create_page(url, analysis, screenshot_bytes, og_image_url, history, my_notes=""):
    cat = analysis.get("category", "🔬 其他")
    if cat not in CATEGORIES:
        cat = "🔬 其他"

    sub = (analysis.get("subcategory") or "").strip()
    if sub and sub not in TAXONOMY.get(cat, []):
        log(f"  ⚠ Gemini 给的 subcategory={sub!r} 不在 {cat} 下,丢弃")
        sub = ""

    headline = (analysis.get("headline") or "")[:300]
    name = (analysis.get("name") or urlparse(url).netloc)[:80]

    visit_count = history.get("visit_count", 0)
    status = visit_count_to_status(visit_count)

    capabilities = analysis.get("capabilities", []) or []
    scenarios = analysis.get("scenarios") or analysis.get("use_case_list") or []
    if not scenarios and analysis.get("use_case"):  # 兼容旧字段名
        scenarios = [analysis["use_case"]]
    tech_highlights = analysis.get("tech_highlights", []) or []
    intro = (analysis.get("intro") or "").strip()
    search_keywords = analysis.get("search_keywords", []) or []
    alts = analysis.get("alternatives", {}) or {}
    replaces = alts.get("replaces", []) or []
    similar = alts.get("similar_to", []) or []
    pairs = alts.get("pairs_with", []) or []
    alt_lines = []
    if replaces: alt_lines.append("替代: " + " / ".join(str(x) for x in replaces))
    if similar: alt_lines.append("类似: " + " / ".join(str(x) for x in similar))
    if pairs: alt_lines.append("搭配: " + " / ".join(str(x) for x in pairs))
    alt_text = "\n".join(alt_lines)
    keywords_text = " · ".join(str(k) for k in search_keywords)

    # GitHub 专属字段 (非 GitHub 工具留空)
    gh = analysis.get("github") or {}
    repo_type = gh.get("repo_type", "")
    language = gh.get("language", "")
    install_cmd = gh.get("install_cmd", "")
    # Stars 来自 capture 阶段 fetch_github_metadata 调用过的 cache
    # 但 analyze() 没把 gh_meta 返回出来,这里通过判断 URL 现拉一次
    stars_count = analysis.get("_stars", 0)

    props = {
        "Name": {"title": [{"text": {"content": name}}]},
        "URL": {"url": url},
        "Headline": {"rich_text": [{"text": {"content": headline}}]},
        "Category": {"select": {"name": cat}},
        # Subcategory 是 select 类型;空值用 None 让 Notion 留空,有值则用 {"name": sub}
        "Subcategory": {"select": ({"name": sub} if sub else None)},
        "Tags": {"multi_select": [{"name": str(t)[:40]} for t in analysis.get("tags", [])[:8]]},
        "Capabilities": {"rich_text": [{"text": {"content": "\n".join("• " + str(c) for c in capabilities)[:1900]}}]},
        "Use Case": {"rich_text": [{"text": {"content": " / ".join(str(s) for s in scenarios)[:500]}}]},
        "Search Keywords": {"rich_text": [{"text": {"content": keywords_text[:1900]}}]},
        "Alternatives": {"rich_text": [{"text": {"content": alt_text[:1900]}}]},
        "Status": {"select": {"name": status}},
        "Visit Count": {"number": visit_count},
        NOTES_FIELD_NAME: {"rich_text": [{"text": {"content": (my_notes or "")[:1900]}}]},
    }
    # GitHub 专属字段(只在是 GitHub repo 时写入)
    if repo_type:
        # 把字符串规范化以匹配 select 选项
        valid_repo_types = {"cli", "library", "app", "skill", "agent", "mcp", "dataset", "awesome 合集", "其他"}
        rt = repo_type.lower().strip()
        if rt not in valid_repo_types:
            rt = "其他"
        props["Repo Type"] = {"select": {"name": rt}}
    if language:
        valid_langs = {"Python", "TypeScript", "JavaScript", "Go", "Rust", "Shell", "Lua", "Swift", "C++", "Java", "Markdown", "HTML"}
        if language in valid_langs:
            props["Language"] = {"select": {"name": language}}
    if stars_count and stars_count > 0:
        props["Stars"] = {"number": int(stars_count)}
    if install_cmd:
        props["Install"] = {"rich_text": [{"text": {"content": install_cmd[:200]}}]}
    if history.get("last_visited"):
        props["Last Visited"] = {"date": {"start": history["last_visited"]}}

    cover = None
    cover_image_block = None
    # 同色画面再判一次: fetch_page 已经重抓过,这里是最后兜底——如果还是同色,优先用 og:image
    use_og_fallback = is_uniform_image(screenshot_bytes, threshold=0.85)
    if use_og_fallback:
        log(f"  ⚠ 最终 cover 仍是同色画面 (>85% 单色), 优先 og:image fallback")
        os.system(f'osascript -e \'display notification "{name[:40]} 截图全同色,已落 og:image,如不满意请手动改 cover" with title "🎨 cover 兜底" sound name "Glass"\'')

    if use_og_fallback and og_image_url and og_image_url.startswith("http"):
        cover = {"type": "external", "external": {"url": og_image_url}}
        cover_image_block = {
            "object": "block", "type": "image",
            "image": {"type": "external", "external": {"url": og_image_url}},
        }
        log("  ✓ 用 og:image 作 cover")
    else:
        try:
            fu_id = upload_to_notion(screenshot_bytes, f"{urlparse(url).netloc}.jpg")
            cover = {"type": "file_upload", "file_upload": {"id": fu_id}}
            cover_image_block = {
                "object": "block", "type": "image",
                "image": {"type": "file_upload", "file_upload": {"id": fu_id}},
            }
            log("  📤 截图已上传到 Notion")
        except Exception as e:
            log(f"  ⚠ 截图上传失败 ({e}), 用 og:image fallback")
            if og_image_url and og_image_url.startswith("http"):
                cover = {"type": "external", "external": {"url": og_image_url}}
                cover_image_block = {
                    "object": "block", "type": "image",
                    "image": {"type": "external", "external": {"url": og_image_url}},
                }

    children = []
    if cover_image_block:
        children.append(cover_image_block)
    children.append({"object": "block", "type": "heading_2",
                     "heading_2": {"rich_text": [{"text": {"content": "📌 " + headline}}]}})

    if intro:
        children.append({"object": "block", "type": "heading_3",
                         "heading_3": {"rich_text": [{"text": {"content": "📖 这是什么"}}]}})
        # 长 intro 拆段（按 \n\n 拆）
        for para in intro.split("\n\n"):
            para = para.strip()
            if para:
                children.append({"object": "block", "type": "paragraph",
                                 "paragraph": {"rich_text": [{"text": {"content": para[:1900]}}]}})

    if capabilities:
        children.append({"object": "block", "type": "heading_3",
                         "heading_3": {"rich_text": [{"text": {"content": "⚡ 能做什么"}}]}})
        for cap in capabilities:
            children.append({"object": "block", "type": "bulleted_list_item",
                             "bulleted_list_item": {"rich_text": [{"text": {"content": str(cap)}}]}})

    if scenarios:
        children.append({"object": "block", "type": "heading_3",
                         "heading_3": {"rich_text": [{"text": {"content": "💡 适合谁/什么场景用"}}]}})
        for s in scenarios:
            children.append({"object": "block", "type": "bulleted_list_item",
                             "bulleted_list_item": {"rich_text": [{"text": {"content": str(s)}}]}})

    if tech_highlights:
        children.append({"object": "block", "type": "heading_3",
                         "heading_3": {"rich_text": [{"text": {"content": "🛠️ 技术亮点"}}]}})
        for t in tech_highlights:
            children.append({"object": "block", "type": "bulleted_list_item",
                             "bulleted_list_item": {"rich_text": [{"text": {"content": str(t)}}]}})

    children.append({"object": "block", "type": "heading_3",
                     "heading_3": {"rich_text": [{"text": {"content": "🔗 链接"}}]}})
    children.append({"object": "block", "type": "bookmark",
                     "bookmark": {"url": url}})

    # My Notes区块（在正文里也展示，方便阅读）
    children.append({"object": "block", "type": "heading_3",
                     "heading_3": {"rich_text": [{"text": {"content": "📝 My Notes"}}]}})
    if my_notes:
        children.append({"object": "block", "type": "paragraph",
                         "paragraph": {"rich_text": [{"text": {"content": my_notes}}]}})
    else:
        children.append({"object": "block", "type": "callout",
                         "callout": {
                             "rich_text": [{"text": {"content": "（在这里写下你的使用感受、场景、想法 —— AI 搜索时也会用到这里的内容）"}}],
                             "icon": {"emoji": "✏️"},
                         }})

    body = {
        "parent": {"database_id": DATABASE_ID},
        "properties": props,
        "children": children,
    }
    if cover:
        body["cover"] = cover

    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
    if r.status_code >= 300:
        raise Exception(f"Notion {r.status_code}: {r.text[:400]}")
    page = r.json()

    # 把截图同步到 tools-wall/public/covers/{id}.jpg
    # 防 Notion S3 cover URL 1h 过期导致前端空白
    # 同色画面不写, 避免覆盖之前的好 cover
    # Skip 如果用户没配 TOOLS_WALL_DIR (前端是可选组件)
    if TOOLS_WALL_DIR:
        try:
            if use_og_fallback:
                log(f"  ⏭ 截图是同色,不覆盖 public/covers/ 原有 cover")
            elif screenshot_bytes and len(screenshot_bytes) > 1000:
                covers_dir = TOOLS_WALL_DIR / "public" / "covers"
                covers_dir.mkdir(parents=True, exist_ok=True)
                pid = page["id"].replace("-", "")
                (covers_dir / f"{pid}.jpg").write_bytes(screenshot_bytes)
                log(f"  💾 截图同步到 {covers_dir.name}/{pid}.jpg")
        except Exception as e:
            log(f"  ⚠ 同步 public/covers 失败: {e}")

    return page

# ---------- Target: Obsidian ----------
def safe_filename(s, maxlen=80):
    s = re.sub(r'[/\\:*?"<>|]', '_', str(s)).strip().strip('.')
    return s[:maxlen] or "untitled"

def extract_my_notes_from_md(md_path):
    """从已存在的 .md 提取『## 📝 My Notes』区块的内容(下次重写时保留)。"""
    if not md_path.exists():
        return ""
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    # 找到 "## 📝 My Notes" 之后到下一个 "## " 或文件结尾
    m = re.search(r"##\s*📝\s*My Notes\s*\n(.*?)(?=\n##\s|\Z)", text, re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()

def write_obsidian(url, analysis, screenshot_bytes, history, my_notes=""):
    """Write markdown + attached screenshot to Obsidian vault.
    No-op if OBSIDIAN_VAULT not configured in .env."""
    if not OBSIDIAN_VAULT or not OBSIDIAN_DIR:
        return None
    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)
    OBSIDIAN_ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

    name = analysis.get("name") or urlparse(url).netloc
    headline = analysis.get("headline", "")
    intro = (analysis.get("intro") or "").strip()
    cat = analysis.get("category", "🔬 其他")
    tags = analysis.get("tags", []) or []
    capabilities = analysis.get("capabilities", []) or []
    scenarios = analysis.get("scenarios") or analysis.get("use_case_list") or []
    if not scenarios and analysis.get("use_case"):
        scenarios = [analysis["use_case"]]
    tech_highlights = analysis.get("tech_highlights", []) or []
    search_keywords = analysis.get("search_keywords", []) or []
    alts = analysis.get("alternatives", {}) or {}
    visit_count = history.get("visit_count", 0)
    status = visit_count_to_status(visit_count)
    last_visited = history.get("last_visited") or ""
    today = datetime.now().date().isoformat()
    domain = urlparse(url).netloc

    img_filename = f"{safe_filename(name)}_{domain}.jpg"
    img_path = OBSIDIAN_ATTACHMENTS_DIR / img_filename
    img_path.write_bytes(screenshot_bytes)

    yaml_tags = ", ".join(f'"{str(t)}"' for t in tags)
    yaml_keywords = ", ".join(f'"{str(k)}"' for k in search_keywords)
    lines = [
        "---",
        f"url: {url}",
        f"category: {cat}",
        f"tags: [{yaml_tags}]",
        f"search_keywords: [{yaml_keywords}]",
        f"visit_count: {visit_count}",
        f"status: {status}",
        f"last_visited: {last_visited}",
        f"added: {today}",
        "---",
        "",
        f"# {name}",
        "",
        f"> {headline}",
        "",
        f"![](attachments/{img_filename})",
        "",
    ]
    if intro:
        lines += ["## 📖 这是什么", "", intro, ""]
    if capabilities:
        lines += ["## ⚡ 能做什么", ""]
        lines += [f"- {c}" for c in capabilities]
        lines.append("")
    if scenarios:
        lines += ["## 💡 适合谁/什么场景用", ""]
        lines += [f"- {s}" for s in scenarios]
        lines.append("")
    if tech_highlights:
        lines += ["## 🛠️ 技术亮点", ""]
        lines += [f"- {t}" for t in tech_highlights]
        lines.append("")

    # 替代关系 (graph-aware retrieval)
    if alts.get("replaces") or alts.get("similar_to") or alts.get("pairs_with"):
        lines += ["## 🔗 替代与关联", ""]
        if alts.get("replaces"):
            lines.append(f"- **替代**: {' / '.join(str(x) for x in alts['replaces'])}")
        if alts.get("similar_to"):
            lines.append(f"- **类似**: {' / '.join(str(x) for x in alts['similar_to'])}")
        if alts.get("pairs_with"):
            lines.append(f"- **搭配**: {' / '.join(str(x) for x in alts['pairs_with'])}")
        lines.append("")

    md_filename = f"{safe_filename(name)}.md"
    md_path = OBSIDIAN_DIR / md_filename

    # 保留你之前在文件里写过的"My Notes"——优先用这个,其次用 Notion 拿过来的
    existing_notes = extract_my_notes_from_md(md_path)
    final_notes = existing_notes or my_notes

    lines += ["## 📝 My Notes", ""]
    if final_notes:
        lines.append(final_notes)
    else:
        lines.append("> 在这里写下你的使用感受、场景、想法 —— 文件保存后下次重新抓取也不会被覆盖。")
    lines.append("")
    lines += ["## 🔗 链接", f"[{url}]({url})", ""]

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return str(md_path)

# ---------- Target: Feishu (via lark-cli) ----------
import subprocess

# FEISHU_AS / LARK_CLI / FEISHU_BASE_TOKEN / FEISHU_TABLE_ID are loaded from .env at top of file.
# If you use nvm and hit a brew-node simdjson dyld conflict, set NVM_NODE_BIN in .env.
NVM_NODE_BIN = ENV.get("NVM_NODE_BIN", "")

def lark_env():
    """Ensure nvm node is found first in PATH (avoids brew node + simdjson dyld conflict)."""
    env = os.environ.copy()
    if NVM_NODE_BIN:
        env["PATH"] = NVM_NODE_BIN + ":" + env.get("PATH", "")
    return env

def feishu_token_status():
    """Returns (state, msg) — state ∈ {ok, near_expiry, expired, unknown}."""
    try:
        r = subprocess.run(
            [LARK_CLI, "auth", "status"],
            capture_output=True, text=True, timeout=10, env=lark_env(),
        )
        if r.returncode != 0 or not r.stdout:
            return ("unknown", "auth status 调用失败")
        d = json.loads(r.stdout)
        if d.get("tokenStatus") != "valid":
            return ("expired", "token 已失效")
        exp = datetime.fromisoformat(d["expiresAt"])
        ref = datetime.fromisoformat(d["refreshExpiresAt"])
        now = datetime.now(exp.tzinfo)
        access_min = int((exp - now).total_seconds() / 60)
        ref_hours = int((ref - now).total_seconds() / 3600)
        if access_min < 0:
            return ("expired", f"access token 已过期 {-access_min} 分钟")
        if access_min < 15:
            return ("near_expiry", f"access {access_min}min, refresh {ref_hours}h")
        return ("ok", f"access {access_min}min, refresh {ref_hours}h")
    except Exception as e:
        return ("unknown", str(e))

def feishu_record_exists(url):
    """Find existing record by URL field."""
    cmd = [
        LARK_CLI, "base", "+record-list",
        "--base-token", FEISHU_BASE_TOKEN,
        "--table-id", FEISHU_TABLE_ID,
        "--as", FEISHU_AS,
        "--filter", json.dumps({"conjunction":"and","conditions":[{"field_name":"URL","operator":"is","value":[url]}]}),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=lark_env())
        if r.returncode != 0:
            return []
        data = json.loads(r.stdout)
        if not data.get("ok"):
            return []
        return [it["record_id"] for it in data.get("data", {}).get("items", []) if it.get("record_id")]
    except Exception:
        return []

def feishu_delete_records(record_ids):
    for rid in record_ids:
        try:
            subprocess.run([
                LARK_CLI, "base", "+record-delete",
                "--base-token", FEISHU_BASE_TOKEN,
                "--table-id", FEISHU_TABLE_ID,
                "--record-id", rid,
                "--as", FEISHU_AS,
            ], capture_output=True, timeout=15, env=lark_env())
        except Exception:
            pass

def write_feishu(url, analysis, screenshot_bytes, history):
    """Write a record to Feishu bitable via lark-cli."""
    # Token health check
    state, msg = feishu_token_status()
    if state == "expired":
        notify("⚠ 飞书 token 过期", "终端跑: lark-cli auth login")
        raise Exception(f"飞书 token 过期: {msg}。请跑 `lark-cli auth login` 续期。")
    if state == "near_expiry":
        log(f"  ⚠ 飞书 token 临过期: {msg}")
        notify("⚠ 飞书 token 临过期", "建议尽快跑 lark-cli auth login")

    name = analysis.get("name") or urlparse(url).netloc
    headline = analysis.get("headline", "")
    cat = analysis.get("category", "🔬 其他")
    tags = analysis.get("tags", []) or []
    capabilities = analysis.get("capabilities", []) or []
    scenarios = analysis.get("scenarios") or []
    if not scenarios and analysis.get("use_case"):
        scenarios = [analysis["use_case"]]
    visit_count = history.get("visit_count", 0)
    status = visit_count_to_status(visit_count)

    # Last Visited as ms epoch (Feishu date field)
    last_visited_ms = None
    if history.get("last_visited"):
        try:
            dt = datetime.fromisoformat(history["last_visited"])
            last_visited_ms = int(dt.timestamp() * 1000)
        except Exception:
            pass

    use_case_str = " / ".join(str(s) for s in scenarios) if scenarios else ""

    intro = (analysis.get("intro") or "").strip()
    tech_highlights = analysis.get("tech_highlights", []) or []

    fields = {
        "Name": name[:100],
        "Headline": headline[:500],
        "Intro": intro[:1900],
        "URL": url,
        "Category": cat,
        "Tags": [str(t)[:40] for t in tags[:10]],
        "Capabilities": "\n".join("• " + str(c) for c in capabilities)[:1900],
        "Use Case": use_case_str[:500],
        "Tech Highlights": "\n".join("• " + str(t) for t in tech_highlights)[:1900],
        "Status": status,
        "Visit Count": visit_count,
    }
    if last_visited_ms:
        fields["Last Visited"] = last_visited_ms

    # Dedupe: delete previous records with same URL
    existing = feishu_record_exists(url)
    if existing:
        feishu_delete_records(existing)

    # Create record (Feishu API expects raw Map<field_name, value>, NOT wrapped in 'fields')
    cmd = [
        LARK_CLI, "base", "+record-upsert",
        "--base-token", FEISHU_BASE_TOKEN,
        "--table-id", FEISHU_TABLE_ID,
        "--as", FEISHU_AS,
        "--json", json.dumps(fields, ensure_ascii=False),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=lark_env())
    if r.returncode != 0 or not r.stdout:
        raise Exception(f"lark-cli failed: rc={r.returncode}, stderr={r.stderr[:300]}")
    data = json.loads(r.stdout)
    if not data.get("ok"):
        raise Exception(f"lark API error: {json.dumps(data, ensure_ascii=False)[:300]}")
    rec = data.get("data", {}).get("record", {})
    record_id = (
        (rec.get("record_id_list") or [None])[0]
        or rec.get("record_id")
        or data.get("data", {}).get("record_id")
    )

    # Upload screenshot to Screenshot field (lark-cli requires relative path within cwd)
    if record_id and screenshot_bytes:
        tmp_dir = Path("/tmp")
        tmp_name = f"feishu_screenshot_{record_id}.jpg"
        tmp = tmp_dir / tmp_name
        tmp.write_bytes(screenshot_bytes)
        try:
            res = subprocess.run([
                LARK_CLI, "base", "+record-upload-attachment",
                "--base-token", FEISHU_BASE_TOKEN,
                "--table-id", FEISHU_TABLE_ID,
                "--record-id", record_id,
                "--field-id", "Screenshot",
                "--file", tmp_name,
                "--as", FEISHU_AS,
            ], capture_output=True, text=True, timeout=60, cwd=str(tmp_dir), env=lark_env())
            ok = False
            try:
                ok = res.returncode == 0 and json.loads(res.stdout).get("ok") is True
            except Exception:
                pass
            if ok:
                log("  📤 截图已上传到飞书")
            else:
                log(f"  ⚠ 飞书截图上传失败: {(res.stdout or res.stderr)[:200]}")
        except Exception as e:
            log(f"  ⚠ 飞书截图上传异常: {e}")
        finally:
            try: tmp.unlink()
            except Exception: pass

    return record_id

# ---------- Main ----------
def main():
    if len(sys.argv) < 2:
        print("Usage: capture.py <URL>")
        sys.exit(1)
    url = sys.argv[1].strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url

    log(f"📸 抓取 {url}")

    try:
        pd = fetch_page(url)
    except Exception as e:
        log(f"❌ 抓取失败: {e}")
        notify("❌ 收藏失败", f"抓取失败: {str(e)[:100]}")
        sys.exit(2)

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    domain = urlparse(url).netloc.replace(".", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = SCREENSHOT_DIR / f"{ts}_{domain}.jpg"
    archive.write_bytes(pd["full_screenshot"])
    log(f"💾 截图存档: {archive}")

    log("🧠 Gemini 分析...")
    try:
        analysis = analyze(url, pd)
    except Exception as e:
        log(f"❌ AI 分析失败: {e}")
        notify("❌ 收藏失败", f"AI 分析失败: {str(e)[:100]}")
        sys.exit(3)
    _subcat = analysis.get('subcategory') or '—'
    log(f"  → {analysis.get('name')} | {analysis.get('category')} / {_subcat} | {analysis.get('tags')}")

    domain = urlparse(url).netloc
    history = query_browser_history(domain)
    log(f"📊 浏览器历史: {history['visit_count']} 次访问 ({history.get('breakdown') or '无记录'}), 最近 {history.get('last_visited') or 'N/A'} → 状态: {visit_count_to_status(history['visit_count'])}")

    # ---------- 多目的地并行写入 ----------
    results = {}

    # Notion
    preserved_notes = ""
    try:
        existing, preserved_notes = find_existing_page(url)
        if existing:
            log(f"♻ Notion 发现 {len(existing)} 条旧记录, 归档" + (f" (保留My Notes {len(preserved_notes)} 字)" if preserved_notes else ""))
            for pid in existing:
                archive_page(pid)
        log("📝 写入 Notion...")
        notion_result = create_page(url, analysis, pd["screenshot"], pd.get("og_image"), history, my_notes=preserved_notes)
        results["Notion"] = notion_result.get("url", "")
        log(f"  ✅ Notion: {results['Notion']}")
        # B 任务: 记录初始分类,供 feedback_collector 后续对比手工修改
        append_classify_log(
            page_id=notion_result.get("id", ""),
            url=url,
            name=analysis.get("name", ""),
            headline=analysis.get("headline", ""),
            cat=analysis.get("category", ""),
            sub=analysis.get("subcategory", "") or "",
        )
        # 综合介绍 (URL Context → Playwright fallback)
        try:
            from backfill_intros import generate as gen_intro, write_intro
            log("📖 生成综合介绍 (URL Context tool)...")
            intro_text, source = gen_intro(url)
            if intro_text and len(intro_text) > 100:
                write_intro(notion_result["id"], intro_text)
                log(f"  ✅ 网站介绍 via {source} ({len(intro_text)} 字)")
            else:
                log(f"  ⚠ 综合介绍生成失败/太短, 跳过 (source={source})")
        except Exception as e:
            log(f"  ⚠ 综合介绍写入失败: {e}")
    except Exception as e:
        results["Notion"] = f"FAIL: {e}"
        log(f"  ❌ Notion 失败: {e}")

    # Obsidian (optional sync target)
    if OBSIDIAN_VAULT:
        try:
            log("📝 写入 Obsidian...")
            md_path = write_obsidian(url, analysis, pd["screenshot"], history, my_notes=preserved_notes)
            results["Obsidian"] = md_path
            log(f"  ✅ Obsidian: {md_path}")
        except Exception as e:
            results["Obsidian"] = f"FAIL: {e}"
            log(f"  ❌ Obsidian 失败: {e}")

    # 飞书（开关控制；关闭时跳过，以后 bulk-sync 一次性回灌）
    if ENABLE_FEISHU:
        try:
            log("📝 写入飞书...")
            rid = write_feishu(url, analysis, pd["screenshot"], history)
            results["Feishu"] = rid
            log(f"  ✅ 飞书: record_id={rid}")
        except Exception as e:
            results["Feishu"] = f"FAIL: {str(e)[:200]}"
            log(f"  ⚠ 飞书写入失败: {str(e)[:200]}")
    else:
        log("⏭  飞书写入已关闭 (ENABLE_FEISHU=False)")

    # 推送刷新 Vercel 卡片墙 (Notion 写成功才有意义；前端是可选组件)
    if TOOLS_WALL_URL and WALL_REVALIDATE_SECRET and not str(results.get("Notion", "FAIL")).startswith("FAIL"):
        try:
            r = requests.post(
                f"{TOOLS_WALL_URL}/api/revalidate",
                params={"secret": WALL_REVALIDATE_SECRET},
                timeout=10,
            )
            if r.status_code == 200:
                log(f"  🌐 卡片墙已刷新 ({TOOLS_WALL_URL})")
                results["Wall"] = "revalidated"
            else:
                log(f"  ⚠ 卡片墙刷新失败 {r.status_code}: {r.text[:100]}")
        except Exception as e:
            log(f"  ⚠ 卡片墙刷新异常: {e}")

    # 自动部署到 Vercel — 把新写的 public/covers/{id}.jpg 上传到 CDN
    # 不等结果 (Popen),后台跑;Vercel 增量上传只传新图
    # Skip 如果用户没配 TOOLS_WALL_DIR
    if TOOLS_WALL_DIR and not str(results.get("Notion", "FAIL")).startswith("FAIL"):
        try:
            vercel_bin = shutil.which("vercel")
            if not vercel_bin:
                raise RuntimeError("找不到 vercel CLI (npm i -g vercel)")

            wall_dir = str(TOOLS_WALL_DIR)
            deploy_log = LOG_DIR / "deploy.log"
            deploy_env = {**os.environ}
            deploy_env.pop("SSL_CERT_FILE", None)  # 避免 stale cert 坑
            # 把 vercel CLI 所在 dir 加进 PATH,launchd 默认 PATH 太简陋会让 vercel 内部 spawn node 失败
            extra_path = os.path.dirname(vercel_bin)
            deploy_env["PATH"] = extra_path + ":" + deploy_env.get("PATH", "")

            cmd = [vercel_bin, "--prod", "--yes"]
            if VERCEL_TOKEN:
                cmd += ["--token", VERCEL_TOKEN]

            with open(deploy_log, "a") as f:
                f.write(f"\n=== {datetime.now().isoformat()} deploy for {url} ===\n")
                proc = subprocess.Popen(
                    cmd,
                    cwd=wall_dir,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=deploy_env,
                )
            # 快速失败检测: token/auth 错误几乎瞬间返回。正常 deploy 30-50s
            # 不阻塞,把 monitor 单开一个守护进程,主 capture 立即返回
            monitor_script = (
                "import sys,time,subprocess,os\n"
                "pid=int(sys.argv[1]); log_path=sys.argv[2]; src=sys.argv[3]\n"
                "deadline=time.time()+8\n"
                "while time.time()<deadline:\n"
                "    try: os.kill(pid,0)\n"
                "    except ProcessLookupError:\n"
                "        # 已退出 (要么超快成功,要么立即失败)\n"
                "        tail=open(log_path).read()[-3000:]\n"
                "        if 'token is not valid' in tail or 'Authentication' in tail or 'Error: ' in tail:\n"
                "            msg='Deploy failed: Vercel token invalid or auth error'\n"
                "            os.system('osascript -e \\'display notification \"'+msg+'\" with title \"🚨 capture deploy\" sound name \"Sosumi\"\\'')\n"
                "        break\n"
                "    time.sleep(0.5)\n"
            )
            subprocess.Popen(
                [sys.executable, '-c', monitor_script, str(proc.pid), str(deploy_log), url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log(f"  🚀 后台 deploy 已启动 (~30s 完成 → 工具卡片图自动出现; 8s 内挂会发通知)")
            results["Deploy"] = "spawned"
        except Exception as e:
            log(f"  ⚠ 启动 deploy 失败: {e}")
            os.system(f'osascript -e \'display notification "启动 vercel deploy 出错: {str(e)[:80]}" with title "🚨 capture deploy" sound name "Sosumi"\'')

    # Summary
    success = [k for k, v in results.items() if not str(v).startswith("FAIL")]
    failed = [k for k, v in results.items() if str(v).startswith("FAIL")]
    log(f"完成: ✓{','.join(success) or 'none'}  ✗{','.join(failed) or 'none'}")

    summary_msg = f"{', '.join(success)} 成功"
    if failed:
        summary_msg += f" / {', '.join(failed)} 失败"
    notify(f"✓ 已收藏: {analysis.get('name')}", summary_msg)
    print(json.dumps(results, ensure_ascii=False))

if __name__ == "__main__":
    main()
