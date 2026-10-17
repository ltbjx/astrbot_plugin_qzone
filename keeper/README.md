# QQ空间 Cookie 保活服务（qzone-keeper）

## 它解决什么问题

本仓库的**纯 Cookie 模式**最大的痛点是：`p_skey` / `skey` 是**会话级 Cookie**，闲置后会失效。
实测最短的一次只撑了 **约 2.5 小时**，点赞就开始报：

```
[AutoLike] 任务执行失败: 登录失效，重试失败
```

人工重新复制 Cookie 很烦，而且你关掉浏览器、电脑休眠唤醒、账号在别处重新登录，都可能让它提前失效。

**qzone-keeper** 在服务器上常驻一个真实 Chromium，替你把这件麻烦事自动化：

```
       你手机扫码（仅一次）
              │
              ▼
   ┌──────────────────────────┐
   │  qzone-keeper 容器        │
   │  Chromium（headful+Xvfb）  │
   │  ① 打开空间登录页 → 暴露二维码
   │  ② 每 10 分钟访问空间保活
   │  ③ 导出最新 Cookie → 文件  │
   └───────────┬──────────────┘
               │ qzone_cookie.txt（原子写入）
               ▼
   AstrBot 插件（配置 qzone_cookie_file 指向它）
               │
               ▼
        每 2 小时自动点赞
```

**关键点：登录的是「QQ空间网页版」，不是 QQ 客户端** —— 所以依然**不会挤掉你电脑 / 手机上的 QQ**。

## 目录结构

```
keeper/
├── Dockerfile            # python:3.11-slim + Xvfb + 中文字体 + Playwright Chromium
├── docker-compose.yml    # 一键起服务
├── keeper.py             # 保活主程序（含二维码 HTTP 服务）
└── README.md
```

## 快速开始

### 1. 配置并构建

```bash
cd keeper
cat > .env <<'EOF'
QZONE_UIN=你的QQ号
KEEPALIVE_SEC=600        # 保活间隔（秒），默认 10 分钟
EOF

docker compose build     # 首次会下载 Chromium，几分钟
docker compose up -d
```

> 国内拉不动 `cdn.playwright.dev` 时，把 Dockerfile 里 `PLAYWRIGHT_DOWNLOAD_HOST`
> 那行的注释取消（走 npmmirror 镜像）再 build。

### 2. 开放端口

云厂商安全组放行入站 **6186**（扫码页面用）。不需要长期开着，扫完可以关掉。

### 3. 扫码登录（只需一次）

浏览器打开：

```
http://<服务器IP>:6186/
```

用手机 QQ 扫码。页面每 30 秒自动刷新二维码，过期不用管。

扫码成功后页面会变成「✅ 已登录，Cookie 保活中」。

### 4. 让插件用上这个 Cookie

AstrBot 面板 → 插件 → QQ空间 → 配置：

| 配置项 | 填什么 |
|---|---|
| `qzone_cookie_file` | `/AstrBot/data/plugin_data/astrbot_plugin_qzone/qzone_cookie.txt` |

**`qzone_cookie` 可以留空。** 插件启动后会自动从这个文件读取，保活服务写得越勤，插件跟得越紧，**不需要重启 AstrBot**。

> 容器里 AstrBot 看到的路径是 `/AstrBot/data/...`，宿主机上是
> `/opt/qzone-bot/astrbot/data/...`。compose 里已经把这个目录挂给 keeper 当 `/out`。

### 5. 验证

```bash
# 保活服务状态
curl -s http://127.0.0.1:6186/status | head -20

# Cookie 文件是否已生成
ls -l /opt/qzone-bot/astrbot/data/plugin_data/astrbot_plugin_qzone/qzone_cookie.txt

# 插件是否用上了（日志里会出现"已从文件加载 QQ空间 Cookie"）
docker logs astrbot 2>&1 | grep "Cookie"

# 点赞是否恢复
docker logs --since 10m astrbot 2>&1 | grep -E "本轮检查|已点赞|登录失效"
```

## HTTP 接口

| 路径 | 说明 |
|---|---|
| `/` | 二维码页面（手机扫这个），30 秒自动刷新 |
| `/qr.png` | 当前二维码图片 |
| `/status` | 运行状态 JSON（阶段、Cookie 更新时间、保活次数、日志尾部） |
| `/health` | 已登录返回 200，否则 503 —— 适合给监控 / 告警用 |

`/status` 示例：

```json
{
  "phase": "logged_in",
  "uin": "123456789",
  "qr_updated_at": "09:46:31",
  "cookie_updated_at": "2026-10-17 09:47:02",
  "keepalive_count": 12,
  "last_error": null
}
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `QZONE_UIN` | 必填 | 要保活的 QQ 号 |
| `KEEPALIVE_SEC` | `600` | 保活间隔（秒）。不建议低于 300 |
| `HTTP_PORT` | `6186` | 二维码页面端口 |
| `OUT_DIR` | `/out` | Cookie 输出目录 |
| `STATE_DIR` | `/data` | 浏览器 profile 与状态文件（**必须持久化**） |

## 原理与注意事项

1. **headful + Xvfb**：容器里用 `xvfb-run` 提供虚拟显示，Chromium 以有头模式运行 —— 比 `headless=True` 更不容易被识别。
2. **profile 持久化**：`./state` 目录保存浏览器 profile，容器重启不必重新扫码。
   （注意：会话 Cookie 能否跨重启恢复取决于 Chromium 的持久化策略，**如果容器重建了，可能仍需重新扫码**。）
3. **保活方式**：定时 `goto` 空间首页，让服务端认为会话仍在使用。这既是保活，也是**探活** —— 一旦发现会话失效会自动回到等待扫码状态。
4. **同一个 QQ 号不要在多处反复登录空间网页版**，否则会话可能互相顶掉。
5. **资源占用**：Chromium 常驻约 300~500 MB 内存。1 核 1G 的小机器会很吃力，建议 2G 以上。
6. **仍然存在风控可能**：任何自动化访问都有风险。请保持低频（默认 10 分钟一次已经很克制）。

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 打开 6186 没反应 | 安全组没放行 / 容器没起来 | `docker compose ps`、`docker logs qzone-keeper` |
| 页面一直「启动中」 | Chromium 启动失败 | 看 `state/keeper.log`，多半是内存不足 |
| 二维码是整页截图、扫不了 | 没匹配到二维码元素 | 提 Issue 并附上登录页的 HTML 结构，我加选择器 |
| 扫码后仍是「等待扫码」 | 登录检测失败 | 看 `/status` 的 `last_error`；可能是页面结构变了 |
| Cookie 文件不生成 | 没拿到 `p_skey`/`uin` | `docker logs qzone-keeper \| grep Cookie` |
| 插件没读文件 | `qzone_cookie_file` 路径写错 | 容器内路径必须是 `/AstrBot/data/...` |
