#!/usr/bin/env python3
"""
Bookmark Watcher: 监听 Chromium 浏览器书签文件，新增书签即触发 capture pipeline。

策略:
- 启动时拍快照（不回填历史书签）
- 每 N 秒 diff 一次：URL 集合新增 → 触发 capture.py
- URL 删除后再添加（取消收藏再点一次）也会触发（diff 自然处理）
- 多浏览器汇总，去重在 capture.py 层做
"""
import json, os, sys, time, subprocess, threading, shutil
# Fix stale SSL_CERT_FILE
_ssl_file = os.environ.get("SSL_CERT_FILE")
if _ssl_file and not os.path.exists(_ssl_file):
    os.environ.pop("SSL_CERT_FILE", None)
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

BROWSER_ROOTS = {
    "Chrome": Path.home() / "Library/Application Support/Google/Chrome",
    "Tabbit": Path.home() / "Library/Application Support/Tabbit",
    "Edge": Path.home() / "Library/Application Support/Microsoft Edge",
    "Brave": Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser",
    "Arc": Path.home() / "Library/Application Support/Arc/User Data",
}

def discover_bookmark_files():
    """Find Bookmarks files for ALL Chromium-based browsers under ~/Library/Application Support.
    Auto-discovers any Chromium fork (Chrome/Brave/Edge/Tabbit/Dia/Comet/Quark/Doubao/Vivaldi/Opera/...)
    so newly-installed browsers are picked up without code changes.
    Returns list of (browser_name, profile_name, path).
    """
    files = []
    seen = set()
    APP_SUP = Path.home() / "Library/Application Support"

    def is_profile_name(n: str) -> bool:
        return n in ("Default", "Guest Profile") or n.startswith("Profile ")

    def maybe_add(browser: str, profile_dir: Path):
        bm = profile_dir / "Bookmarks"
        if bm.exists() and bm not in seen:
            files.append((browser, profile_dir.name, bm))
            seen.add(bm)

    # 1) Known structural overrides (browsers that don't follow ~/Library/AS/<Name>/<Profile>/Bookmarks)
    for name, root in BROWSER_ROOTS.items():
        if not root.exists():
            continue
        for entry in root.iterdir():
            if entry.is_dir() and is_profile_name(entry.name):
                maybe_add(name, entry)

    # 2) Auto-discover: scan up to depth 2 under ~/Library/Application Support
    #    Pattern A: ~/Library/Application Support/<X>/<Profile>/Bookmarks       (Chrome/Edge/Tabbit/Doubao/Comet/Quark)
    #    Pattern B: ~/Library/Application Support/<X>/<sub>/<Profile>/Bookmarks (Brave Software/Brave-Browser, Arc/User Data, Dia/User Data)
    if APP_SUP.exists():
        for app_dir in APP_SUP.iterdir():
            if not app_dir.is_dir():
                continue
            try:
                children = list(app_dir.iterdir())
            except (PermissionError, OSError):
                # macOS protects some dirs (MobileSync, etc) — skip
                continue
            for entry in children:
                if not entry.is_dir():
                    continue
                if is_profile_name(entry.name):
                    maybe_add(app_dir.name, entry)
                else:
                    # Depth-2 (e.g. Dia/User Data/Default, BraveSoftware/Brave-Browser/Default)
                    try:
                        for sub in entry.iterdir():
                            if sub.is_dir() and is_profile_name(sub.name):
                                maybe_add(app_dir.name, sub)
                    except (PermissionError, OSError):
                        pass

    return files

PROJECT_ROOT = Path(__file__).parent
BASELINE_FILE = PROJECT_ROOT / ".bookmark-baseline.json"
LAST_PROCESSED_FILE = PROJECT_ROOT / ".last-processed.json"
STARTUP_GRACE_SECONDS = 60  # 容忍 Chrome 写文件延迟
LOG_DIR = PROJECT_ROOT / "logs"
CAPTURE_SCRIPT = str(PROJECT_ROOT / "capture.py")
PYTHON = sys.executable
POLL_INTERVAL = 3  # seconds
STAGGER_DELAY = 0.5

def log(msg):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat()}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "watcher.log", "a") as f:
        f.write(line + "\n")

CHROME_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 UTC and 1970-01-01 UTC

