# Changelog

## v3.2.0 — 增强 Fork

> 本版本基于上游 v3.1.3（commit `10bf854`）。完整的排查过程与证据链见
> [docs/03-点赞失效根因报告.md](docs/03-点赞失效根因报告.md)。

### Bug 修复（严重）

* **修复点赞静默失效**：点赞接口的 `unikey`/`curkey` 必须使用 `http://` 协议拼接。
  上游使用 `https://`，接口会返回 `{"code":0,"message":"succ"}` 但**服务端直接忽略**，
  导致"日志显示已点赞、空间里却没有赞"。新增 `trigger.like_use_http_key` 开关，**默认开启**。
  依据来自动态流 HTML 中点赞按钮携带的 `data-unikey`（浏览器实际发送值）。
* **修复点赞流程崩溃**：`core/qzone/client.py` 中写死 `parsed["data"].get("ret")`，
  而点赞接口返回的 `data` 是字符串 `""`，必然抛
  `'str' object has no attribute 'get'`。改为先判断 `isinstance(data, dict)`。
* **修复定时任务永久断链**：任务采用「一次性任务链」，但 `add_job` 未设置
  `misfire_grace_time`（APScheduler 默认仅 1 秒）。任务只要晚 1 秒执行就会被跳过，
  而"排下一轮"写在被跳过的 `finally` 里，于是**任务链永久断掉、点赞静默失效且无任何报错**。
  已设置 `misfire_grace_time=3600` 与 `coalesce=True`。
* **修复点赞失败被记录为成功**：`PostService.like_posts()` 原本不检查返回值，
  接口失败也照常打印「已点赞」并计入去重库。现在失败会抛出真实错误。
* 修正日志中把偏移单位「秒」写成「分钟」的问题。

### 新功能

* **`AutoLike` 独立定时任务**：只点赞、不评论、不调用 LLM；带本地落盘去重
  （`liked_tids.json`，保留最近 2000 条）与每条之间 8~20 秒随机间隔（防风控）。
* **`trigger.like_only` 只点赞不评论模式**（默认开启）：不再自动评论好友动态；
  上游的自动点赞挂在评论任务里，评论一旦失败会连带跳过点赞。
* **手工 Cookie 模式**：新增 `qzone_cookie` 配置项，填写后无需 NapCat / QQ 客户端即可工作，
  **不会把使用者本机或手机上的 QQ 挤下线**；Cookie 中除 `uin`/`skey`/`p_skey` 外的字段也会原样透传。
* **`tools/qzone_check.py` 点赞状态自查工具**：从动态流的 `data-islike` 字段读取真实点赞状态，
  支持 `--list` / `--like-latest` / `--like <unikey>` 并在点赞后自动回查，仅依赖标准库。

### 文档

* 新增 `docs/`：快速开始、配置详解、点赞失效根因报告、常见问题、补丁清单、服务器部署。
* 新增 `NOTICE` 与 `patches/fork-vs-upstream.patch`（相对上游的完整 diff）。
* 修正许可证说明：上游 README 徽章标注 MIT，但 `LICENSE` 文件实为 **GPL-3.0**，本 Fork 以文件为准。

---

## v3.1.3

* 统一使用内置卡片渲染器，Sender 直接创建并调用，移除工厂、协议与 pillowmd 回退分支。
* 移除旧渲染器配置、依赖及默认样式资源，显式依赖 Pillow，保留无图片时的纯文本发送。

## v3.1.2

Bug Fixes:

* 将内置卡片渲染器的头像与资源缓存移至插件数据目录（`data_dir / cache`），防止被 AstrBot 定时清理误删。
* 增加缓存 TTL 校验与过期自动清理机制（默认 1 天）。

## v3.1.1

Bug Fixes:

* 支持本地文件路径和 `file://` URI 图片上传与卡片渲染。
* 优化带图发说说失败时的错误提示与响应解析。

## v3.1.0

New Features:

* 新增前端pages，可在Astrbot仪表盘直接查看、评论、点赞自己或好友的说说。
* 添加可插拔的消息渲染抽象层，支持新的内置 Qzone 卡片渲染器以及现有的 pillowmd 渲染器。
* 扩展帖子模型和数据库模式，以在渲染卡片时存储和使用用户头像 URL。
* 将评论、图片和转发内容渲染为富卡片风格的图片，支持表情符号（emoji），并包括头像与媒体的缓存及资源获取。

Bug Fixes:

* 在 HTTP 请求期间处理更多 Qzone 图片过期错误码，以触发重新登录。

Enhancements:

* 重构发送逻辑，将消息分段的创建与发送委托给新的渲染器抽象层，从而减少重复代码。
* 改进 Qzone 解析逻辑，以从动态中抓取头像 URL 并传播至帖子模型。
* 在渲染失败时增加优雅的回退机制，返回纯文本而不是图片。

Build:

* 添加 `apilmoji` 作为依赖，用于在内置卡片渲染器中支持识别 emoji 的文本渲染。

## v3.0.4

Bug 修复：

- 在自动评论调度器中使用专用的评论偏移配置，而不是发布偏移配置。
- 确保自动评论过滤能够正确检测已有的自我评论，同时利用内存中的帖子数据和之前保存的帖子记录，防止在同一条 Feed 上重复评论。

改进：

- 在为自动评论查询 Feed 时，更早排除自己的帖子和已评论的帖子，以减少不必要的处理。
- 在自动评论任务中为每条帖子添加错误处理和结构化日志记录，这样单条评论失败不会中断剩余帖子的处理。

## v3.0.3

- Refactor: 重构自动发说说、自动评论的定时调度逻辑，改为以 `cron` 触发点作为基准时间，并通过 `publish_offset_minutes` / `comment_offset_minutes` 在基准时间前后随机浮动（`±N` 分钟）；偏移为 `0` 时严格按 `cron` 触发。

## v3.0.2

- Fix: 修复 QZone API 返回空字符串时 `json5.loads()` 抛出 `ValueError: Empty strings are not legal JSON5` 导致插件异常中断的问题。
- Improve: 新增接口响应兜底解析，针对空响应、无 JSON 片段、JSON 解析失败返回可处理的错误结果，避免直接抛异常。
- Improve: `QzoneParser.parse_response` 在解析结果非 `dict` 时，改为返回统一错误对象，不再抛出 `RuntimeError`，调用方错误处理路径更一致。
- Fix: 修复 `403` 被误判为“登录失效”并重复重登的问题；仅在 `401` 或接口明确登录失效（`code = -3000`）时触发重登。
- Improve: 查询说说失败时细化错误提示，区分“无权限查看”“登录状态失效”“接口响应异常”“暂无可见说说”等场景。
- Refactor: 新增 `core/qzone/constants.py`，集中维护错误码、错误消息、HTTP 状态及内部元数据键，降低多处散落导致的不一致风险。
- Refactor: 将 HTTP 状态注入到内部元数据 `__qzone_internal__.http_status`，并在 `ApiResponse.data` 中剥离内部字段，避免与业务字段冲突。
- Improve: 统一错误消息常量为中文（如“响应内容为空”“权限不足”），更符合插件用户使用场景。
- Behavior: 保持无参数调用默认行为为查询最新一条说说（`pos=0, num=1`）。
