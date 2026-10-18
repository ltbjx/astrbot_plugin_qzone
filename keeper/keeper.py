#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ空间 Cookie 保活服务（qzone-keeper）

解决什么问题
------------
纯 Cookie 模式最大的痛点是 **会话过期**：浏览器里拿到的 p_skey/skey 是会话级的，
闲置一段时间（实测最短只撑了约 2.5 小时）后 QQ 会把它判为失效，
点赞就开始报「登录失效」。而人工重新复制 Cookie 很烦。

本服务在服务器上常驻一个真实 Chromium：

  1. 首次启动 → 打开空间登录页 → 把二维码截图通过 HTTP 暴露出来，你手机扫一次
  2. 登录成功后 → 每 KEEPALIVE_SEC 秒访问一次空间页面，让会话持续存活
  3. 每次访问后把最新 Cookie 原子写入 qzone_cookie.txt
  4. AstrBot 插件配置 qzone_cookie_file 指向该文件 → 自动使用最新 Cookie，无需重启

因此：**服务器上依然没有登录任何 QQ 客户端**，不会挤掉你本机 / 手机上的 QQ。

环境变量
--------
  QZONE_UIN        必填，要保活的 QQ 号
  OUT_DIR          输出目录（默认 /out），Cookie 写到 $OUT_DIR/qzone_cookie.txt
  STATE_DIR        浏览器 profile 与状态文件目录（默认 /data），需要持久化
  KEEPALIVE_SEC    保活间隔秒数（默认 600 = 10 分钟）
  HTTP_PORT        二维码页端口（默认 6186）

HTTP 接口
---------
  GET /            自动刷新的二维码页面（手机扫这个）
  GET /qr.png      当前二维码图片
  GET /status      运行状态 JSON
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

import panel as panel_mod

