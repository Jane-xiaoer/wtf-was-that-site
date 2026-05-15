#!/usr/bin/env python3
"""Backfill 网站介绍 (long-form intro) for all sites in Notion.

Strategy:
1. URL Context tool first (Gemini self-fetches → deeper structured access)
2. If URL retrieval fails or output too short → Playwright fallback (real browser + screenshot)
3. Write the markdown intro into Notion '网站介绍' rich_text property
4. Skip pages that already have a non-empty 网站介绍 (idempotent)

Run:
    unset SSL_CERT_FILE
    python3 backfill_intros.py
"""
import json
import os
import ssl
import sys
import time
import traceback
from pathlib import Path

# load project-local .env
PROJECT_ROOT = Path(__file__).parent
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# import capture.py's fetch_page (for Playwright fallback)
sys.path.insert(0, str(PROJECT_ROOT))
from capture import fetch_page  # type: ignore

from google import genai
from google.genai import types

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
DB_ID = os.environ.get("NOTION_DB_ID", "")
if not (NOTION_TOKEN and GEMINI_KEY and DB_ID):
    raise SystemExit("❌ .env 缺必需字段: NOTION_TOKEN / GEMINI_API_KEY / NOTION_DB_ID")

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

client = genai.Client(api_key=GEMINI_KEY)


# ─── Notion helpers ───────────────────────────────────────────────────
def notion_request(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib_request(f"https://api.notion.com/v1{path}", method, body)
    return json.loads(_urlopen(req).read())


def urllib_request(url: str, method: str, body: dict | None):
    import urllib.request as u
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    return u.Request(url, method=method, data=data, headers=headers)


def _urlopen(req):
    """Open URL with auto-retry on transient SSL/network errors."""
    import urllib.request as u
    last = None
    for attempt in range(4):
        try:
            return u.urlopen(req, context=SSL_CTX, timeout=30)
        except Exception as e:
            last = e
            msg = str(e)
            # retry on SSL EOF / connection reset / timeout
            if any(s in msg for s in ["UNEXPECTED_EOF", "Connection reset", "timed out", "EOF occurred"]):
                time.sleep(2 + attempt * 2)
                continue
            raise
    raise last  # type: ignore


def query_all_sites() -> list[dict]:
    """Return list of {id, name, url, has_intro} for all non-archived pages."""
    out = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion_request("POST", f"/databases/{DB_ID}/query", body)
        for p in data.get("results", []):
            if p.get("archived"):
                continue
            props = p.get("properties", {})

            def _t(prop_obj):
                return "".join(
                    t.get("plain_text", "") for t in (prop_obj.get("rich_text") or [])
                )

            name = "".join(t.get("plain_text", "") for t in (props.get("Name", {}).get("title") or []))
            url = props.get("URL", {}).get("url", "")
            intro = _t(props.get("网站介绍", {}))
            out.append({
                "id": p["id"],
                "name": name,
                "url": url,
                "has_intro": bool(intro and len(intro) > 50),
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def write_intro(page_id: str, intro_md: str):
    """Set Notion 网站介绍 property AND append rendered blocks to page body."""
    # 1) property (used by frontend chat-card 详情 modal)
    chunks = []
    s = intro_md
    while s:
        chunks.append(s[:1900])
        s = s[1900:]
    rt = [{"type": "text", "text": {"content": c}} for c in chunks] if chunks else [{"type": "text", "text": {"content": ""}}]
    body = {"properties": {"网站介绍": {"rich_text": rt}}}
    notion_request("PATCH", f"/pages/{page_id}", body)
    # 2) page body — append at end so users can read it inside Notion too
    blocks = md_to_notion_blocks(intro_md)
    if blocks:
        # archive any old "📖 网站综合介绍" + its descendants from previous runs (idempotent)
        try:
            existing = notion_request("GET", f"/blocks/{page_id}/children?page_size=100")
            for b in existing.get("results", []):
                if b.get("type") == "heading_3":
                    txt = "".join(t.get("plain_text", "") for t in (b["heading_3"].get("rich_text") or []))
                    if "网站综合介绍" in txt or "AI 综合介绍" in txt:
                        notion_request("DELETE", f"/blocks/{b['id']}")
        except Exception:
            pass
        # append heading + intro blocks
        wrapped = [
            {"object": "block", "type": "heading_3",
             "heading_3": {"rich_text": [{"text": {"content": "📖 网站综合介绍 (AI 生成)"}}]}},
            *blocks,
        ]
        # Notion accepts up to 100 children per call
        notion_request("PATCH", f"/blocks/{page_id}/children", {"children": wrapped[:100]})


def _parse_inline(s: str) -> list[dict]:
    """Parse **bold** and [text](url) into rich_text array."""
    rich = []
    i = 0
    pat_bold = "**"
    pat_link = ("[", "](")
    while i < len(s):
        # bold
        if s.startswith(pat_bold, i):
            end = s.find(pat_bold, i + 2)
            if end > i:
                rich.append({"type": "text", "text": {"content": s[i + 2:end]}, "annotations": {"bold": True}})
                i = end + 2
                continue
        # link [text](url)
        if s[i] == "[":
            close = s.find("]", i + 1)
            if close > i and s[close + 1:close + 2] == "(":
                url_end = s.find(")", close + 2)
                if url_end > close:
                    text = s[i + 1:close]
                    url = s[close + 2:url_end]
                    rich.append({"type": "text", "text": {"content": text, "link": {"url": url}}})
                    i = url_end + 1
                    continue
        # plain char accumulator
        # find next special char
        next_special = len(s)
        for token in ["**", "["]:
            j = s.find(token, i)
            if 0 <= j < next_special:
                next_special = j
        if next_special > i:
            rich.append({"type": "text", "text": {"content": s[i:next_special]}})
            i = next_special
        else:
            rich.append({"type": "text", "text": {"content": s[i]}})
            i += 1
    return rich or [{"type": "text", "text": {"content": ""}}]


def md_to_notion_blocks(md: str) -> list[dict]:
    """Convert our intro Markdown to Notion blocks (h3, paragraph, bullets)."""
    blocks: list[dict] = []
    for line in md.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        if line.startswith("### "):
            blocks.append({
                "object": "block",
                "type": "heading_3",
                "heading_3": {"rich_text": _parse_inline(line[4:])},
            })
        elif line.lstrip().startswith(("* ", "- ", "• ")):
            txt = line.lstrip()[2:]
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _parse_inline(txt)},
            })
        else:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _parse_inline(line)},
            })
    return blocks


