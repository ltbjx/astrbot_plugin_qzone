<div align="center">

# astrbot_plugin_qzone · 增强 Fork

_✨ [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 QQ空间对接插件 ✨_

[![AstrBot](https://img.shields.io/badge/AstrBot-4.x-orange)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-GPL--3.0-green)](./LICENSE)

**修复了上游「点赞显示成功但实际无效」的静默失效问题 · 支持不登录 QQ 客户端的纯 Cookie 模式 · 支持只点赞不评论**

</div>

---

## 这个 Fork 解决什么问题

本仓库基于 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) v3.1.3，在**真机联调**中定位并修复了 4 个会导致「自动点赞完全不可用」的问题。上游 README 里那句 **「点赞说说（接口显示成功，但实测点赞无效）」**，根因就在这里：

| # | 问题 | 后果 | 状态 |
|---|---|---|---|
| 1 | 点赞接口的 `unikey`/`curkey` 用了 `https://`，而浏览器实际用 `http://` | 接口返回 `{"code":0,"message":"succ"}`，**但服务端直接忽略，赞根本没点上** | ✅ 已修复（`like_use_http_key`，**默认开启**） |
| 2 | `core/qzone/client.py` 写死 `parsed["data"].get("ret")`，而点赞接口的 `data` 是**字符串** `""` | 抛 `'str' object has no attribute 'get'`，**点赞流程整体崩溃** | ✅ 已修复 |
| 3 | 定时任务用「一次性任务链」却没设 `misfire_grace_time` | 任务只要晚 1 秒执行就被跳过，**下一轮永远不会被排上 → 点赞永久静默失效** | ✅ 已修复 |
| 4 | 调完点赞接口不看返回值，直接打印「已点赞」 | 失败也报成功，**假日志** | ✅ 已修复（失败抛错） |

> 完整的排查过程、证据链和复现方法见 **[docs/03-点赞失效根因报告.md](docs/03-点赞失效根因报告.md)**。这是本 Fork 最值得阅读的一篇文档。

## 新增能力

- 🍪 **纯 Cookie 模式（免登录 QQ 客户端）**
  服务器上**不需要 NapCat、不需要扫码登录 QQ**。只需从浏览器复制一次 QQ空间 Cookie，插件直接调空间接口。
  → 不会像 NapCat 那样以「电脑端」身份登录，**不会把你本机 / 手机上的 QQ 挤下线**。

- ♻️ **Cookie 自动保活（`keeper/`）**
  Cookie 是会话级的，闲置后会失效（实测最短约 2.5 小时就报「登录失效」）。
  `keeper/` 是一个常驻侧车服务：服务器上跑一个真实 Chromium，**手机扫码一次**，
  之后每 10 分钟访问空间保活，并把最新 Cookie 原子写入文件，插件自动读取 —— **从此不用人工换 Cookie**。
  依旧不登录 QQ 客户端，不影响你电脑 / 手机上的 QQ。详见 [keeper/README.md](keeper/README.md)。

- 👍 **只点赞不评论模式（`like_only`）**
  上游的自动点赞**挂在自动评论任务里**：会先用 LLM 给好友动态挨个评论，评论失败还会跳过点赞。
  开启后走独立的 `AutoLike` 任务：**只点赞、不评论、不调 LLM**，并带**本地去重**（不会重复点同一条）与**随机间隔**（防风控）。

- 🔍 **点赞状态自查工具（`tools/qzone_check.py`）**
  不依赖日志自说自话，直接从动态流里读 `data-islike` 字段，**验证赞到底有没有真的点上**。

- 🛠 **配置项全部可视化**：所有新增开关都在 AstrBot 插件配置面板里，不用改代码。

## 特性一览（含上游原有功能）

- 定时自动点赞好友动态（本 Fork 新增的独立任务）
- 只点赞不评论 / 评论时顺便点赞，两种模式可选
- 查看 / 评论 / 点赞说说，支持 `@某人` 与序号、范围语法（`2~5`）
- 定时自动发说说、AI 写说说（需配置 LLM）
- 校园表白墙：投稿、匿名投稿、审核过稿 / 拒稿
- 查看空间访客
- 说说卡片图片渲染（内置渲染器）

---

## ⚠️ 使用前必读

1. **本项目为非官方第三方工具**，与腾讯 / QQ空间官方无关。使用自动化脚本访问 QQ空间**存在账号被限流或风控的风险**，请自行评估并自担后果。
2. **Cookie 等于你的空间登录凭证**。不要提交到 Git、发到群里或粘贴到公开场所。`.gitignore` 已默认排除相关文件。
3. 请**合理设置频率**。默认「每 2 小时一次、每次最多 20 条、每条间隔 8~20 秒随机」是相对保守的参数，不建议调得更激进。
4. **不要用它去刷陌生人的空间**。给好友点赞是社交行为，滥用会给你自己带来麻烦。

## 🚀 快速开始

### 方式一：纯 Cookie 模式（推荐，服务器上不登 QQ）

**适合**：只想让它自动点赞，不希望服务器登录 QQ（避免挤掉自己电脑 / 手机的 QQ）。

**第 1 步：安装插件**

```bash
cd /AstrBot/data/plugins
git clone https://github.com/ltbjx/astrbot_plugin_qzone

# 安装依赖（国内建议走清华源）
pip install -r astrbot_plugin_qzone/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 重启 AstrBot
```

> Docker 部署的话：`docker exec astrbot pip install -i https://pypi.tuna.tsinghua.edu.cn/simple Pillow json5 "apilmoji[tqdm]>=0.3.0,<1.0.0" && docker restart astrbot`

**第 2 步：拿一份 QQ空间 Cookie**

1. 电脑浏览器打开 `https://user.qzone.qq.com/<你的QQ号>`，确认已登录
2. 按 `F12` → **Network / 网络** → 按 `F5` 刷新
3. 右键任意一条发往 `user.qzone.qq.com` 的请求 → **Copy → Copy as cURL**
4. 从里面取出 `cookie:` 那一行的值（**必须含 `p_skey` 和 `uin`**）

> 更省事的办法：**用 QQ 客户端的内嵌浏览器打开你的空间**。QQ 客户端那套浏览器的 Cookie 库是**明文存储**的，可以直接读出来，不用手动复制。
> 注意：`p_skey` 是 HttpOnly，在控制台敲 `document.cookie` **取不到**，必须从请求头或 DevTools 的 Application 面板拿。

**第 3 步：填配置**

AstrBot 面板 → 插件 → QQ空间 → 配置：

| 配置项 | 填什么 |
|---|---|
| `qzone_cookie` | 粘贴上一步的 Cookie（**填了它就不需要 QQ 客户端**） |
| `trigger.like_only` | `true`（只点赞不评论） |
| `trigger.like_cron` | `0 */2 * * *`（每 2 小时一次；留空则关闭） |
| `trigger.comment_cron` / `publish_cron` | 留空（纯点赞模式下用不到） |

**第 4 步：验证**

```bash
export QZONE_COOKIE='你的Cookie'
python3 tools/qzone_check.py --list          # 看动态流里每条动态的真实点赞状态
python3 tools/qzone_check.py --like-latest   # 点赞最新一条并自动回查是否真的生效
```

### 方式二：NapCat 模式（需要 QQ 客户端在服务器上登录）

**适合**：除了点赞，还想用机器人的群聊互动 / 发说说 / 表白墙等功能。

此时**不要**填 `qzone_cookie`，插件会通过 OneBot 的 `get_cookies` 从 NapCat 自动获取登录态。

> ⚠️ NapCat 是以「电脑端」身份登录 QQ 的，同一个号不能同时登两台电脑 —— **服务器一登，你本机 QQ 会被挤下线**（手机不受影响）。不能接受这点就用方式一。

## ⚙️ 配置详解

完整字段说明见 **[docs/02-配置详解.md](docs/02-配置详解.md)**，这里只列本 Fork 新增的部分：

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `qzone_cookie` | text | 空 | 手工 QQ空间 Cookie。**填了就优先用它**，服务器无需登录 QQ |
| `qzone_cookie_file` | string | 空 | Cookie 文件路径。**优先级高于 `qzone_cookie`**，配合 [keeper](keeper/README.md) 实现自动续期 |
| `trigger.like_only` | bool | `true` | 只点赞不评论。开启后 `AutoComment` 自动跳过，点赞交给 `AutoLike` |
| `trigger.like_cron` | string | `0 */2 * * *` | 自动点赞的基准时间（Cron）。**留空 = 关闭自动点赞** |
| `trigger.like_offset` | int | `900` | 围绕基准时间随机前后浮动的秒数（打散时间特征） |
| `trigger.like_num` | int | `20` | 每轮最多处理动态流最新 N 条 |
| `trigger.like_delay_min` / `like_delay_max` | int | `8` / `20` | 每条点赞之间的随机间隔（秒），防风控 |
| `trigger.like_use_http_key` | bool | **`true`** | 点赞接口用 `http://` 拼 `unikey`。**实测必须开启**，关掉点赞就会静默失效 |

### 定时任务语义（上游设计 + 本 Fork 的修复）

- `*_cron` 是**基准时间**，`*_offset` 是围绕它随机浮动的**秒数**。
  例：`like_cron = 0 */2 * * *` + `like_offset = 900` → 每两小时一次，实际执行时间在基准点前后 15 分钟内随机。
- 上游用「一次性任务链」（本轮跑完才排下一轮），**且没有设置宽限期**：任务只要晚 1 秒执行，APScheduler 会直接跳过，而下一轮永远不会被排上 —— 任务链就此断掉、**点赞永久静默失效**。本 Fork 已加 `misfire_grace_time=3600`。

## 🐛 排查与验证

| 需求 | 做法 |
|---|---|
| 看点赞有没有真的生效 | `python3 tools/qzone_check.py --list` |
| 看插件运行日志 | `docker logs -f astrbot \| grep -E "AutoLike\|已点赞"` |
| 判断 Cookie 是否过期 | 日志出现「登录状态失效，请重新登录后重试」即为过期，重新复制一次即可 |
| 完整排查方法论 | **[docs/03-点赞失效根因报告.md](docs/03-点赞失效根因报告.md)** |
| 常见问题 | **[docs/04-常见问题.md](docs/04-常见问题.md)** |

## 📖 命令一览（继承自上游）

> 序号从 `0` 开始（`0` = 最新一条，`-1` = 最后一条），支持范围语法 `2~5`；`@某人` 表示操作该用户的空间。

| 命令 | 别名 | 权限 | 参数 | 功能 |
|---|---|---|---|---|
| 查看访客 | - | ADMIN | - | 查看空间最近访客 |
| 看说说 | 查看说说 | ALL | `[@用户] [序号/范围]` | 查看说说 |
| 评说说 | 评论说说、读说说 | ALL | `[@用户] [序号/范围]` | 评论说说（可配置自动点赞） |
| 赞说说 | - | ALL | `[@用户] [序号/范围]` | 给说说点赞 |
| 发说说 | - | ADMIN | `<文本> [图片]` | 立即发布说说 |
| 写说说 | 写稿 | ADMIN | `<主题> [图片]` | AI 生成草稿 |
| 删说说 | - | ADMIN | `<序号>` | 删除自己发的说说 |
| 回评 | 回复评论 | ALL | `<稿件ID> [评论序号]` | 回复评论 |
| 投稿 / 匿名投稿 | - | ALL | `<文本> [图片]` | 向表白墙投稿 |
| 看稿 / 过稿 / 拒稿 / 撤稿 | - | 混合 | `<稿件ID>` | 表白墙稿件管理 |

## 📁 目录结构

```
astrbot_plugin_qzone/
├── main.py                     # 插件入口（本 Fork 增加了 AutoLike 注册）
├── metadata.yaml
├── requirements.txt
├── _conf_schema.json           # 配置面板定义（含本 Fork 新增项）
├── core/
│   ├── scheduler.py            # 定时任务（本 Fork 新增 AutoLike 任务）
│   ├── service.py              # 业务服务（本 Fork 让点赞失败不再谎报成功）
│   ├── qzone/
│   │   ├── api.py              # 空间接口封装（本 Fork 增加 http unikey 开关）
│   │   ├── client.py           # HTTP 客户端（本 Fork 修复 data 为字符串时崩溃）
│   │   ├── session.py          # 登录态（本 Fork 增加手工 Cookie 支持）
│   │   └── model.py            # 请求上下文（本 Fork 支持 Cookie 字段透传）
│   └── ...
├── docs/                       # 完整文档
├── keeper/                     # Cookie 保活侧车服务（常驻浏览器，自动续期 Cookie）
├── tools/qzone_check.py        # 点赞状态自查工具
└── pages/dashboard/            # 面板前端页面
```

## 🤝 贡献

- 上游项目：[Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) —— 请优先给上游点 Star
- 本 Fork 的补丁清单与逐条说明：[docs/05-补丁清单.md](docs/05-补丁清单.md)
- 提交 Issue 时请附上：AstrBot 版本、`AutoLike` 相关日志、`tools/qzone_check.py --list` 的输出（**记得打码 Cookie**）

## 📄 许可证与致谢

- 本仓库沿用上游的 **GNU General Public License v3.0**（见 [LICENSE](./LICENSE)）。
  > 📌 上游 README 的徽章写的是 MIT，但仓库里的 `LICENSE` 文件实际是 **GPL-3.0**。以文件为准，本 Fork 因此同样采用 GPL-3.0。
- 上游作者：**Zhalslar**；表白墙部分参考了 [CampuxBot](https://github.com/idoknow/CampuxBot)
- 本 Fork 的排查、补丁与文档：由 **AI Agent（DeepSeek Harness）** 在真机联调中完成，详见 [NOTICE](./NOTICE)

#本插件借助deepseekv4开发.