def chrome_time_to_unix(s):
    """Convert Chrome's microseconds-since-1601 string to Unix seconds. 0 if invalid."""
    try:
        return int(s) / 1_000_000 - CHROME_EPOCH_OFFSET
    except Exception:
        return 0

def collect_bookmark_urls(path):
    """Recursively extract URLs from a Chromium Bookmarks JSON file. (legacy: set)"""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        log(f"⚠ 解析失败 {path.name}: {e}")
        return set()
    urls = set()
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "url":
                u = node.get("url", "")
                if u.startswith(("http://", "https://")):
                    urls.add(u)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(data.get("roots", {}))
    return urls

def collect_bookmarks_with_time(path):
    """Yield (url, date_added_unix) for every bookmark entry."""
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        log(f"⚠ 解析失败 {path.name}: {e}")
        return
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "url":
                u = node.get("url", "")
                added = chrome_time_to_unix(node.get("date_added", "0"))
                if u.startswith(("http://", "https://")):
                    yield u, added
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)
    yield from walk(data.get("roots", {}))

def all_urls():
    """Aggregate URLs across browsers/profiles (used for first-time baseline)."""
    s = set()
    for browser, profile, path in discover_bookmark_files():
        s |= collect_bookmark_urls(path)
    return s

def urls_per_profile():
    """Return dict mapping 'Browser/Profile' → set of URLs."""
    result = {}
    for browser, profile, path in discover_bookmark_files():
        result[f"{browser}/{profile}"] = collect_bookmark_urls(path)
    return result

def load_last_processed():
    if not LAST_PROCESSED_FILE.exists():
        return None
    try:
        return float(json.loads(LAST_PROCESSED_FILE.read_text())["last_processed"])
    except Exception:
        return None

def save_last_processed(t):
    LAST_PROCESSED_FILE.write_text(json.dumps({"last_processed": t}))

def trigger_capture(url):
    log(f"  ⚡ 触发: {url}")
    subprocess.Popen(
        [PYTHON, CAPTURE_SCRIPT, url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ─── Cross-path URL dedup ────────────────────────────────────────────
# 同一次 ⌘D 会同时被 Chrome 扩展（HTTP）和 Bookmarks 文件 watcher 两路看到。
# 这里给两路共享一个 60s 窗口，避免重复跑 capture。
_RECENT_WINDOW = 60.0
_recent_urls: dict[str, float] = {}
_recent_lock = threading.Lock()

def should_trigger(url: str, source: str) -> bool:
    """两路共享去重：60s 内同一 URL 只跑一次 capture。"""
    now = time.time()
    with _recent_lock:
        last = _recent_urls.get(url, 0.0)
        if now - last < _RECENT_WINDOW:
            log(f"  ⏭ 跳过重复 ({source}, 距上次 {int(now - last)}s): {url}")
            return False
        _recent_urls[url] = now
        if len(_recent_urls) > 500:
            cutoff = now - 3600
            for k in [k for k, v in _recent_urls.items() if v < cutoff]:
                _recent_urls.pop(k, None)
    return True

# ─── HTTP receiver for Chrome extension (绕开 Chrome Sync 不刷盘) ──────
# Chrome 装上自己的 chrome-extension/ 后,⌘D 收藏会 POST 到这里
_HTTP_PORT = 7331

def _start_http_receiver():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):  # silence default access log
            pass

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            # health check
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"service":"bookmark-capture-receiver"}')

        def do_POST(self):
            if self.path != "/hook":
                self.send_response(404); self.end_headers(); return
            length = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(length).decode() or "{}")
            except Exception:
                self.send_response(400); self.end_headers(); return
            url = (body.get("url") or "").strip()
            title = (body.get("title") or "").strip()
            if not url.startswith(("http://", "https://")):
                self.send_response(400); self.end_headers(); return

            if not should_trigger(url, "Chrome扩展"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"ok":true,"deduped":true}')
                return

            log(f"📌 [Chrome扩展] 新书签: {url}" + (f" — {title[:30]}" if title else ""))
            trigger_capture(url)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"queued":true}')

    server = ThreadingHTTPServer(("127.0.0.1", _HTTP_PORT), Handler)
    log(f"  📡 HTTP receiver: localhost:{_HTTP_PORT}/hook (for Chrome extension)")
    server.serve_forever()

