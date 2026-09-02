# -*- coding: utf-8 -*-
"""
agent-reach 本地桥接服务
========================
让「自媒体工作台」页面通过本地 HTTP 调用实时抓取通道。

免登录:
    B站  热门 + 搜索

登录解锁（内嵌 Chromium 窗口扫码/验证码登录 → Cookie 自动保存）:
    微博      登录后: 实时热搜         GET /api/hot?platform=weibo
    小红书    登录后: 关键词搜索 + 发现流  GET /api/search?platform=xiaohongshu&q=..
                                     GET /api/hot?platform=xiaohongshu

说明:
    小红书/微博接口或风控要求签名，因此登录后由「保持登录态的无头浏览器」直接翻页抓取页面内容，
    比纯 HTTP 慢（约 5-15 秒/次），并可能偶尔遇到平台验证码；失败时可稍后重试或重新登录。

登录接口:
    POST /api/login/start    {platform:"weibo"|"xiaohongshu"}  启动本机登录窗口(后台线程)
    GET  /api/login/status?platform=weibo                     查询登录状态
    POST /api/login/logout   {platform:"weibo"}               清除已保存登录态

Cookie 保存在本脚本同目录 agent_reach_cookies.json，仅本机使用。

用法:
    python agent_reach_bridge.py            # 默认 0.0.0.0:8799
    python agent_reach_bridge.py --port 9000
    python agent_reach_bridge.py --host 127.0.0.1
"""
import argparse
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BILI_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
BILI_HOT_URL = "https://api.bilibili.com/x/web-interface/popular?ps={n}&pn=1"
BILI_SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type?search_type=video&keyword={q}&page=1"

WEIBO_HOT_URL = "https://weibo.com/ajax/side/hotSearch"
WEIBO_LOGIN_URL = "https://passport.weibo.com/sso/signin"
XHS_BASE = "https://www.xiaohongshu.com"

AGENT_REACH_HINT = {
    "shipinhao": "视频号内容在微信 App 内，暂无公开可直连的热门/搜索接口。解锁方向：使用能打开视频号分享链接的已登录微信环境，或接入微信生态第三方数据源（如友望/新榜视频号榜单服务）后接入 agent-reach 通道",
    "xiaohongshu": "点「🔐 扫码 / 短信登录」：本机弹出登录窗口，扫码或手机验证码登录后自动保存 Cookie，即可实时抓取搜索与发现流",
    "weibo": "点「🔐 扫码 / 短信登录」：本机弹出登录窗口，扫码或手机验证码登录后自动保存 Cookie，即可实时抓取微博热搜",
    "douyin": "点「🔐 扫码 / 短信登录」：本机弹出登录窗口，扫码或手机验证码登录后自动保存 Cookie，即可实时抓取热榜与关键词搜索（抖音风控较重，偶尔需手动过验证码）",
    "kuaishou": "快手暂未开放免登录接口",
    "youtube": "需安装 yt-dlp（已在 agent-reach venv 内）并确保可访问 YouTube",
    "web": "任意网页读取: curl -s \"https://r.jina.ai/URL\"（需能访问 r.jina.ai）",
}

# ================= 登录态 / Cookie 存储 =================
_HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(_HERE, "agent_reach_cookies.json")
_LOGIN_LOCK = threading.Lock()
# LOGIN_STATE[platform] = {"status": idle/running/ready/error/timeout, "msg": str, "time": int}
LOGIN_STATE = {}


def _login_mark(platform, status, msg):
    with _LOGIN_LOCK:
        LOGIN_STATE[platform] = {"status": status, "msg": msg, "time": int(time.time())}


def _login_get(platform):
    with _LOGIN_LOCK:
        return LOGIN_STATE.get(platform, {"status": "idle", "msg": "", "time": 0})


def cookies_load():
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def cookies_save(store):
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print("[cookies_save] 失败:", e)


def cookie_header(platform):
    """把已保存的某平台 Cookie 拼成 HTTP 请求头字符串。"""
    store = cookies_load().get(platform) or {}
    cs = store.get("cookies") or []
    if not cs:
        return ""
    seen = {}
    for c in cs:
        seen[c.get("name")] = c.get("value", "")
    return "; ".join("%s=%s" % (k, v) for k, v in seen.items() if v)