# ─── Gemini intro generation ──────────────────────────────────────────
INTRO_PROMPT_TEMPLATE = """请基于真实页面内容,用 Markdown 给我介绍这是个什么网站。

格式必须是:

这是一个名为 **[网站名]({url})** 的 [简短定位 = X 库 / 平台 / 工具]。

简单来说,它是为 **[用户群]** 准备的"**[一个有记忆点的比喻或绰号]**"。以下是它的核心功能点:

### 1. [小节名]

[1 句话简介]

* **[关键词]**:[简短解释,带页面里实际出现的产品名/数字/品牌名]
* **[关键词]**:[简短解释]
* **[关键词]**:[简短解释]

### 2. [小节名]

[1 句话简介。如果是网站独门特色,加 "这是该网站的一大特色。"]

* **[关键词]**:[解释]
* **[关键词]**:[解释]

### 3. [小节名]

[简介]

* **[关键词]**:[解释]

**一句话总结：** 这是一个让你 [动作 1],并能 [动作 2] 的工具网站。

—— 严格要求 ——
1. 绝不用这些词:致力于、高效、专业、强大、全面、为您、帮您、宝库、海洋、领先、卓越
2. 多举具体名字 (品牌/产品/数字),少抽象形容
3. 单条 bullet 严格 15-30 字
4. 整篇 250-380 字
5. 中文输出,文末不加来源链接
6. 没把握的细节宁可不写,也不许编
"""