_state_lock = threading.Lock()
_state = {
    "last_processed": 0.0,
    "triggered_recently": {},
}

def process_changes():
    """Scan all bookmark files for new URLs since last_processed, trigger captures."""
    with _state_lock:
        last_processed = _state["last_processed"]
        triggered_recently = _state["triggered_recently"]

        new_items = []
        for browser, profile, path in discover_bookmark_files():
            key = f"{browser}/{profile}"
            for url, added in collect_bookmarks_with_time(path):
                if added > last_processed and triggered_recently.get(url, 0) < added:
                    new_items.append((url, added, key))
                    triggered_recently[url] = added

        if not new_items:
            return

        seen = set()
        for url, added, key in new_items:
            if url in seen:
                continue
            seen.add(url)
            if not should_trigger(url, key):
                continue
            ts = datetime.fromtimestamp(added).strftime("%H:%M:%S")
            log(f"📌 [{key}] 新书签 (added@{ts}): {url}")
            trigger_capture(url)

        _state["last_processed"] = max(last_processed, max(it[1] for it in new_items))
        save_last_processed(_state["last_processed"])

class BookmarkChangeHandler(FileSystemEventHandler):
    """Trigger process_changes() on any fs event in watched dirs.
    Debounces 500ms to coalesce Chrome's multi-step bookmark write."""
    def __init__(self):
        self._timer = None
        self._lock = threading.Lock()

    def _fire(self):
        try:
            process_changes()
        except Exception as e:
            log(f"⚠ process_changes 出错: {e}")

    def on_any_event(self, event):
        if event.is_directory:
            return
        # Only care about Bookmarks file changes (Chrome also writes Bookmarks.bak, History, etc.)
        if not event.src_path.endswith("/Bookmarks") and not event.src_path.endswith("/Bookmarks~"):
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(0.5, self._fire)
            self._timer.daemon = True
            self._timer.start()

def attach_observers(observer, handler, watched_dirs):
    """Discover bookmark files & attach watchdog to any new parent dirs."""
    new_count = 0
    for browser, profile, bm_path in discover_bookmark_files():
        parent = bm_path.parent
        if parent in watched_dirs:
            continue
        try:
            observer.schedule(handler, str(parent), recursive=False)
            watched_dirs.add(parent)
            new_count += 1
            log(f"  + 新监听: {browser} / {profile}")
        except Exception as e:
            log(f"  ⚠ 无法监听 {parent}: {e}")
    return new_count

def rediscover_loop(observer, handler, watched_dirs):
    """Background thread: every 60s, look for newly-installed browsers."""
    while True:
        time.sleep(60)
        try:
            attach_observers(observer, handler, watched_dirs)
        except Exception as e:
            log(f"⚠ rediscover 出错: {e}")

# ─── B 任务: 分类自学反馈 (每 30min 跑一次) ─────────────────────────
FEEDBACK_COLLECTOR = PROJECT_ROOT / "feedback_collector.py"