def platform_ready(platform):
    return bool(cookie_header(platform))


def logout_platform(platform):
    with _LOGIN_LOCK:
        store = cookies_load()
        if platform in store:
            store.pop(platform, None)
            try:
                with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                    json.dump(store, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
        LOGIN_STATE[platform] = {"status": "idle", "msg": "已退出登录", "time": int(time.time())}
    return True


# ================= 内嵌浏览器登录（通用） =================
# auth: cookie 名匹配任意一个（支持前缀）即视为登录成功
LOGIN_TARGETS = {
    "weibo": {
        "url": WEIBO_LOGIN_URL,
        "auth": ("SUB", "SUBP", "SUBS"),
        "name": "微博",
        "ready": lambda: platform_ready("weibo"),
    },
    "xiaohongshu": {
        "url": XHS_BASE + "/explore",
        "auth": ("web_session", "web_session_ss"),
        "name": "小红书",
        "ready": lambda: platform_ready("xiaohongshu"),
    },
    "douyin": {
        "url": "https://www.douyin.com/",
        "auth": ("sessionid", "sid_guard"),
        "name": "抖音",
        "ready": lambda: platform_ready("douyin"),
    },
}


def _have_auth_cookie(cookies, names):
    for c in cookies:
        cname = c.get("name") or ""
        for n in names:
            if cname == n or cname.startswith(n):
                return True
    return False


def run_login(platform):
    t = LOGIN_TARGETS[platform]
    _login_mark(platform, "running", "正在启动本机浏览器…")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        _login_mark(platform, "error", "未安装 Playwright：请运行 pip install playwright && python -m playwright install chromium 后重启桥接")
        return
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 820})
            page = ctx.new_page()
            _login_mark(platform, "running", "已打开%s登录窗口：请扫码或用手机验证码登录%s（建议勾选“记住登录状态”）…" % (t["name"], t["name"]))
            try:
                page.goto(t["url"], wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            ok = False
            for _ in range(200):  # 最长约 10 分钟
                time.sleep(3)
                try:
                    if page.is_closed():
                        break
                    cookies = ctx.cookies()
                    if _have_auth_cookie(cookies, t["auth"]):
                        store = cookies_load()
                        store[platform] = {
                            "cookies": [{
                                "name": c.get("name"), "value": c.get("value"),
                                "domain": c.get("domain", ""), "path": c.get("path", "/"),
                                "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly")),
                                "sameSite": c.get("sameSite", "Lax"),
                            } for c in cookies],
                            "time": int(time.time()),
                        }
                        cookies_save(store)
                        ok = True
                        break
                except Exception:
                    break
            try:
                browser.close()
            except Exception:
                pass
            if ok:
                _login_mark(platform, "ready", "✅ %s登录成功，Cookie 已保存，现在可以实时抓取" % t["name"])
            else:
                _login_mark(platform, "timeout", "%s登录超时或窗口已关闭，请重试" % t["name"])
    except Exception as e:
        _login_mark(platform, "error", "登录过程出错：%s" % str(e)[:200])


