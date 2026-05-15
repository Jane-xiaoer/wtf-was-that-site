#!/usr/bin/env python3
"""
分类自学反馈收集器 (B 任务)

工作流:
1. 读 .classification_log.jsonl 拿初始分类 (capture.py 写)
2. 查 Notion 这些 page 的当前 Category/Subcategory + last_edited_time
3. 如果当前分类 != 初始分类 → 写一条 correction 到 .classification_corrections.jsonl
4. 已 process 的 page_id 记到 .feedback_cursor.json,下次跳过

设计要点:
- 只看「用户在创建后手工改过」的卡: last_edited > created_time + 60s
- archived 的卡跳过 (那些是去重产物,不是用户主动改的)
- 一条 page 改过多次也只记最新一次 (last_edited 决定)
- corrections.jsonl 是 append-only,capture.py 注入 fewshot 时只看最近 200 条
"""
import os, json, time, sys
from pathlib import Path
from datetime import datetime
import requests

PROJECT_ROOT = Path(__file__).parent
CLASSIFY_LOG = PROJECT_ROOT / ".classification_log.jsonl"
CORRECTIONS = PROJECT_ROOT / ".classification_corrections.jsonl"
CURSOR_FILE = PROJECT_ROOT / ".feedback_cursor.json"
ENV_FILE = PROJECT_ROOT / ".env"
LOG_FILE = PROJECT_ROOT / "logs" / "feedback.log"

# Fix stale SSL_CERT_FILE
_ssl_file = os.environ.get("SSL_CERT_FILE")
if _ssl_file and not os.path.exists(_ssl_file):
    os.environ.pop("SSL_CERT_FILE", None)


def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_env():
    env = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
NOTION_TOKEN = ENV.get("NOTION_TOKEN", "")
NOTION_VERSION = "2022-06-28"


def load_cursor():
    if not CURSOR_FILE.exists():
        return {"processed_pages": {}, "last_run": ""}
    try:
        return json.loads(CURSOR_FILE.read_text())
    except Exception:
        return {"processed_pages": {}, "last_run": ""}


def save_cursor(c):
    c["last_run"] = datetime.now().isoformat(timespec="seconds")
    CURSOR_FILE.write_text(json.dumps(c, ensure_ascii=False, indent=2))


def load_classify_log():
    """yield (page_id, record) from JSONL."""
    if not CLASSIFY_LOG.exists():
        return
    for ln in CLASSIFY_LOG.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
            if rec.get("page_id"):
                yield rec["page_id"], rec
        except Exception:
            continue


def fetch_page(page_id):
    """Return Notion page dict or None."""
    r = requests.get(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": NOTION_VERSION},
        timeout=30,
    )
    if r.status_code != 200:
        return None
    return r.json()


def get_cat_sub(page):
    p = page.get("properties", {})
    cat_obj = (p.get("Category") or {}).get("select") or {}
    cat = cat_obj.get("name", "") if cat_obj else ""
    sub_obj = (p.get("Subcategory") or {}).get("select") or {}
    sub = sub_obj.get("name", "") if sub_obj else ""
    return cat, sub


def get_name_headline(page):
    p = page.get("properties", {})
    name_arr = (p.get("Name") or {}).get("title") or []
    name = "".join(x.get("plain_text", "") for x in name_arr)
    hl_arr = (p.get("Headline") or {}).get("rich_text") or []
    hl = "".join(x.get("plain_text", "") for x in hl_arr)
    return name, hl


def get_url(page):
    p = page.get("properties", {})
    return (p.get("URL") or {}).get("url") or ""


def main():
    if not NOTION_TOKEN:
        log("⚠ 找不到 NOTION_TOKEN,跳过")
        return 1

    cursor = load_cursor()
    processed = cursor.get("processed_pages", {})  # page_id → last_seen_edit_time

    # 已记录的 corrections: 按 page_id dedup,避免同一 page 多次记
    seen_corrections = set()
    if CORRECTIONS.exists():
        for ln in CORRECTIONS.read_text().splitlines():
            try:
                rec = json.loads(ln)
                if rec.get("page_id"):
                    seen_corrections.add(rec["page_id"])
            except Exception:
                continue

    checked = 0
    new_corrections = 0
    skipped_archived = 0

    for page_id, log_rec in load_classify_log():
        page = fetch_page(page_id)
        if page is None:
            continue
        if page.get("archived"):
            skipped_archived += 1
            continue

        checked += 1
        created = page.get("created_time", "")
        edited = page.get("last_edited_time", "")
        # 没改过 (或秒级差距,可忽略)
        if not edited or edited <= created:
            processed[page_id] = edited
            continue

        # 增量优化: 如果 edited 时间没变,跳过
        if processed.get(page_id) == edited:
            continue

        cur_cat, cur_sub = get_cat_sub(page)
        init_cat = log_rec.get("cat", "")
        init_sub = log_rec.get("sub", "")

        if (cur_cat, cur_sub) == (init_cat, init_sub):
            # 改了别的字段,分类没变
            processed[page_id] = edited
            continue

        # 命中: 分类被用户改过
        name, headline = get_name_headline(page)
        url = get_url(page) or log_rec.get("url", "")
        correction = {
            "page_id": page_id,
            "url": url,
            "name": name or log_rec.get("name", ""),
            "headline": headline or log_rec.get("headline", ""),
            "was": {"cat": init_cat, "sub": init_sub},
            "is": {"cat": cur_cat, "sub": cur_sub},
            "corrected_at": edited,
            "logged_at": datetime.now().isoformat(timespec="seconds"),
        }

        # 同一 page 已记过 → 更新 (用 append + 后续 dedup 模式)
        with open(CORRECTIONS, "a") as f:
            f.write(json.dumps(correction, ensure_ascii=False) + "\n")

        new_corrections += 1
        log(f"  📝 学到: {name[:30]} | [{init_cat}/{init_sub or '—'}] → [{cur_cat}/{cur_sub or '—'}]")
        processed[page_id] = edited

    cursor["processed_pages"] = processed
    save_cursor(cursor)

    log(f"完成: 扫了 {checked} 条 (skip-archived {skipped_archived}), 学到 {new_corrections} 条新更正")
    return 0


if __name__ == "__main__":
    sys.exit(main())
