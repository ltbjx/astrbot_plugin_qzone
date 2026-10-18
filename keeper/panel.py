#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
控制台页面（由 keeper 服务的 HTTP 端口提供）

面板功能：
  · 显示「下次执行时间」+ 倒计时 + 上次执行结果
  · 一键点赞（立即跑一轮）
  · 修改执行周期 / 随机偏移 / 单轮条数 / 点赞间隔
  · 实时查看 AstrBot 日志

工作原理：不直接改 AstrBot 进程内存，而是
  · 读  /botdata/config/astrbot_plugin_qzone_config.json   （插件配置）
  · 写  同文件 + 在 /out 下放 reload.flag                  （插件轮询后热重载）
  · 写  /out/like_now.flag                                 （插件轮询后立即跑一轮）
  · 读  /out/schedule.json                                 （插件每次排期时写出）
  · 读  /botdata/logs/astrbot.log                          （AstrBot 文件日志）
所以不需要 docker.sock，也不需要重启 AstrBot。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import hashlib
import re
import http.client
import urllib.request
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))

BOT_DATA = Path(os.environ.get("BOT_DATA", "/botdata"))
OUT_DIR = Path(os.environ.get("OUT_DIR", "/out"))
CONFIG_PATH = BOT_DATA / "config" / "astrbot_plugin_qzone_config.json"
LOG_PATH = BOT_DATA / "logs" / "astrbot.log"

_auth_lock = threading.Lock()
_creds: dict[str, str] = {}


def init_auth() -> dict[str, str]:
    """初始化面板账号密码：优先环境变量，否则生成随机密码并落盘"""
    global _creds
    with _auth_lock:
        if _creds:
            return _creds

        user = os.environ.get("PANEL_USER", "admin").strip() or "admin"
        pwd = os.environ.get("PANEL_PASS", "").strip()

        if not pwd:
            state = OUT_DIR / "panel_auth.json"
            try:
                if state.is_file():
                    data = json.loads(state.read_text(encoding="utf-8"))
                    user = data.get("user") or user
                    pwd = data.get("pass") or ""
            except Exception:
                pass
            if not pwd:
                pwd = secrets.token_urlsafe(12)
                try:
                    state.write_text(
                        json.dumps({"user": user, "pass": pwd}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    os.chmod(state, 0o600)
                except Exception:
                    pass

        _creds = {"user": user, "pass": pwd}
        return _creds


def check_basic_auth(header: str | None) -> bool:
    """校验 HTTP Basic 认证头"""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(None, 1)[1]).decode("utf-8", "replace")
        user, _, pwd = raw.partition(":")
    except Exception:
        return False
    c = init_auth()
    return secrets.compare_digest(user, c["user"]) and secrets.compare_digest(
        pwd, c["pass"]
    )


# --------------------------------------------------------------------------- #
# 读写业务数据
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def save_trigger(payload: dict) -> tuple[bool, str]:
    """保存 trigger 配置并通知插件热重载"""
    cfg = load_config()
    if not cfg:
        return False, f"读不到配置文件：{CONFIG_PATH}"

    trigger = cfg.setdefault("trigger", {})

    def _int(name: str, lo: int, hi: int, label: str) -> tuple[int, str]:
        try:
            v = int(payload.get(name))
        except (TypeError, ValueError):
            raise ValueError(f"{label} 必须填数字")
        if not (lo <= v <= hi):
            raise ValueError(f"{label} 取值范围 {lo} ~ {hi}")
        return v, ""

    try:
        cron = str(payload.get("like_cron", "")).strip()
        if cron and len(cron.split()) != 5:
            raise ValueError("Cron 必须是 5 段（分 时 日 月 周），例如 0 */2 * * *；留空 = 关闭")

        off_min, _ = _int("like_offset_min", -3600, 0, "随机偏移下限（秒）")
        off_max, _ = _int("like_offset_max", 0, 7200, "随机偏移上限（秒）")
        num, _ = _int("like_num", 1, 50, "每轮条数")
        dmin, _ = _int("like_delay_min", 0, 300, "最小间隔（秒）")
        dmax, _ = _int("like_delay_max", 0, 600, "最大间隔（秒）")

        if off_min > off_max:
            raise ValueError("随机偏移下限不能大于上限")
        if dmin > dmax:
            raise ValueError("最小间隔不能大于最大间隔")
    except ValueError as e:
        return False, str(e)

    trigger["like_cron"] = cron
    trigger["like_offset_min"] = off_min
    trigger["like_offset_max"] = off_max
    trigger["like_offset"] = max(abs(off_min), abs(off_max)) or 900
    trigger["like_num"] = num
    trigger["like_delay_min"] = dmin
    trigger["like_delay_max"] = dmax

    try:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
    except Exception as e:
        return False, f"写入配置失败：{e}"

    record_action(
        "配置",
        f"cron={cron or '关闭'} 偏移={off_min}~{off_max}s 条数={num} 间隔={dmin}~{dmax}s",
    )

    # 通知插件热重载
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "reload.flag").write_text(str(time.time()), encoding="utf-8")
    except Exception as e:
        return False, f"配置已保存，但通知插件失败：{e}"

    return True, "已保存，插件将在 2 秒内重载并生成新排期"


