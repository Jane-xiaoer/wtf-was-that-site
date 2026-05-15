#!/bin/bash
# 从前台浏览器自动拿 URL 并触发 capture.py
# 多窗口安全：用 System Events 拿视觉最前窗口的标题，再去对应浏览器找匹配的 window

set -u

FRONT_APP=$(osascript -e 'tell application "System Events" to name of first application process whose frontmost is true' 2>/dev/null)

case "$FRONT_APP" in
    "Google Chrome"|"Chrome") BROWSER="Google Chrome" ;;
    "Tabbit") BROWSER="Tabbit" ;;
    "Microsoft Edge") BROWSER="Microsoft Edge" ;;
    "Brave Browser") BROWSER="Brave Browser" ;;
    "Arc") BROWSER="Arc" ;;
    "Safari") BROWSER="Safari" ;;
    *)
        osascript -e "display notification \"前台不是浏览器: $FRONT_APP\" with title \"❌ 网站收藏失败\""
        exit 1
        ;;
esac

URL=""

if [ "$BROWSER" = "Safari" ]; then
    URL=$(osascript -e 'tell application "Safari" to URL of front document' 2>/dev/null)
else
    # 拿视觉最前窗口的标题（System Events 准确反映 z-order）
    FRONT_TITLE=$(osascript -e "tell application \"System Events\" to tell process \"$FRONT_APP\" to try
        return name of window 1
    end try" 2>/dev/null)

    # 转义特殊字符以安全嵌入 AppleScript
    ESCAPED_TITLE=$(printf '%s' "$FRONT_TITLE" | sed 's/\\/\\\\/g; s/"/\\"/g')

    # 在浏览器的 windows 集合里按标题匹配
    if [ -n "$FRONT_TITLE" ]; then
        URL=$(osascript <<APPLESCRIPT 2>/dev/null
tell application "$BROWSER"
    set targetTitle to "$ESCAPED_TITLE"
    repeat with w in windows
        try
            if title of w is targetTitle then
                return URL of active tab of w
            end if
        end try
    end repeat
end tell
APPLESCRIPT
)
    fi

    # 兜底：用浏览器自己认为的 front window
    if [ -z "$URL" ]; then
        URL=$(osascript -e "tell application \"$BROWSER\" to URL of active tab of front window" 2>/dev/null)
    fi
fi

if [ -z "$URL" ]; then
    osascript -e "display notification \"无法从 $FRONT_APP 获取 URL\" with title \"❌ 网站收藏失败\""
    exit 1
fi

# 立刻通知用户要存的 URL (15 秒内能反悔)
DOMAIN=$(echo "$URL" | sed -E 's|^https?://([^/]+).*|\1|')
osascript -e "display notification \"$DOMAIN\" with title \"📸 开始收藏...\" subtitle \"$URL\"" 2>/dev/null

# 用 capture.py 所在目录定位脚本和 python（不写死路径）
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PYTHON_BIN="$(command -v python3)"
exec "$PYTHON_BIN" "$SCRIPT_DIR/capture.py" "$URL"