def feedback_loop():
    """每 30 分钟跑一次 feedback_collector.py,从 Notion 收集用户手工改的分类。"""
    time.sleep(120)  # 启动后等 2 分钟再开始,避免和首次 process_changes 抢资源
    while True:
        try:
            subprocess.run(
                [PYTHON, str(FEEDBACK_COLLECTOR)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=300,
            )
        except Exception as e:
            log(f"⚠ feedback_collector 出错: {e}")
        time.sleep(1800)  # 30 min

# ─── C 任务: 每日健康自检 (每 24h 跑一次) ─────────────────────────
ENV_FILE = PROJECT_ROOT / ".env"

def _read_env_value(key):
    """从项目本地 .env 读 key value, 不存在返回空串。"""
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k == key:
            return v.strip().strip('"').strip("'")
    return ""

def _notify(title, msg):
    """macOS notification (failsafe, exception → silent)."""
    try:
        safe_title = title.replace('"', "'")
        safe_msg = msg.replace('"', "'")
        os.system(f'osascript -e \'display notification "{safe_msg}" with title "{safe_title}" sound name "Sosumi"\'')
    except Exception:
        pass

def health_check_once():
    """执行一次健康自检; 任何异常发 macOS 通知。"""
    problems = []

    # 1) Vercel token 健康度 (仅当配了 wall front-end)
    tools_wall_url = _read_env_value("TOOLS_WALL_URL")
    token = _read_env_value("VERCEL_TOKEN")
    if tools_wall_url and token:
        vercel_bin = shutil.which("vercel")
        if vercel_bin:
            try:
                r = subprocess.run([vercel_bin, "whoami", "--token", token],
                                   capture_output=True, text=True, timeout=20)
                if r.returncode != 0:
                    problems.append(f"Vercel token 失效 (rc={r.returncode})")
            except Exception as e:
                problems.append(f"Vercel whoami 失败: {e}")

    # 2) 最近 deploy 失败率 (扫 deploy.log)
    deploy_log = LOG_DIR / "deploy.log"
    if deploy_log.exists():
        try:
            txt = deploy_log.read_text(errors="ignore")[-50000:]
            error_lines = [ln for ln in txt.splitlines() if "Error:" in ln or "token is not valid" in ln]
            if len(error_lines) >= 3:
                problems.append(f"deploy.log 近期出现 {len(error_lines)} 条 Error")
        except Exception:
            pass

    # 3) 工具墙 sites API 健康 (仅当配了 wall front-end)
    if tools_wall_url:
        try:
            import urllib.request
            req = urllib.request.Request(f"{tools_wall_url.rstrip('/')}/api/sites",
                                         headers={"User-Agent": "bookmark-watcher-healthcheck"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    problems.append(f"Tools wall sites API 返回 {resp.status}")
        except Exception as e:
            problems.append(f"Tools wall sites API 不可达: {str(e)[:80]}")

    if problems:
        log("🚨 健康自检发现问题: " + " | ".join(problems))
        _notify("🚨 Watcher 自检异常", " | ".join(problems)[:200])
    else:
        log("💚 健康自检全部通过")

def healthcheck_loop():
    """每 24 小时跑一次健康自检。启动 5 分钟后跑首次。"""
    time.sleep(300)
    while True:
        try:
            health_check_once()
        except Exception as e:
            log(f"⚠ health_check 出错: {e}")
        time.sleep(86400)  # 24h

def main():
    log("=" * 60)
    log("📚 Bookmark Watcher 启动 (FSEvents 事件驱动)")

    # 时间游标
    persisted = load_last_processed()
    startup = time.time()
    if persisted is None:
        _state["last_processed"] = startup - STARTUP_GRACE_SECONDS
        log(f"  首次启动: 时间游标 = 启动时间 - {STARTUP_GRACE_SECONDS}s")
    else:
        _state["last_processed"] = persisted
        log(f"  恢复时间游标: {datetime.fromtimestamp(persisted).isoformat()}")
    save_last_processed(_state["last_processed"])

    # 启动 watchdog observer
    observer = Observer()
    handler = BookmarkChangeHandler()
    watched_dirs = set()

    files = discover_bookmark_files()
    log(f"  监听 {len(files)} 个 Bookmarks 文件:")
    for browser, profile, _ in files:
        log(f"    • {browser} / {profile}")
    attach_observers(observer, handler, watched_dirs)
    observer.start()

    # 后台线程: 60 秒探测一次新浏览器
    t = threading.Thread(target=rediscover_loop, args=(observer, handler, watched_dirs), daemon=True)
    t.start()

    # 后台线程: HTTP receiver,接 Chrome 扩展 POST(绕开 Chrome Sync 不刷 Bookmarks 文件的坑)
    http_t = threading.Thread(target=_start_http_receiver, daemon=True)
    http_t.start()

    # 后台线程: B 任务 - 分类反馈学习 (每 30min)
    fb_t = threading.Thread(target=feedback_loop, daemon=True)
    fb_t.start()
    log("  📚 反馈学习就位 (每 30min 扫 Notion 手工改动)")

    # 后台线程: C 任务 - 每日健康自检 (每 24h)
    hc_t = threading.Thread(target=healthcheck_loop, daemon=True)
    hc_t.start()
    log("  💚 健康自检就位 (每 24h 检查 vercel/deploy/sites API)")

    log("  事件驱动就位,等浏览器写 Bookmarks → 立刻处理")

    # 启动时也跑一次,捕获 watcher 不在线时漏掉的书签
    process_changes()

    try:
        while True:
            time.sleep(3600)  # 主线程不忙,事件由 observer 触发
    except KeyboardInterrupt:
        log("👋 退出")
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()
