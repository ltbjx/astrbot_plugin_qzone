#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qzone_check.py — QQ空间「点赞状态」自查工具

为什么需要它？
    点赞接口 internal_dolike_app 对「首次点赞」「重复点赞」「参数被忽略」三种情况
    返回完全一样的 {"code":0,"message":"succ"}，所以：
        · 日志说"已点赞" —— 不可信
        · 接口返回 succ  —— 不可信
    真正可信的是好友动态流 HTML 里点赞按钮携带的 data-islike / data-likecnt 字段。
    本工具就靠读它来判断赞到底有没有真的点上。

用法：
    # Cookie 三种给法（任选其一）
    export QZONE_COOKIE='uin=o123456789; skey=@xxx; p_skey=xxx; ...'
    python3 qzone_check.py --cookie 'uin=o...; p_skey=...'
    python3 qzone_check.py --cookie-file cookie.txt

    python3 qzone_check.py --list                 # 列出动态流里每条动态的真实点赞状态
    python3 qzone_check.py --like-latest          # 点赞最新一条「未赞」的动态，并自动回查
    python3 qzone_check.py --like <unikey>        # 点赞指定的 unikey
    python3 qzone_check.py --list --json          # 以 JSON 输出，便于脚本处理

退出码：
    0 正常；1 参数/网络错误；2 点赞后回查发现没生效

依赖：仅 Python 标准库（3.8+）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request