def request_like_now() -> tuple[bool, str]:
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "like_now.flag").write_text(str(time.time()), encoding="utf-8")
        record_action("点赞", "已下发『立即点赞』指令")
        return True, "已下发指令，插件将在 2 秒内执行一轮点赞"
    except Exception as e:
        return False, f"下发失败：{e}"


def read_schedule() -> dict:
    try:
        return json.loads((OUT_DIR / "schedule.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def tail_log(lines: int = 120) -> list[str]:
    if not LOG_PATH.is_file():
        return ["（还没有日志文件：请确认 cmd_config.json 里 log_file_enable = true）"]
    try:
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-lines:]]
    except Exception as e:
        return [f"读取日志失败：{e}"]


def next_run_info() -> dict:
    """把排期文件转成前端好用的结构"""
    sched = read_schedule()
    if not sched:
        return {"known": False}
    try:
        target_ts = float(sched.get("target_ts") or 0)
    except (TypeError, ValueError):
        target_ts = 0
    now = datetime.now(CST).timestamp()
    return {
        "known": True,
        "target": sched.get("target"),
        "target_ts": target_ts,
        "base": sched.get("base"),
        "offset_minutes": sched.get("offset_minutes"),
        "cron": sched.get("cron"),
        "updated_at": sched.get("updated_at"),
        "seconds_left": max(0, int(target_ts - now)) if target_ts else 0,
    }


# --------------------------------------------------------------------------- #
# 系统操作：重启 / 开关 / 清空去重 / 测试 Cookie / 服务器连接信息
# --------------------------------------------------------------------------- #
COOKIE_FILE = OUT_DIR / "qzone_cookie.txt"
DEDUP_FILE = OUT_DIR / "liked_tids.json"
CHECK_TOOL = BOT_DATA / "plugins" / "astrbot_plugin_qzone" / "tools" / "qzone_check.py"
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
BOT_CONTAINER = os.environ.get("BOT_CONTAINER", "astrbot")