def gen_via_url_context(url: str) -> tuple[str, str]:
    """Stage 1: let Gemini fetch the URL itself. Auto-retry on 503."""
    prompt = f"先访问 {url}, 然后基于真实页面内容写。\n\n" + INTRO_PROMPT_TEMPLATE.format(url=url)
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(url_context=types.UrlContext())],
                ),
            )
            text = (resp.text or "").strip()
            status = "UNKNOWN"
            try:
                meta = resp.candidates[0].url_context_metadata
                if meta and meta.url_metadata:
                    status = str(meta.url_metadata[0].url_retrieval_status).split(".")[-1]
            except Exception:
                pass
            return text, status
        except Exception as e:
            last_err = e
            msg = str(e)
            if "503" in msg or "UNAVAILABLE" in msg or "RESOURCE_EXHAUSTED" in msg:
                wait = 5 * (2 ** attempt)  # 5s, 10s, 20s, 40s
                print(f"   ⏳ Gemini 503, retry in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    if last_err:
        raise last_err
    return "", "FAIL"


def gen_via_playwright(url: str) -> tuple[str, str]:
    """Stage 2: fetch with Playwright + screenshot, feed to Gemini. Retry on 503."""
    pd = fetch_page(url)
    img_part = types.Part.from_bytes(data=pd["screenshot"], mime_type="image/jpeg")
    full = INTRO_PROMPT_TEMPLATE.format(url=url) + f"""

—— 真实页面内容 ——
URL: {url}
页面标题: {pd['title']}
og:description: {pd['og_desc']}
页面文字 (节选):
{pd['page_text']}
"""
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[img_part, full],
            )
            return (resp.text or "").strip(), "PLAYWRIGHT"
        except Exception as e:
            last_err = e
            msg = str(e)
            if "503" in msg or "UNAVAILABLE" in msg or "RESOURCE_EXHAUSTED" in msg:
                wait = 5 * (2 ** attempt)
                print(f"   ⏳ Gemini 503 (Playwright path), retry in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    if last_err:
        raise last_err
    return "", "FAIL"


FAIL_SIGNALS = ["URL_RETRIEVAL_STATUS_ERROR", "URL_RETRIEVAL_STATUS_UNSAFE"]
FAIL_PHRASES = ["无法访问", "cannot access", "unable to access", "I'm sorry", "I cannot", "无法获取"]


def looks_failed(text: str, status: str) -> bool:
    if status in FAIL_SIGNALS:
        return True
    if len(text) < 200:
        return True
    if any(s in text for s in FAIL_PHRASES):
        return True
    # missing the expected structure
    if "###" not in text or "一句话总结" not in text:
        return True
    return False


def generate(url: str) -> tuple[str, str]:
    """Hybrid generator. Returns (intro_text, source_label)."""
    try:
        text, status = gen_via_url_context(url)
        if not looks_failed(text, status):
            return text, "URL_CONTEXT"
    except Exception as e:
        print(f"  ⚠ URL Context error: {e}", file=sys.stderr)
    # fallback
    try:
        return gen_via_playwright(url)
    except Exception as e:
        print(f"  ✗ Playwright fallback failed: {e}", file=sys.stderr)
        return "", "FAILED"


# ─── Main ─────────────────────────────────────────────────────────────
def main():
    sites = query_all_sites()
    print(f"📚 total {len(sites)} sites in DB")
    todo = [s for s in sites if not s["has_intro"]]
    print(f"   {len(sites) - len(todo)} already have intro · {len(todo)} to process\n")

    ok = 0
    failed = 0
    via_url = 0
    via_pw = 0

    for i, s in enumerate(todo, start=1):
        print(f"[{i:3d}/{len(todo)}] {s['name'][:35]:35s} | {s['url'][:55]}")
        if not s["url"] or not s["url"].startswith(("http://", "https://")):
            print("   skip (no url)")
            failed += 1
            continue
        try:
            text, source = generate(s["url"])
            if not text:
                failed += 1
                print(f"   ✗ no text generated")
                continue
            write_intro(s["id"], text)
            if source == "URL_CONTEXT":
                via_url += 1
            elif source == "PLAYWRIGHT":
                via_pw += 1
            ok += 1
            print(f"   ✓ via {source} ({len(text)} chars)")
        except Exception as e:
            failed += 1
            print(f"   ✗ {e}")
            traceback.print_exc(file=sys.stderr)
        # gentle pacing — avoid Gemini rate limit (5s default, ramps if 503 hit)
        time.sleep(5)

    print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"✓ ok: {ok} (URL_CONTEXT: {via_url} · PLAYWRIGHT: {via_pw})  ✗ fail: {failed}")


if __name__ == "__main__":
    main()
