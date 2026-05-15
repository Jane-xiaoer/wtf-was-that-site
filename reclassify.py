"""
两层分类重做: Gemini 批量重新给所有工具打 category + subcategory
- 顺手校正一级分错的 (PageOn.AI 该在 PPT 不该在视觉创作 之类)
- subcategory 写到 Notion 的 rich_text 字段「Subcategory」
"""
import os, sys, json, time, requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ.pop("SSL_CERT_FILE", None)

# load project-local .env
PROJECT_ROOT = Path(__file__).parent
env_file = PROJECT_ROOT / ".env"
ENV_KV = {}
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            ENV_KV[k.strip()] = v.strip().strip('"').strip("'")

NOTION_TOKEN = ENV_KV.get("NOTION_TOKEN", "")
GEMINI_KEY = ENV_KV.get("GEMINI_API_KEY", "")
DB_ID = ENV_KV.get("NOTION_DB_ID", "")
if not (NOTION_TOKEN and GEMINI_KEY and DB_ID):
    raise SystemExit("❌ .env 缺必需字段: NOTION_TOKEN / GEMINI_API_KEY / NOTION_DB_ID")

H = {"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}

# === 两层体系 ===
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

CLASSIFY_PROMPT = """你是工具分类专家。给出工具描述,你必须从下面的两层分类里选一个最匹配的。

# 一级 + 二级 分类树
""" + "\n".join(f"- {c}: {', '.join(subs)}" for c, subs in TAXONOMY.items()) + """

# 输入(JSON)
{INPUT}

# 输出
严格 JSON,不要 markdown,形如:
{"results":[{"i":0,"cat":"🛠️ 办公与效率","sub":"PPT 演示"},{"i":1,"cat":"🎨 视觉创作","sub":"图像生成"}]}

每个 i 对应输入数组的下标。cat 必须是上面 12 个一级之一,sub 必须是该 cat 下列出的二级之一。
判断时优先看「能做什么/适合场景/标签」,而非工具名。"""

def query_pages():
    pages = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor: body["start_cursor"] = cursor
        r = requests.post(f"https://api.notion.com/v1/databases/{DB_ID}/query", headers=H, json=body)
        d = r.json()
        pages.extend(d["results"])
        if not d.get("has_more"): break
        cursor = d["next_cursor"]
    return pages


def page_to_input(p, idx):
    props = p["properties"]
    name = "".join(t.get("plain_text","") for t in props.get("Name",{}).get("title",[]))
    return {
        "i": idx,
        "name": name[:60],
        "headline": "".join(t.get("plain_text","") for t in (props.get("Headline",{}).get("rich_text") or []))[:120],
        "tags": [t.get("name","") for t in (props.get("Tags",{}).get("multi_select") or [])][:8],
        "cap": "".join(t.get("plain_text","") for t in (props.get("Capabilities",{}).get("rich_text") or []))[:200].replace("\n", " | "),
        "url": props.get("URL",{}).get("url","") or "",
    }


def classify_batch(items):
    """Send a batch of <= 25 items to Gemini, get categorization."""
    payload = {"items": items}
    body = {
        "contents": [{
            "parts": [{"text": CLASSIFY_PROMPT.replace("{INPUT}", json.dumps(payload, ensure_ascii=False))}]
        }],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
    r = requests.post(url, headers={"Content-Type": "application/json"}, json=body, timeout=120)
    if r.status_code != 200:
        raise Exception(f"Gemini {r.status_code}: {r.text[:300]}")
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    parsed = json.loads(text)
    return parsed.get("results", [])


def patch_page(page_id, cat, sub):
    body = {"properties": {
        "Category": {"select": {"name": cat}},
        "Subcategory": {"rich_text": [{"text": {"content": sub}}]},
    }}
    r = requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=H, json=body)
    return r.status_code == 200, r.text[:200]


def main():
    print("拉 pages...")
    pages = query_pages()
    print(f"  {len(pages)}")

    items = [page_to_input(p, i) for i, p in enumerate(pages)]

    BATCH = 25
    all_results = []
    for i in range(0, len(items), BATCH):
        chunk = items[i:i+BATCH]
        print(f"  Gemini {i}/{len(items)}...")
        try:
            results = classify_batch(chunk)
            all_results.extend(results)
        except Exception as e:
            print(f"    ⚠ batch fail: {e}")
            time.sleep(2)
            try:
                results = classify_batch(chunk)
                all_results.extend(results)
            except Exception as e:
                print(f"    ✗ retry fail: {e}")
        time.sleep(0.4)

    print(f"\n got {len(all_results)} classifications")

    # Save snapshot before patching
    snap = []
    for r in all_results:
        idx = r["i"]
        if idx >= len(pages): continue
        p = pages[idx]
        old_cat = p["properties"].get("Category",{}).get("select",{}).get("name","")
        old_sub = "".join(t.get("plain_text","") for t in (p["properties"].get("Subcategory",{}).get("rich_text") or []))
        title = "".join(t.get("plain_text","") for t in p["properties"].get("Name",{}).get("title",[]))
        snap.append({
            "id": p["id"],
            "name": title,
            "old_cat": old_cat, "old_sub": old_sub,
            "new_cat": r["cat"], "new_sub": r["sub"],
            "changed_cat": old_cat != r["cat"],
        })
    Path("/tmp/xiaoer-audit/reclass-snapshot.json").write_text(json.dumps(snap, ensure_ascii=False, indent=2))

    changed_cat = sum(1 for x in snap if x["changed_cat"])
    print(f"  一级类变化: {changed_cat}/{len(snap)}")

    # Patch Notion in parallel
    print("\nPATCH Notion...")
    ok = 0; fail = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(patch_page, x["id"], x["new_cat"], x["new_sub"]): x for x in snap}
        for fut in as_completed(futs):
            success, msg = fut.result()
            if success: ok += 1
            else:
                fail += 1
                x = futs[fut]
                print(f"    ✗ {x['name']}: {msg[:80]}")
    print(f"  PATCH ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