UIN = os.environ.get("QZONE_UIN", "").strip()
OUT_DIR = Path(os.environ.get("OUT_DIR", "/out"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/data"))
KEEPALIVE_SEC = int(os.environ.get("KEEPALIVE_SEC", "600"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "6186"))

COOKIE_FILE = OUT_DIR / "qzone_cookie.txt"
QR_FILE = STATE_DIR / "qr.png"
PROFILE_DIR = STATE_DIR / "profile"
LOG_FILE = STATE_DIR / "keeper.log"
DASHBOARD_FILE = Path(__file__).with_name("dashboard.html")
LOGIN_FILE = Path(__file__).with_name("login.html")

CST = timezone(timedelta(hours=8))
HOME_URL = f"https://user.qzone.qq.com/{UIN}"

# 二维码元素可能出现的若干位置（QQ 登录页结构改过好几次）
QR_SELECTORS = (
    "#qrlogin_img",
    "img#qrlogin_img",
    "#qrlogin img",
    ".qrcode img",
    "img[src*='qrcode']",
    "img[src*='qr']",
    "#qr_code img",
    "canvas#qrcode",
)

# 导出 Cookie 时的字段顺序（把关键字段排前面，便于人工检查）
COOKIE_ORDER = (
    "uin",
    "p_uin",
    "skey",
    "p_skey",
    "pt4_token",
    "ptcz",
    "RK",
    "pgv_pvid",
    "pgv_info",
    "qlogin_uid",
    "QZ_FE_WEBP_SUPPORT",
)

# --------------------------------------------------------------------------- #
# 状态（HTTP 线程与主协程共享）
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_state = {
    "started_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
    "phase": "starting",          # starting | waiting_scan | logged_in | error
    "uin": UIN,
    "qr_updated_at": None,
    "cookie_updated_at": None,
    "keepalive_count": 0,
    "last_error": None,
    "log": [],
}


def log(msg: str) -> None:
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _lock:
        _state["log"] = (_state["log"] + [line])[-80:]
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def set_state(**kwargs) -> None:
    with _lock:
        _state.update(kwargs)


# --------------------------------------------------------------------------- #
# 二维码 / 状态 HTTP 服务
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    """HTTP 入口：控制台页面 + 各种 API（除 /health /status 外都需要 Basic 认证）"""

    def log_message(self, *args):  # 静音访问日志
        pass

    # ---------------- 基础工具 ----------------
    def _send(self, code: int, ctype: str, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False).encode())

    def _session_token(self) -> str | None:
        """从 Cookie 里取会话 token"""
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == panel_mod.COOKIE_NAME:
                return v
        return None

    def _device_token(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == panel_mod.DEVICE_COOKIE:
                return v
        return None

    def _authed(self) -> bool:
        # ① Cookie 会话（本次登录）
        if panel_mod.check_session(self._session_token()):
            return True
        # ② 信任设备（同一台设备 + 同一 UA，之前输对过密码）
        ua = self.headers.get("User-Agent") or ""
        if panel_mod.check_device(self._device_token(), ua):
            return True
        # ③ HTTP Basic（方便 curl / 监控）
        return panel_mod.check_basic_auth(self.headers.get("Authorization"))

    def _need_auth(self) -> bool:
        """未登录时：页面请求给登录页，API 请求给 401"""
        if self._authed():
            return False
        path = self._path()
        if path.startswith("/api/"):
            self._json({"ok": False, "message": "未登录或会话已过期"}, 401)
            return True
        if path in ("/", "/index.html"):
            self._send_login()
            return True
        self._send(401, "text/plain; charset=utf-8", b"need login")
        return True

    def _send_login(self, error: str = "") -> None:
        try:
            html = LOGIN_FILE.read_text(encoding="utf-8")
        except Exception as e:
            self._send(500, "text/plain; charset=utf-8", f"登录页缺失：{e}".encode())
            return
        if error:
            html = html.replace(
                "<!--ERROR-->", f'<p class="err">{error}</p>'
            )
        else:
            html = html.replace("<!--ERROR-->", "")
        self._send(200, "text/html; charset=utf-8", html.encode())

    def _set_session_cookie(self, token: str, max_age: int) -> dict:
        return {
            "Set-Cookie": f"{panel_mod.COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; "
                          f"HttpOnly; SameSite=Lax"
        }

    def _path(self) -> str:
        return self.path.split("?")[0]

    def _query(self, key: str, default: str = "") -> str:
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        return (qs.get(key) or [default])[0]

    # ---------------- GET ----------------
    def do_GET(self):  # noqa: N802
        path = self._path()

        # 健康检查 / 状态：无需认证（不含敏感信息，方便监控）
        if path == "/health":
            with _lock:
                ok = _state.get("phase") == "logged_in"
            self._send(200 if ok else 503, "text/plain", b"ok" if ok else b"not-ready")
            return

        if self._need_auth():
            return

        if path == "/status":
            with _lock:
                snap = dict(_state)
            snap["panel_user"] = panel_mod.init_auth().get("user")
            self._json(snap)
            return

        if path == "/logout":
            panel_mod.drop_session(self._session_token())
            panel_mod.forget_device(self._device_token())   # 退出即不再自动登录
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.send_header("Set-Cookie", f"{panel_mod.COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly")
            self.send_header("Set-Cookie", f"{panel_mod.DEVICE_COOKIE}=; Path=/; Max-Age=0; HttpOnly")
            self.end_headers()
            return

        if path == "/qr.png":
            if QR_FILE.is_file():
                self._send(200, "image/png", QR_FILE.read_bytes())
            else:
                self._send(404, "text/plain; charset=utf-8", "二维码还没生成".encode())
            return

        if path == "/api/state":
            with _lock:
                keeper = {
                    "phase": _state.get("phase"),
                    "uin": _state.get("uin"),
                    "cookie_updated_at": _state.get("cookie_updated_at"),
                    "keepalive_count": _state.get("keepalive_count"),
                    "last_error": _state.get("last_error"),
                }
            self._json({
                "keeper": keeper,
                "next_run": panel_mod.next_run_info(),
                "config": panel_mod.load_config(),
                "sys": panel_mod.server_info(),
                "bot": panel_mod.bot_state(),
            })
            return

        if path == "/api/logs":
            try:
                n = int(self._query("lines", "120"))
            except ValueError:
                n = 120
            n = max(10, min(n, 1000))
            where = self._query("where", "tail")
            lines = panel_mod.startup_view(n) if where == "head" else panel_mod.tail_log(n)
            self._json({"ok": True, "where": where, "lines": lines})
            return

        if path == "/api/blacklist":
            self._json({"ok": True, "users": panel_mod.list_blacklist()})
            return

        if path == "/api/devices":
            self._json({"ok": True, "devices": panel_mod.list_devices()})
            return

        if path == "/api/server-info":
            self._json(panel_mod.server_info())
            return

        if path in ("/", "/index.html"):
            if not self._authed():
                self._send_login()
                return
            try:
                html = DASHBOARD_FILE.read_text(encoding="utf-8")
            except Exception as e:
                self._send(500, "text/plain; charset=utf-8", f"控制台页面缺失：{e}".encode())
                return
            self._send(200, "text/html; charset=utf-8", html.encode())
            return

        self._send(404, "text/plain; charset=utf-8", "not found".encode())

    # ---------------- POST ----------------
    def do_POST(self):  # noqa: N802
        path = self._path()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        # ---- 登录 / 登出（不需要已登录）----
        if path == "/login":
            from urllib.parse import parse_qs

            form = parse_qs(raw.decode("utf-8", "replace"))
            user = (form.get("username") or [""])[0]
            pwd = (form.get("password") or [""])[0]
            remember = (form.get("remember") or [""])[0] == "1"

            if panel_mod.verify_login(user, pwd):
                days = 30 if remember else 1
                token = panel_mod.create_session(days)
                ua = self.headers.get("User-Agent") or ""
                cookies = [
                    f"{panel_mod.COOKIE_NAME}={token}; Path=/; Max-Age={days * 86400}; "
                    f"HttpOnly; SameSite=Lax"
                ]
                if remember:
                    # 信任设备：以后这台设备直接进，不用再输密码
                    dev_token = panel_mod.register_device(ua, days=730)
                    cookies.append(
                        f"{panel_mod.DEVICE_COOKIE}={dev_token}; Path=/; "
                        f"Max-Age={730 * 86400}; HttpOnly; SameSite=Lax"
                    )
                panel_mod.record_action(
                    "登录", f"用户 {user} 登录成功（会话 {days} 天"
                    f"{'，并信任该设备' if remember else ''}）"
                )
                extra = {"Location": "/"}
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                for c in cookies:
                    self.send_header("Set-Cookie", c)
                self.end_headers()
                return
            else:
                panel_mod.record_action("登录", f"登录失败（用户 {user or '空'}）")
                self._send_login("账号或密码不对")
            return

        if path == "/logout":
            panel_mod.drop_session(self._session_token())
            panel_mod.forget_device(self._device_token())   # 退出即不再自动登录
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.send_header("Set-Cookie", f"{panel_mod.COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly")
            self.send_header("Set-Cookie", f"{panel_mod.DEVICE_COOKIE}=; Path=/; Max-Age=0; HttpOnly")
            self.end_headers()
            return

        if self._need_auth():
            return

        if path == "/api/like-now":
            ok, msg = panel_mod.request_like_now()
            self._json({"ok": ok, "message": msg})
            return

        if path == "/api/restart":
            ok, msg = panel_mod.restart_bot()
            self._json({"ok": ok, "message": msg})
            return

        if path == "/api/auto":
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                enabled = bool(payload.get("enabled"))
            except Exception:
                enabled = True
            ok, msg = panel_mod.set_auto(enabled)
            self._json({"ok": ok, "message": msg, "auto": panel_mod.auto_enabled()})
            return

        if path == "/api/blacklist":
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            ok, msg = panel_mod.add_blacklist(payload.get("uin"))
            self._json({"ok": ok, "message": msg, "users": panel_mod.list_blacklist()})
            return

        if path == "/api/blacklist/remove":
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            ok, msg = panel_mod.remove_blacklist(payload.get("uin"))
            self._json({"ok": ok, "message": msg, "users": panel_mod.list_blacklist()})
            return

        if path == "/api/devices/revoke":
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            ok, msg = panel_mod.revoke_device(str(payload.get("id") or ""))
            self._json({"ok": ok, "message": msg})
            return

        if path == "/api/clear-dedup":
            ok, msg = panel_mod.clear_dedup()
            self._json({"ok": ok, "message": msg})
            return

        if path == "/api/test-cookie":
            ok, msg, out = panel_mod.test_cookie()
            self._json({"ok": ok, "message": msg, "output": out})
            return

        if path == "/api/config":
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                self._json({"ok": False, "message": "请求体不是合法 JSON"}, 400)
                return
            ok, msg = panel_mod.save_trigger(payload)
            self._json({"ok": ok, "message": msg})
            return

        self._send(404, "text/plain; charset=utf-8", "not found".encode())


def start_http() -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    panel_mod.record_action("面板", "控制台启动")
    log(f"控制台已启动：http://0.0.0.0:{HTTP_PORT}/  （账号见容器日志 /out/panel_auth.json）")


# --------------------------------------------------------------------------- #
# 浏览器逻辑
# --------------------------------------------------------------------------- #
async def is_logged_in(ctx, page) -> bool:
    """判定是否已登录：URL 不在登录域，且拿到了 qzone 的 p_skey"""
    try:
        if "ptlogin" in (page.url or "") or "login" in (page.url or "").lower():
            return False
        cookies = await ctx.cookies()
        return any(
            c.get("name") == "p_skey"
            and c.get("value")
            and "qq.com" in (c.get("domain") or "")
            for c in cookies
        )
    except Exception as e:
        log(f"判定登录状态失败：{e}")
        return False


async def capture_qr(page) -> bool:
    """把二维码存成清晰的图片。

    要点：
      · 优先**直接下载二维码原图**（<img id="qrlogin_img" src=".../ptqrshow?...">）。
        元素截图会把 111px 的原图按 CSS 尺寸重采样，插值后模块边缘糊掉，
        手机解不出来就会报"二维码已过期"，这是踩过的坑。
      · 下载失败才退回到元素截图（仍取面积最大的那个元素）。
      · 再不行整页截图兜底。
    页面端用 CSS `image-rendering: pixelated` 把原图**无损放大**显示。
    """
    # ---- 1) 用 canvas 把「页面上正在显示的」二维码按原始尺寸导出 ----
    # 为什么不直接下载 src / 不截屏：
    #   · 截屏：element.screenshot() 会把 111px 原图按 CSS 尺寸重采样，插值后糊掉，手机解不出
    #   · 下载 src：ptqrshow 每次请求都可能生成**新的**二维码会话，会把页面正在轮询的那张顶掉
    #   · canvas 导出：取的是浏览器里已经加载好的位图，原始尺寸、零插值、零新请求 ✓
    import base64 as _b64

    for frame in [page] + list(page.frames):
        for sel in ("#qrlogin_img", "img.qrImg", "img[src*='ptqrshow']"):
            try:
                el = frame.locator(sel).first
                if not await el.count() or not await el.is_visible():
                    continue
                data_url = await el.evaluate(
                    """el => {
                        const w = el.naturalWidth || el.width;
                        const h = el.naturalHeight || el.height;
                        if (!w || !h) return "";
                        const c = document.createElement('canvas');
                        c.width = w; c.height = h;
                        c.getContext('2d').drawImage(el, 0, 0);
                        try { return c.toDataURL('image/png'); } catch (e) { return ""; }
                    }"""
                )
                if not data_url or "," not in data_url:
                    continue
                png = _b64.b64decode(data_url.split(",", 1)[1])
                if len(png) < 400:
                    continue
                QR_FILE.write_bytes(png)
                with _lock:
                    _state["qr_bytes"] = len(png)
                    _state["qr_updated_at"] = datetime.now(CST).strftime("%H:%M:%S")
                return True
            except Exception:
                continue

    # ---- 2) 兜底：元素截图（取面积最大的） ----
    best = None
    for frame in [page] + list(page.frames):
        for sel in QR_SELECTORS:
            try:
                loc = frame.locator(sel)
                count = await loc.count()
                for i in range(min(count, 3)):
                    el = loc.nth(i)
                    if not await el.is_visible():
                        continue
                    box = await el.bounding_box()
                    if not box:
                        continue
                    area = box["width"] * box["height"]
                    if area < 40 * 40:
                        continue
                    if best is None or area > best[0]:
                        best = (area, el)
            except Exception:
                continue

    if best is not None:
        try:
            await best[1].screenshot(path=str(QR_FILE))
            set_state(qr_updated_at=datetime.now(CST).strftime("%H:%M:%S"))
            return True
        except Exception as e:
            log(f"二维码元素截图失败，改用整页：{e}")

    try:
        await page.screenshot(path=str(QR_FILE))
        set_state(qr_updated_at=datetime.now(CST).strftime("%H:%M:%S"))
        return False
    except Exception as e:
        log(f"二维码截图失败：{e}")
        return False


async def export_cookies(ctx) -> bool:
    """导出 Cookie 到文件（原子替换），供 AstrBot 插件读取"""
    try:
        cookies = await ctx.cookies()
    except Exception as e:
        log(f"读取浏览器 Cookie 失败：{e}")
        return False

    jar: dict[str, str] = {}
    for c in cookies:
        domain = c.get("domain") or ""
        if "qq.com" in domain and c.get("value"):
            jar[c["name"]] = c["value"]

    if not jar.get("p_skey") or not jar.get("uin"):
        log("Cookie 里缺少 p_skey/uin，暂不写出")
        return False

    parts = [f"{k}={jar[k]}" for k in COOKIE_ORDER if k in jar]
    parts += [f"{k}={v}" for k, v in jar.items() if k not in COOKIE_ORDER]
    text = "; ".join(parts)

    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = COOKIE_FILE.with_name(COOKIE_FILE.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(COOKIE_FILE)
        os.chmod(COOKIE_FILE, 0o600)
    except Exception as e:
        log(f"写入 Cookie 文件失败：{e}")
        return False

    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    set_state(
        cookie_updated_at=stamp,
        phase="logged_in",
        last_error=None,
    )
    log(f"Cookie 已更新并写出（{len(text)} 字节）→ {COOKIE_FILE}")
    panel_mod.record_action("Cookie", f"保活刷新成功（{len(text)} 字节）")
    return True


async def current_qr_src(page) -> str:
    """读取当前二维码图片的 src（用来判断二维码有没有换新的）"""
    for frame in [page] + list(page.frames):
        for sel in ("#qrlogin_img", "img.qrImg", "img[src*='ptqrshow']"):
            try:
                el = frame.locator(sel).first
                if await el.count():
                    src = await el.get_attribute("src")
                    if src:
                        return src
            except Exception:
                continue
    return ""


async def wait_for_login(page, ctx) -> None:
    """等扫码。

    关键：**不要反复刷新登录页**。
    每刷新一次页面 QQ 都会生成一个新二维码，用户正在扫的那张会被顶掉，
    表现就是"扫了说已过期"。所以这里只加载一次，之后靠轮询 Cookie 判断是否登录，
    二维码变了（ptlogin2 自己轮换）才重新抓图。
    """
    set_state(phase="waiting_scan")
    log("等待扫码登录（二维码页面见 HTTP 端口）…")

    try:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"打开登录页失败：{e}")

    last_src = ""
    reloaded_at = time.time()
    scan_warned = False

    while True:
        await asyncio.sleep(2)

        # 登录成功？
        if await is_logged_in(ctx, page):
            log("检测到登录成功")
            panel_mod.record_action("扫码", "扫码登录成功")
            return

        # 二维码换了才重新抓（避免把用户正在扫的那张存成旧的）
        src = await current_qr_src(page)
        if src and src != last_src:
            ok = await capture_qr(page)
            last_src = src
            with _lock:
                nbytes = _state.get("qr_bytes", 0)
            if ok or nbytes:
                log(f"二维码已更新（{nbytes} 字节）")
                scan_warned = False

        # 超过 5 分钟还没登上，重载一次页面拿新二维码（页面可能自己卡死了）
        if time.time() - reloaded_at > 300:
            log("等待超时 5 分钟，重载登录页以刷新二维码")
            try:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"重载登录页失败：{e}")
            last_src = ""
            reloaded_at = time.time()


async def keepalive_loop(page, ctx) -> None:
    while True:
        await asyncio.sleep(KEEPALIVE_SEC)
        try:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)

            if not await is_logged_in(ctx, page):
                log("⚠️ 会话已失效，需要重新扫码")
                panel_mod.record_action("Cookie", "会话失效，进入重新扫码状态")
                set_state(phase="waiting_scan", last_error="会话失效，等待重新扫码")
                await wait_for_login(page, ctx)
                await export_cookies(ctx)

            await export_cookies(ctx)
            with _lock:
                _state["keepalive_count"] += 1
                n = _state["keepalive_count"]
            log(f"保活第 {n} 次完成")
        except Exception as e:
            log(f"保活失败：{e}")
            set_state(last_error=str(e))


async def main() -> None:
    if not UIN:
        print("错误：必须设置环境变量 QZONE_UIN", file=sys.stderr)
        sys.exit(2)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    start_http()

    async with async_playwright() as p:
        log("启动 Chromium（headful + Xvfb，降低被识别概率）")
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
            # 3 倍渲染：二维码元素截图会放大 3 倍，手机才好扫
            device_scale_factor=3,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,900",
            ],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            # 先看看 profile 里的登录态还在不在
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            if await is_logged_in(ctx, page):
                log("复用已有登录态，无需扫码")
                await export_cookies(ctx)
            else:
                await wait_for_login(page, ctx)
                await export_cookies(ctx)
            await keepalive_loop(page, ctx)
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:  # 让容器日志能直接看到崩溃原因
        set_state(phase="error", last_error=str(e))
        log(f"致命错误：{e}")
        raise