# ================= HTTP 抓取（免登录 / Cookie） =================
def http_get(url, timeout=15, referer=None, cookie=None):
    headers = {"User-Agent": BILI_UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def clean_title(t):
    t = re.sub(r"<[^>]+>", "", t)
    return t.strip()


def fmt_num(v):
    try:
        v = int(v)
        if v >= 100000000:
            return "%.1f亿" % (v / 100000000)
        if v >= 10000:
            return "%.1f万" % (v / 10000)
        return str(v)
    except Exception:
        return "—"


def fetch_bili_hot(n):
    data = json.loads(http_get(BILI_HOT_URL.format(n=n)))
    if data.get("code") != 0:
        raise RuntimeError("B站热门接口错误: %s" % data.get("message", data))
    out = []
    for v in data["data"]["list"]:
        out.append({
            "platform": "bilibili",
            "title": clean_title(v.get("title", "")),
            "author": (v.get("owner") or {}).get("name", ""),
            "views": fmt_num((v.get("stat") or {}).get("view")),
            "likes": fmt_num((v.get("stat") or {}).get("like")),
            "danmaku": fmt_num((v.get("stat") or {}).get("danmaku")),
            "url": "https://www.bilibili.com/video/%s" % v.get("bvid", ""),
            "pic": v.get("pic", ""),
            "duration": v.get("duration", 0),
            "hotLevel": min(99, (v.get("stat") or {}).get("his_rank") or 80),
        })
    return out


def fetch_bili_search(q, n):
    url = BILI_SEARCH_URL.format(q=urllib.parse.quote(q))
    data = json.loads(http_get(url, referer="https://search.bilibili.com/"))
    if data.get("code") != 0:
        raise RuntimeError("B站搜索接口错误: %s" % data.get("message", data))
    items = data.get("data", {}).get("result") or []
    out = []
    for v in items[:n]:
        if v.get("type") != "video":
            continue
        out.append({
            "platform": "bilibili",
            "title": clean_title(v.get("title", "")),
            "author": v.get("author", ""),
            "views": fmt_num(v.get("play")),
            "likes": fmt_num(v.get("like")),
            "danmaku": fmt_num(v.get("video_review")),
            "url": "https://www.bilibili.com/video/%s" % v.get("bvid", ""),
            "pic": v.get("pic", ""),
            "duration": v.get("duration", ""),
            "hotLevel": min(99, int(v.get("rank") or 80) + 60),
            "desc": clean_title(v.get("description", ""))[:80],
        })
    return out


WEIBO_LABEL_HOT = {"爆": 99, "沸": 96, "热": 92, "新": 87, "荐": 84, "商": 80}


def fetch_weibo_hot(n):
    """微博实时热搜（需要登录 Cookie，HTTP 直连即可）。"""
    ck = cookie_header("weibo")
    if not ck:
        raise RuntimeError("微博未登录：请先点「🔐 扫码 / 短信登录」")
    data = json.loads(http_get(WEIBO_HOT_URL, referer="https://weibo.com/hot/search", cookie=ck))
    if data.get("ok") != 1:
        raise RuntimeError("微博热搜接口异常，可能登录态失效，请重新登录")
    realtime = (data.get("data") or {}).get("realtime") or []
    out = []
    i = 0
    for v in realtime:
        word = v.get("word") or v.get("note") or ""
        if not word or v.get("is_ad"):
            continue
        i += 1
        label = v.get("label_name") or ""
        num = v.get("num")
        out.append({
            "platform": "weibo",
            "title": word,
            "author": "微博热搜",
            "views": (fmt_num(num) + " 热") if num else "—",
            "likes": "—",
            "comments": "—",
            "url": "https://s.weibo.com/weibo?q=" + urllib.parse.quote("#" + word + "#"),
            "pic": "",
            "duration": "",
            "hotLevel": WEIBO_LABEL_HOT.get(label, 80),
            "desc": ("#" + label + "#") if label else ("实时热搜 #%d" % i),
        })
        if len(out) >= n:
            break
    return out


def fetch_weibo_search(q, n):
    raise RuntimeError("微博关键词搜索抓取暂未开放；当前可用「实时热搜榜」，清空关键词即可抓取")


# ================= 小红书：登录态浏览器翻页抓取 =================
# 抓取页面中“探索/搜索”内容卡片 DOM（section.note-item），兼顾站点结构变化做多选择器回退。
_XHS_EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const pick = document.querySelectorAll('a[href*="/explore/"]');
  pick.forEach(a => {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/(explore|discovery\/item)\/([0-9a-zA-Z]+)/);
    const id = m ? m[2] : href;
    const card = a.closest('section') || a.closest('.note-item') || a.parentElement;
    const tEl = card ? card.querySelector('.title, .note-title, .title span') : null;
    let title = tEl ? (tEl.textContent || '').trim() : '';
    if (!title) title = (a.getAttribute('title') || '').trim();
    if (!title) {
      const img = a.querySelector('img');
      if (img) title = (img.getAttribute('alt') || '').trim();
    }
    if (!title) return;
    if (seen.has(title)) return;
    seen.add(title);
    const nEl = card ? card.querySelector('.author .name, .author-name, .nickname') : null;
    const author = nEl ? (nEl.textContent || '').trim() : '';
    const cEl = card ? card.querySelector('.count, .like-wrapper .count, .like .count') : null;
    const likes = cEl ? (cEl.textContent || '').trim() : '';
    const bgEl = card ? (card.querySelector('.cover') || card) : a;
    const st = (bgEl.getAttribute('style') || '') + ' ' + (a.getAttribute('style') || '');
    const pm = st.match(/url\(['"]?([^'")]+)/);
    let pic = pm ? pm[1] : '';
    if (pic.startsWith('//')) pic = 'https:' + pic;
    out.push({id: id, title: title, author: author, likes: likes, pic: pic, url: 'https://www.xiaohongshu.com/explore/' + id});
  });
  return out;
}
"""


def _ctx_from_cookies(platform, headless=True):
    """用已保存 Cookie 启动无头/有头浏览器上下文（用于登录态翻页抓取）。"""
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    browser = p.chromium.launch(headless=headless)
    ctx = browser.new_context(
        locale="zh-CN",
        viewport={"width": 1280, "height": 900},
        user_agent=BILI_UA,
    )
    store = cookies_load().get(platform) or {}
    cs = store.get("cookies") or []
    if cs:
        ctx.add_cookies(cs)
    return p, browser, ctx


def fetch_xhs(mode, q, n):
    """小红书搜索(mode=search) / 发现流(mode=feed)。需已保存登录 Cookie。"""
    if not platform_ready("xiaohongshu"):
        raise RuntimeError("小红书未登录：请先点「🔐 扫码 / 短信登录」")
    p, browser, ctx = None, None, None
    try:
        p, browser, ctx = _ctx_from_cookies("xiaohongshu")
        page = ctx.new_page()
        if mode == "search":
            url = XHS_BASE + "/search_result?keyword=" + urllib.parse.quote(q)
        else:
            url = XHS_BASE + "/explore"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        # 等待内容卡片出现（页面可能先弹登录引导/加载较慢）
        found = False
        for _ in range(12):
            time.sleep(1)
            try:
                cnt = page.evaluate("() => document.querySelectorAll('section.note-item, .note-item').length")
                if cnt > 0:
                    found = True
                    break
            except Exception:
                break
        # 向下滚动收集更多
        for _ in range(3):
            if not found and mode == "search":
                break
            try:
                page.mouse.wheel(0, 2200)
                time.sleep(1.2)
            except Exception:
                break
        raw = page.evaluate(_XHS_EXTRACT_JS)
        out = []
        for v in raw:
            if not v.get("title"):
                continue
            out.append({
                "platform": "xiaohongshu",
                "title": v["title"],
                "author": v.get("author", ""),
                "views": "—",
                "likes": v.get("likes") or "—",
                "comments": "—",
                "url": v.get("url", XHS_BASE),
                "pic": v.get("pic", ""),
                "duration": "",
                "hotLevel": 80,
                "desc": "",
            })
            if len(out) >= n:
                break
        if not out:
            raise RuntimeError("页面未抓到内容：可能要求登录、触发验证码或页面结构变化，请稍后重试")
        return out
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass


# ================= 抖音：登录态浏览器翻页抓取 =================
_DOUYIN_EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const as = document.querySelectorAll('a[href*="/video/"]');
  as.forEach(a => {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/video\/(\d+)/);
    const id = m ? m[1] : href;
    if (seen.has(id)) return;
    seen.add(id);
    const img = a.querySelector('img');
    let title = (a.getAttribute('title') || '').trim();
    if (!title) title = (a.textContent || '').trim().replace(/\s+/g, ' ');
    if (!title && img) title = ((img.getAttribute('alt') || '')).trim();
    if (!title) {
      const card = a.closest('div');
      if (card && card.parentElement) title = (card.textContent || '').trim().replace(/\s+/g, ' ');
    }
    title = (title || '').slice(0, 60).trim();
    if (!title || title.length < 2) return;
    if (seen.has(id + '|' + title)) return;
    seen.add(id + '|' + title);
    const cover = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
    out.push({id: id, title: title, pic: cover.startsWith('//') ? 'https:' + cover : cover, url: 'https://www.douyin.com/video/' + id});
  });
  return out;
}
"""


def fetch_douyin(mode, q, n):
    """抖音热榜(mode=hot) / 关键词搜索(mode=search)。需已保存登录 Cookie，由无头浏览器翻页抓 DOM。"""
    if not platform_ready("douyin"):
        raise RuntimeError("抖音未登录：请先点「🔐 扫码 / 短信登录」")
    p, browser, ctx = None, None, None
    try:
        p, browser, ctx = _ctx_from_cookies("douyin")
        page = ctx.new_page()
        if mode == "search":
            url = "https://www.douyin.com/search/" + urllib.parse.quote(q) + "?type=general"
        else:
            url = "https://www.douyin.com/hot"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        found = False
        for _ in range(15):  # 等待视频卡片出现（最多约 15 秒）
            time.sleep(1)
            try:
                cnt = page.evaluate("() => document.querySelectorAll('a[href*=\"/video/\"]').length")
                if cnt > 0:
                    found = True
                    break
            except Exception:
                break
        for _ in range(4):  # 向下滚动触发懒加载
            try:
                page.mouse.wheel(0, 2400)
                time.sleep(1.3)
            except Exception:
                break
        raw = page.evaluate(_DOUYIN_EXTRACT_JS)
        out = []
        for v in raw:
            if not v.get("title"):
                continue
            out.append({
                "platform": "douyin",
                "title": v["title"],
                "author": "",
                "views": "—",
                "likes": "—",
                "comments": "—",
                "url": v.get("url", "https://www.douyin.com"),
                "pic": v.get("pic", ""),
                "duration": "",
                "hotLevel": 80,
                "desc": "",
            })
            if len(out) >= n:
                break
        if not out:
            raise RuntimeError("页面未抓到内容：可能触发验证码/滑块，或需在登录窗口手动确认一次，请稍后重试")
        return out
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass


# ================= HTTP 服务 =================
class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _status_channels(self):
        def login_item(platform, name, hint_ready, note_ready):
            if platform_ready(platform):
                return {"id": platform, "name": name, "status": "ready", "note": note_ready}
            return {"id": platform, "name": name, "status": "needs-login", "note": hint_ready}
        return [
            {"id": "bilibili", "name": "B站", "status": "ready", "note": "热门+搜索 直连可用"},
            login_item("xiaohongshu", "小红书", AGENT_REACH_HINT["xiaohongshu"], "已登录 · 搜索/发现流实时可用"),
            login_item("douyin", "抖音", AGENT_REACH_HINT["douyin"], "已登录 · 热榜/搜索实时可用"),
            {"id": "shipinhao", "name": "视频号", "status": "needs-login", "note": AGENT_REACH_HINT["shipinhao"]},
            login_item("weibo", "微博", AGENT_REACH_HINT["weibo"], "已登录 · 实时热搜可用"),
            {"id": "youtube", "name": "YouTube", "status": "needs-tool", "note": AGENT_REACH_HINT["youtube"]},
        ]

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

            if path == "/api/status":
                return self._json(200, {
                    "ok": True,
                    "service": "agent-reach bridge v2.1",
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "channels": self._status_channels(),
                })

            if path == "/api/login/status":
                platform = (params.get("platform") or "").strip()
                if platform not in LOGIN_TARGETS:
                    return self._json(501, {"ok": False, "error": "该平台暂未接入登录态"})
                st = _login_get(platform)
                if LOGIN_TARGETS[platform]["ready"]():
                    status, msg = "ready", (st.get("msg") if st.get("status") == "ready" else "✅ 已登录，可直接实时抓取（如需重登可先退出）")
                else:
                    status, msg = st.get("status"), st.get("msg")
                return self._json(200, {"ok": True, "platform": platform, "status": status, "msg": msg, "ready": status == "ready"})

            if path == "/api/hot":
                platform = params.get("platform", "bilibili")
                n = min(int(params.get("n", 10)), 30)
                if platform == "bilibili":
                    items = fetch_bili_hot(n)
                    return self._json(200, {"ok": True, "platform": "bilibili", "items": items})
                if platform == "weibo":
                    items = fetch_weibo_hot(n)
                    return self._json(200, {"ok": True, "platform": "weibo", "items": items})
                if platform == "xiaohongshu":
                    items = fetch_xhs("feed", "", n)
                    return self._json(200, {"ok": True, "platform": "xiaohongshu", "items": items})
                if platform == "douyin":
                    items = fetch_douyin("hot", "", n)
                    return self._json(200, {"ok": True, "platform": "douyin", "items": items})
                return self._json(501, {"ok": False, "error": "该平台当前不可用", "hint": AGENT_REACH_HINT.get(platform, "平台未配置")})

            if path == "/api/search":
                platform = params.get("platform", "bilibili")
                q = params.get("q", "").strip()
                n = min(int(params.get("n", 10)), 30)
                if not q:
                    return self._json(400, {"ok": False, "error": "缺少搜索关键词 q"})
                if platform == "bilibili":
                    items = fetch_bili_search(q, n)
                    return self._json(200, {"ok": True, "platform": "bilibili", "q": q, "items": items})
                if platform == "xiaohongshu":
                    items = fetch_xhs("search", q, n)
                    return self._json(200, {"ok": True, "platform": "xiaohongshu", "q": q, "items": items})
                if platform == "douyin":
                    items = fetch_douyin("search", q, n)
                    return self._json(200, {"ok": True, "platform": "douyin", "q": q, "items": items})
                if platform == "weibo":
                    return self._json(501, {"ok": False, "error": "微博关键词搜索暂未开放", "hint": "微博当前支持「实时热搜榜」：清空关键词、直接点实时抓取即可"})
                return self._json(501, {"ok": False, "error": "该平台当前不可用", "hint": AGENT_REACH_HINT.get(platform, "平台未配置")})

            return self._json(404, {"ok": False, "error": "未知接口: " + path})
        except Exception as e:
            return self._json(500, {"ok": False, "error": str(e)[:300]})

    def do_POST(self):
        try:
            path = urllib.parse.urlparse(self.path).path
            body = self._read_body()
            platform = (body.get("platform") or "").strip()

            if path == "/api/login/start":
                if platform not in LOGIN_TARGETS:
                    return self._json(501, {"ok": False, "error": "该平台的登录助手暂未接入", "hint": AGENT_REACH_HINT.get(platform, "平台未配置")})
                t = LOGIN_TARGETS[platform]
                cur = _login_get(platform).get("status")
                if cur == "running":
                    return self._json(200, {"ok": True, "platform": platform, "status": "running", "msg": "登录窗口已在运行中，请完成登录", "ready": t["ready"]()})
                if t["ready"]():
                    _login_mark(platform, "ready", "✅ %s已处于登录状态，可直接实时抓取" % t["name"])
                    return self._json(200, {"ok": True, "platform": platform, "status": "ready", "msg": "%s已登录" % t["name"], "ready": True})
                threading.Thread(target=run_login, args=(platform,), daemon=True).start()
                return self._json(200, {"ok": True, "platform": platform, "status": "running", "msg": "正在启动本机登录窗口…", "ready": False})

            if path == "/api/login/logout":
                if platform in LOGIN_TARGETS:
                    logout_platform(platform)
                    return self._json(200, {"ok": True, "platform": platform, "status": "idle", "msg": "已清除登录态", "ready": False})
                return self._json(501, {"ok": False, "error": "该平台暂未接入登录态"})

            return self._json(404, {"ok": False, "error": "未知接口: " + path})
        except Exception as e:
            return self._json(500, {"ok": False, "error": str(e)[:300]})


def main():
    ap = argparse.ArgumentParser(description="agent-reach local bridge for 自媒体工作台")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8799)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("agent-reach bridge v2.1 running at http://%s:%d" % (a.host, a.port))
    print("工作台面板「桥接地址」请填: http://%s:%d" % ("127.0.0.1", a.port))
    for pid in LOGIN_TARGETS:
        if LOGIN_TARGETS[pid]["ready"]():
            print("%s：已登录，实时抓取可用" % LOGIN_TARGETS[pid]["name"])
        else:
            print("%s：未登录，可在工作台点「🔐 扫码 / 短信登录」解锁" % LOGIN_TARGETS[pid]["name"])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