def _docker_request(method: str, path: str, timeout: int = 30) -> tuple[int, bytes]:
    """通过 unix socket 调 Docker Engine API（不需要 docker CLI）"""
    import socket as _socket

    class _UnixHTTPConnection(http.client.HTTPConnection):  # noqa: N801
        def __init__(self, sock_path: str, tmo: float):
            super().__init__("localhost", timeout=tmo)
            self.sock_path = sock_path

        def connect(self):
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect(self.sock_path)
            self.sock = s

    conn = _UnixHTTPConnection(DOCKER_SOCK, timeout)
    # 用 docker 自己的 API 版本前缀不需要，直接用 /v1.41 也行；这里走默认（会返回最新版本）
    conn.request(method, path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def docker_available() -> bool:
    return os.path.exists(DOCKER_SOCK)


def restart_bot() -> tuple[bool, str]:
    """重启 AstrBot 容器"""
    if not docker_available():
        record_action("重启", "失败：未挂载 docker.sock")
        return False, "没有挂载 docker.sock，无法重启容器（见 keeper/docker-compose.yml）"
    record_action("重启", f"已下发重启指令（容器 {BOT_CONTAINER}）")
    try:
        code, body = _docker_request("POST", f"/containers/{BOT_CONTAINER}/restart?t=15", 120)
    except Exception as e:
        record_action("重启", f"失败：{e}")
        return False, f"调用 Docker API 失败：{e}"
    if code in (204, 304):
        record_action("重启", "Docker 已接受重启")
        return True, f"已重启 {BOT_CONTAINER} 容器，约 20 秒后恢复"
    record_action("重启", f"失败：Docker API {code}")
    return False, f"Docker API 返回 {code}：{body[:200]!r}"


def bot_state() -> dict:
    """查询 AstrBot 容器状态"""
    if not docker_available():
        return {"ok": False, "reason": "未挂载 docker.sock"}
    try:
        code, body = _docker_request("GET", f"/containers/{BOT_CONTAINER}/json")
        if code != 200:
            return {"ok": False, "reason": f"HTTP {code}"}
        d = json.loads(body)
        st = d.get("State") or {}
        return {
            "ok": True,
            "status": st.get("Status"),
            "running": st.get("Running"),
            "started_at": st.get("StartedAt"),
            "restart_count": d.get("RestartCount"),
            "oom": st.get("OOMKilled"),
        }
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def set_auto(enabled: bool) -> tuple[bool, str]:
    """自动点赞总开关：关闭时把 like_cron 置空"""
    cfg = load_config()
    if not cfg:
        return False, "读不到插件配置"
    trigger = cfg.setdefault("trigger", {})
    if enabled:
        cron = trigger.get("like_cron") or "0 */2 * * *"
        trigger["like_cron"] = cron
        msg = f"已开启自动点赞（{cron}）"
    else:
        trigger["_like_cron_backup"] = trigger.get("like_cron") or "0 */2 * * *"
        trigger["like_cron"] = ""
        msg = "已关闭自动点赞（已记住原周期，下次开启自动恢复）"
    try:
        _write_config(cfg)
        _flag("reload.flag")
    except Exception as e:
        record_action("开关", f"保存失败：{e}")
        return False, f"保存失败：{e}"
    record_action("开关", msg)
    return True, msg


def auto_enabled() -> bool:
    t = (load_config().get("trigger") or {})
    return bool((t.get("like_cron") or "").strip())


def clear_dedup() -> tuple[bool, str]:
    try:
        if DEDUP_FILE.exists():
            DEDUP_FILE.unlink()
            record_action("去重", "已清空去重记录")
            return True, "已清空去重记录，下一轮会重新检查所有动态"
        return True, "去重记录本来就是空的"
    except Exception as e:
        return False, f"删除失败：{e}"


def test_cookie() -> tuple[bool, str, list[str]]:
    """用当前 Cookie 拉一次动态流，判断是否还有效"""
    import subprocess
    import sys

    if not COOKIE_FILE.is_file():
        return False, "还没有 Cookie 文件", []
    if not CHECK_TOOL.is_file():
        return False, f"找不到检查工具：{CHECK_TOOL}", []
    try:
        p = subprocess.run(
            [sys.executable, str(CHECK_TOOL), "--cookie-file", str(COOKIE_FILE), "--list"],
            capture_output=True, text=True, timeout=40,
        )
    except Exception as e:
        return False, f"执行失败：{e}", []
    out = (p.stdout or "").strip().splitlines()
    ok = p.returncode == 0 and any("已赞" in l or "未赞" in l for l in out)
    msg = "Cookie 有效" if ok else "Cookie 可能已失效（输出见下方）"
    record_action("Cookie", f"手动检测：{'有效' if ok else '疑似失效'}")
    return ok, msg, out[-12:]


def _write_config(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def _flag(name: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / name).write_text(str(time.time()), encoding="utf-8")


def server_info() -> dict:
    """给面板显示的服务器连接信息"""
    creds = init_auth()
    return {
        "host": os.environ.get("SERVER_HOST", ""),
        "ssh_port": os.environ.get("SSH_PORT", "22"),
        "ssh_user": os.environ.get("SSH_USER", "root"),
        "ssh_pass": os.environ.get("SSH_PASS", ""),
        "panel_user": creds.get("user"),
        "panel_pass": creds.get("pass"),
        "terminal_url": os.environ.get("TERMINAL_URL", ""),
        "docker": docker_available(),
        "auto_enabled": auto_enabled(),
        "cookie_updated_at": _stat_time(COOKIE_FILE),
        "dedup_count": _dedup_count(),
    }


def _stat_time(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, CST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _dedup_count() -> int:
    try:
        return len(json.loads(DEDUP_FILE.read_text(encoding="utf-8")))
    except Exception:
        return 0


def log_head(lines: int = 60) -> list[str]:
    """日志开头部分（服务启动时的日志）"""
    if not LOG_PATH.is_file():
        return ["（还没有日志文件）"]
    try:
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[:lines]]
    except Exception as e:
        return [f"读取日志失败：{e}"]


# --------------------------------------------------------------------------- #
# 操作记录：把面板上的每个动作（重启、点赞、改配置…）都记下来，
# 并合并进「启动日志」视图，方便事后回溯「什么时候重启过 / 谁触发了点赞」
# --------------------------------------------------------------------------- #
ACTION_LOG = OUT_DIR / "panel_actions.log"


def record_action(tag: str, msg: str) -> None:
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        with ACTION_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] [{tag}] {msg}\n")
    except Exception:
        pass


def read_actions(n: int = 80) -> list[str]:
    try:
        if not ACTION_LOG.is_file():
            return []
        with ACTION_LOG.open("r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
    except Exception as e:
        return [f"读取操作记录失败：{e}"]


def startup_view(limit: int = 60) -> list[str]:
    """「启动日志」视图：面板操作记录 + AstrBot 启动日志头部"""
    acts = read_actions(80)
    head = log_last_start(limit)
    out: list[str] = []
    if acts:
        out.append(f"════════ 面板操作记录（最近 {len(acts)} 条）════════")
        out.extend(acts)
        out.append("")
    out.append(f"════════ AstrBot 最近一次启动（{len(head)} 行）════════")
    out.extend(head)
    return out


def log_last_start(lines: int = 80) -> list[str]:
    """取「最近一次启动」的日志段。

    AstrBot 的日志文件是累积的（重启只是往后追加），所以"文件开头"是很久以前的历史。
    这里以最后一次出现「AstrBot started」为锚点，往前带一点上下文，
    这样「启动日志」看到的永远是**本次启动**的过程。
    """
    if not LOG_PATH.is_file():
        return ["（还没有日志文件：请确认 cmd_config.json 里 log_file_enable = true）"]
    try:
        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
            content = [ln.rstrip("\n") for ln in f.readlines()]
    except Exception as e:
        return [f"读取日志失败：{e}"]

    if not content:
        return ["（日志文件是空的）"]

    anchor = None
    for i, ln in enumerate(content):
        if "AstrBot started" in ln or "core_lifecycle:365" in ln:
            anchor = i
    if anchor is None:
        return content[-lines:]          # 没找到启动标记就退化成尾部

    start = max(0, anchor - 45)          # 往前带 45 行，包含启动横幅
    return content[start : start + lines]


# --------------------------------------------------------------------------- #
# 会话（Cookie 登录）
# 目的：把 HTTP Basic 弹窗换成真正的表单登录，这样 iOS/macOS 才会
#       「存储密码」，之后用面容 ID / Touch ID 自动填充。
# --------------------------------------------------------------------------- #
SESSION_FILE = OUT_DIR / "panel_sessions.json"
COOKIE_NAME = "qz_sess"
_sess_lock = threading.Lock()
_sessions: dict[str, float] = {}


def _load_sessions() -> dict[str, float]:
    global _sessions
    if _sessions:
        return _sessions
    try:
        if SESSION_FILE.is_file():
            raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            now = time.time()
            _sessions = {k: float(v) for k, v in raw.items() if float(v) > now}
    except Exception:
        _sessions = {}
    return _sessions


def _save_sessions() -> None:
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        alive = {k: v for k, v in _sessions.items() if v > now}
        SESSION_FILE.write_text(json.dumps(alive), encoding="utf-8")
        os.chmod(SESSION_FILE, 0o600)
    except Exception:
        pass


def verify_login(user: str, pwd: str) -> bool:
    c = init_auth()
    return secrets.compare_digest(user or "", c["user"]) and secrets.compare_digest(
        pwd or "", c["pass"]
    )


def create_session(days: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    with _sess_lock:
        _load_sessions()
        _sessions[token] = time.time() + days * 86400
        _save_sessions()
    return token


def check_session(token: str | None) -> bool:
    if not token:
        return False
    with _sess_lock:
        _load_sessions()
        exp = _sessions.get(token)
        if not exp:
            return False
        if exp < time.time():
            _sessions.pop(token, None)
            _save_sessions()
            return False
        return True


def drop_session(token: str | None) -> None:
    if not token:
        return
    with _sess_lock:
        _load_sessions()
        if _sessions.pop(token, None) is not None:
            _save_sessions()


# --------------------------------------------------------------------------- #
# 信任设备（自动登录）
# 首次输对密码时，给这台设备发一个长期 token（存在 HttpOnly Cookie 里），
# 并把「设备指纹（User-Agent）」一起记下来。之后同一台设备、同一个 UA
# 再来就直接进，不用再输密码。
# --------------------------------------------------------------------------- #
DEVICE_FILE = OUT_DIR / "panel_devices.json"
DEVICE_COOKIE = "qz_dev"
_dev_lock = threading.Lock()
_devices: dict[str, dict] = {}


def _now_iso() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def ua_label(ua: str) -> str:
    """把 User-Agent 变成人能看懂的名字"""
    u = ua or ""
    if "iPhone" in u:
        plat = "iPhone"
    elif "iPad" in u:
        plat = "iPad"
    elif "Android" in u:
        plat = "Android"
        if m := re.search(r"Android [\d.]+; ([^;)]+)", u):
            plat = m.group(1).strip()
    elif "Macintosh" in u:
        plat = "Mac"
    elif "Windows" in u:
        plat = "Windows"
    else:
        plat = "未知设备"

    if "MicroMessenger" in u:
        browser = "微信"
    elif "QQBrowser" in u or "MQQBrowser" in u:
        browser = "QQ浏览器"
    elif "Edg/" in u:
        browser = "Edge"
    elif "CriOS" in u or "Chrome/" in u:
        browser = "Chrome"
    elif "Safari" in u:
        browser = "Safari"
    else:
        browser = "浏览器"
    return f"{plat} · {browser}"


def _load_devices() -> dict[str, dict]:
    global _devices
    if _devices:
        return _devices
    try:
        if DEVICE_FILE.is_file():
            now = time.time()
            raw = json.loads(DEVICE_FILE.read_text(encoding="utf-8"))
            _devices = {k: v for k, v in raw.items() if float(v.get("expires") or 0) > now}
    except Exception:
        _devices = {}
    return _devices


def _save_devices() -> None:
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        alive = {k: v for k, v in _devices.items() if float(v.get("expires") or 0) > now}
        DEVICE_FILE.write_text(json.dumps(alive, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(DEVICE_FILE, 0o600)
    except Exception:
        pass


def register_device(ua: str, days: int = 730) -> str:
    """登录成功时调用：登记这台设备，返回要写进 Cookie 的明文 token"""
    token = secrets.token_urlsafe(32)
    dev_id = secrets.token_hex(4)
    rec = {
        "id": dev_id,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "ua": (ua or "")[:300],
        "label": ua_label(ua),
        "created_at": _now_iso(),
        "last_seen": _now_iso(),
        "expires": time.time() + days * 86400,
    }
    with _dev_lock:
        _load_devices()
        _devices[dev_id] = rec
        _save_devices()
    record_action("设备", f"登记信任设备：{rec['label']}（{days} 天）")
    return token


def check_device(token: str | None, ua: str) -> bool:
    """Cookie 里的设备 token 是否有效，且 UA 与登记时一致"""
    if not token:
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _dev_lock:
        _load_devices()
        for dev_id, rec in list(_devices.items()):
            if not secrets.compare_digest(rec.get("token_sha256", ""), digest):
                continue
            if float(rec.get("expires") or 0) < time.time():
                _devices.pop(dev_id, None)
                _save_devices()
                return False
            if (rec.get("ua") or "") != (ua or "")[:300]:
                record_action("设备", f"设备指纹不匹配，拒绝自动登录：{rec.get('label')}")
                return False
            rec["last_seen"] = _now_iso()
            _devices[dev_id] = rec
            _save_devices()
            return True
    return False


def list_devices() -> list[dict]:
    with _dev_lock:
        _load_devices()
        return sorted(
            (
                {
                    "id": d.get("id"),
                    "label": d.get("label"),
                    "ua": d.get("ua"),
                    "created_at": d.get("created_at"),
                    "last_seen": d.get("last_seen"),
                    "expires": datetime.fromtimestamp(
                        float(d.get("expires") or 0), CST
                    ).strftime("%Y-%m-%d"),
                }
                for d in _devices.values()
            ),
            key=lambda x: x.get("last_seen") or "",
            reverse=True,
        )


def revoke_device(dev_id: str) -> tuple[bool, str]:
    with _dev_lock:
        _load_devices()
        rec = _devices.pop(dev_id, None)
        if rec:
            _save_devices()
            record_action("设备", f"撤销信任设备：{rec.get('label')}")
            return True, f"已撤销 {rec.get('label')}"
        return False, "没有这台设备的记录"


def forget_device(token: str | None) -> None:
    """退出登录时移除这台设备的信任记录"""
    if not token:
        return
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _dev_lock:
        _load_devices()
        for dev_id, rec in list(_devices.items()):
            if secrets.compare_digest(rec.get("token_sha256", ""), digest):
                _devices.pop(dev_id, None)
                _save_devices()
                record_action("设备", f"退出登录，已移除信任：{rec.get('label')}")
                break


# --------------------------------------------------------------------------- #
# 点赞黑名单：名单里的 QQ 发动态一律不点赞
# 存成 plugin_data 下的 like_blacklist.json，插件每轮读取，控制台改完立即生效
# --------------------------------------------------------------------------- #
BLACKLIST_FILE = OUT_DIR / "like_blacklist.json"
POSTS_DB = OUT_DIR / "posts_5.db"


def _nickname_of(uin: str) -> str:
    """从插件自己的说说库里查最近一次用过的昵称，方便辨认"""
    try:
        import sqlite3

        if not POSTS_DB.is_file():
            return ""
        con = sqlite3.connect(f"file:{POSTS_DB}?immutable=1", uri=True)
        row = con.execute(
            "SELECT name FROM posts WHERE uin = ? ORDER BY create_time DESC LIMIT 1",
            (int(uin),),
        ).fetchone()
        con.close()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def list_blacklist() -> list[dict]:
    try:
        raw = json.loads(BLACKLIST_FILE.read_text(encoding="utf-8")) if BLACKLIST_FILE.is_file() else []
    except Exception:
        raw = []
    out = []
    for uin in raw:
        uin = str(uin).strip()
        if not uin:
            continue
        out.append({"uin": uin, "nickname": _nickname_of(uin)})
    return out


def _write_blacklist(uins: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BLACKLIST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(uins, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(BLACKLIST_FILE)


def add_blacklist(uin: str) -> tuple[bool, str]:
    uin = str(uin or "").strip()
    if not uin.isdigit() or len(uin) < 5:
        return False, "请填正确的 QQ 号（纯数字）"
    cur = [x["uin"] for x in list_blacklist()]
    if uin in cur:
        return False, f"{uin} 已经在黑名单里了"
    cur.append(uin)
    try:
        _write_blacklist(cur)
    except Exception as e:
        return False, f"写入失败：{e}"
    nick = _nickname_of(uin)
    record_action("黑名单", f"加入 {uin}{('（' + nick + '）') if nick else ''}")
    return True, f"已加入黑名单：{uin}" + (f"（{nick}）" if nick else "")


def remove_blacklist(uin: str) -> tuple[bool, str]:
    uin = str(uin or "").strip()
    cur = [x["uin"] for x in list_blacklist()]
    if uin not in cur:
        return False, f"{uin} 不在黑名单里"
    cur.remove(uin)
    try:
        _write_blacklist(cur)
    except Exception as e:
        return False, f"写入失败：{e}"
    record_action("黑名单", f"移除 {uin}")
    return True, f"已移出黑名单：{uin}"