FEED_URL = (
    "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"
)
DOLIKE_URL = (
    "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


def build_ssl_context(insecure: bool = False) -> ssl.SSLContext:
    """
    构建 SSL 上下文。

    很多国内 Python 环境（尤其是 macOS 官方安装包）默认 CA 库是空的，
    会直接报 CERTIFICATE_VERIFY_FAILED。这里优先用 certifi 的证书包，
    实在不行才让调用方走 --insecure。
    """
    if insecure:
        return ssl._create_unverified_context()
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def calc_gtk(p_skey: str) -> str:
    """由 p_skey 计算 g_tk（QZone 系通用算法）"""
    h = 5381
    for ch in p_skey:
        h += (h << 5) + ord(ch)
    return str(h & 0x7FFFFFFF)


def parse_cookie(raw: str) -> dict[str, str]:
    """
    解析 Cookie 字符串。

    兼容以下常见粘贴姿势：
      · 单行：uin=o123; skey=abc; p_skey=def
      · 整行：Cookie: uin=o123; p_skey=def
      · 多行（从 DevTools 表格里复制出来的换行版）
    """
    raw = (raw or "").strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[len("cookie:") :].strip()

    # 换行 / 制表符统一成分号，并压掉多余的分号，避免出现空字段中断解析
    raw = re.sub(r"[\r\n\t]+", ";", raw)
    raw = re.sub(r";\s*;+", ";", raw)

    jar: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        if key:
            jar[key] = value
    return jar


def unescape_qzone_html(text: str) -> str:
    """动态流返回的是「JSON 里嵌 HTML」，引号斜杠都被转义了，先还原再解析"""
    for a, b in (
        ("\\\\x22", '"'),
        ("\\x22", '"'),
        ("\\\\/", "/"),
        ("\\/", "/"),
        ('\\"', '"'),
    ):
        text = text.replace(a, b)
    return text


# --------------------------------------------------------------------------- #
# API 客户端
# --------------------------------------------------------------------------- #
class QzoneClient:
    def __init__(self, cookie_raw: str, timeout: int = 20, insecure: bool = False):
        jar = parse_cookie(cookie_raw)
        if not jar:
            raise ValueError("Cookie 为空")

        uin_text = jar.get("uin", "")
        uin_text = uin_text[1:] if uin_text[:1].lower() == "o" else uin_text
        if not uin_text.isdigit():
            raise ValueError("Cookie 中缺少合法 uin（应形如 uin=o123456789）")

        p_skey = jar.get("p_skey") or jar.get("skey")
        if not p_skey:
            raise ValueError("Cookie 中缺少 p_skey / skey")

        self.uin = int(uin_text)
        self.p_skey = p_skey
        self.g_tk = calc_gtk(p_skey)
        self.timeout = timeout
        self.ssl_context = build_ssl_context(insecure)
        # 除 uin/skey/p_skey 外的字段也一并带上，贴合浏览器行为
        self.extra = {
            k: v for k, v in jar.items() if k.lower() not in ("uin", "skey", "p_skey")
        }
        self.header_cookie = self._build_cookie_header()

    def _build_cookie_header(self) -> str:
        jar = dict(self.extra)
        jar.update(
            {"uin": f"o{self.uin}", "skey": self.p_skey, "p_skey": self.p_skey}
        )
        return "; ".join(f"{k}={v}" for k, v in jar.items())

    def request(self, url: str, data: dict | None = None) -> str:
        headers = {
            "User-Agent": UA,
            "Cookie": self.header_cookie,
            "Referer": f"https://user.qzone.qq.com/{self.uin}",
            "Origin": "https://user.qzone.qq.com",
        }
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(
            req, timeout=self.timeout, context=self.ssl_context
        ) as resp:
            return resp.read().decode("utf-8", "replace")

    # ---------------- 读：好友动态流 ----------------
    def fetch_feed(self) -> list[dict]:
        params = {
            "uin": self.uin,
            "scope": 0,
            "view": 1,
            "filter": "all",
            "flag": 1,
            "applist": "all",
            "pagenum": 1,
            "aisortEndTime": 0,
            "aisortOffset": 0,
            "aisortBeginTime": 0,
            "begintime": 0,
            "format": "json",
            "g_tk": self.g_tk,
            "useutf8": 1,
            "outputhtmlfeed": 1,
        }
        text = unescape_qzone_html(
            self.request(FEED_URL + "?" + urllib.parse.urlencode(params))
        )
        return self._parse_like_buttons(text)

    @staticmethod
    def _parse_like_buttons(text: str) -> list[dict]:
        """从动态流 HTML 里提取每条动态的点赞按钮状态"""
        items: list[dict] = []
        seen: set[str] = set()
        for m in re.finditer(r"qz_like_btn_v3", text):
            seg = text[m.start() : m.start() + 500]
            unikey = re.search(r'data-unikey="([^"]+)"', seg)
            if not unikey:
                continue
            key = unikey.group(1)
            if key in seen:
                continue
            seen.add(key)

            islike = re.search(r'data-islike="(\d)"', seg)
            likecnt = re.search(r'data-likecnt="(\d+)"', seg)
            owner = re.search(r"user\.qzone\.qq\.com/(\d+)/mood/([0-9a-zA-Z]+)", key)
            items.append(
                {
                    "unikey": key,
                    "islike": int(islike.group(1)) if islike else None,
                    "likecnt": int(likecnt.group(1)) if likecnt else None,
                    "owner": owner.group(1) if owner else None,
                    "tid": owner.group(2) if owner else None,
                }
            )
        return items

    # ---------------- 写：点赞 ----------------
    def like(self, unikey: str) -> dict:
        owner = re.search(r"user\.qzone\.qq\.com/(\d+)/mood/([0-9a-zA-Z]+)", unikey)
        if not owner:
            raise ValueError(f"unikey 格式不对：{unikey}")
        fid = owner.group(2)
        data = {
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
            "opuin": self.uin,
            # ★ 关键：unikey/curkey 必须保持调用方传入的原样（浏览器用的是 http:// 开头）
            "unikey": unikey,
            "curkey": unikey,
            "appid": 311,
            "from": 1,
            "typeid": 0,
            "abstime": int(time.time()),
            "fid": fid,
            "active": 0,
            "format": "json",
            "fupdate": 1,
        }
        raw = self.request(DOLIKE_URL + "?g_tk=" + self.g_tk, data)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def _print_items(items: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    if not items:
        print("⚠️  动态流里没解析到任何动态（可能没有好友动态，或 Cookie 已失效）")
        return
    liked = sum(1 for i in items if i["islike"] == 1)
    print(f"好友动态流：共 {len(items)} 条，其中已赞 {liked} 条\n")
    for i in items:
        flag = "✅ 已赞" if i["islike"] == 1 else "❌ 未赞"
        cnt = "?" if i["likecnt"] is None else i["likecnt"]
        print(f"  {flag}  likecnt={cnt:<4} {i['unikey']}")


def cmd_list(client: QzoneClient, as_json: bool) -> int:
    items = client.fetch_feed()
    _print_items(items, as_json)
    return 0


def cmd_like(client: QzoneClient, unikey: str) -> int:
    print(f"→ 点赞 {unikey}")
    resp = client.like(unikey)
    print(f"  接口返回：{json.dumps(resp, ensure_ascii=False)}")
    print("  （注意：返回 succ 并不代表成功，继续回查……）")

    time.sleep(3)
    items = client.fetch_feed()
    target = next((i for i in items if i["unikey"] == unikey), None)
    if target is None:
        print("  ⚠️  回查时目标动态已不在动态流里，无法确认")
        return 1
    if target["islike"] == 1:
        print(f"  ✅ 回查确认：已赞（likecnt={target['likecnt']}）—— 点赞真实生效")
        return 0
    print(f"  ❌ 回查确认：仍是未赞（likecnt={target['likecnt']}）—— 点赞没有生效！")
    print("     常见原因：unikey 使用了 https:// 前缀（必须与原样一致，通常是 http://）")
    return 2


def cmd_like_latest(client: QzoneClient) -> int:
    items = client.fetch_feed()
    pending = [i for i in items if i["islike"] == 0]
    if not pending:
        print("动态流里没有「未赞」的动态，无需操作")
        return 0
    target = pending[0]
    print(f"选中最新一条未赞动态（owner={target['owner']} tid={target['tid']}）")
    return cmd_like(client, target["unikey"])


# --------------------------------------------------------------------------- #
def load_cookie(args) -> str:
    if args.cookie:
        return args.cookie
    if args.cookie_file:
        with open(args.cookie_file, encoding="utf-8") as f:
            return f.read()
    env = os.environ.get("QZONE_COOKIE")
    if env:
        return env
    sys.exit(
        "错误：没有提供 Cookie。\n"
        "  用法：export QZONE_COOKIE='uin=o...; skey=...; p_skey=...'\n"
        "    或  python3 qzone_check.py --cookie '...'\n"
        "    或  python3 qzone_check.py --cookie-file cookie.txt"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="QQ空间点赞状态自查工具（判据来自动态流的 data-islike 字段）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--cookie", help="Cookie 字符串")
    ap.add_argument("--cookie-file", help="从文件读取 Cookie")
    ap.add_argument("--list", action="store_true", help="列出动态流每条动态的真实点赞状态")
    ap.add_argument("--like", metavar="UNIKEY", help="点赞指定 unikey 并回查")
    ap.add_argument("--like-latest", action="store_true", help="点赞最新一条未赞动态并回查")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（配合 --list）")
    ap.add_argument("--timeout", type=int, default=20, help="请求超时秒数（默认 20）")
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="跳过 HTTPS 证书校验（仅在本地 CA 库损坏、且你信任网络环境时使用）",
    )
    args = ap.parse_args()

    try:
        client = QzoneClient(
            load_cookie(args), timeout=args.timeout, insecure=args.insecure
        )
    except ValueError as e:
        print(f"错误：{e}")
        return 1

    print(f"登录 QQ：{client.uin}   g_tk={client.g_tk}\n")

    try:
        if args.like:
            return cmd_like(client, args.like)
        if args.like_latest:
            return cmd_like_latest(client)
        return cmd_list(client, args.json)
    except urllib.error.HTTPError as e:
        print(f"HTTP 错误：{e.code} {e.reason}")
        if e.code in (401, 403):
            print("  → 通常是 Cookie 已失效，请重新获取")
        return 1
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        print(f"网络错误：{reason}")
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            print("  → 本地 Python 缺少 CA 证书。装一个 certifi 即可：pip install certifi")
            print("  → 或临时跳过校验：加 --insecure")
        return 1


if __name__ == "__main__":
    sys.exit(main())
