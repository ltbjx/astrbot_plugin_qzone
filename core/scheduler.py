import asyncio
import json
import random
import zoneinfo
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot.api import logger

from .config import PluginConfig
from .sender import Sender
from .service import PostService


class AutoRandomCronTask:
    """
    Schedule one task per cron cycle around the cron anchor time.
    Subclasses only need to implement async do_task().
    """

    def __init__(
        self,
        job_name: str,
        cron_expr: str,
        timezone: zoneinfo.ZoneInfo,
        offset_seconds: int,
    ):
        self.timezone = timezone
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.scheduler.start()

        self.cron_expr = cron_expr
        self.job_name = job_name
        self.offset_seconds = offset_seconds
        self._last_base_time: datetime | None = None
        self._terminated = False

        self._register_task()

        logger.info(
            f"[{self.job_name}] 已启动，任务周期：{self.cron_expr}，偏移范围：±{self.offset_seconds} 秒"
        )

    def _register_task(self):
        try:
            self.trigger = CronTrigger.from_crontab(
                self.cron_expr, timezone=self.timezone
            )
            self._schedule_next_job()
        except Exception as e:
            logger.error(f"[{self.job_name}] Cron 格式错误：{e}")

    def _schedule_next_job(self):
        if self._terminated:
            logger.debug(f"[{self.job_name}] 调度器已终止，跳过后续调度")
            return
        if not hasattr(self, "trigger"):
            return

        now = datetime.now(self.timezone)
        base_time = self.trigger.get_next_fire_time(self._last_base_time, now)
        if not base_time:
            logger.error(f"[{self.job_name}] 无法计算下一次基准时间")
            return

        self._last_base_time = base_time

        delay_seconds = (
            random.randint(-self.offset_seconds, self.offset_seconds)
            if self.offset_seconds
            else 0
        )
        target_time = base_time + timedelta(seconds=delay_seconds)

        if target_time <= now:
            target_time = now + timedelta(seconds=1)
            logger.warning(
                f"[{self.job_name}] 偏移后时间已过，改为立即补偿执行：{target_time}"
            )

        logger.info(
            f"[{self.job_name}] 基准时间：{base_time}，偏移：{delay_seconds} 秒，执行时间：{target_time}"
        )

        try:
            self.scheduler.add_job(
                func=self._run_task_wrapper,
                trigger=DateTrigger(run_date=target_time, timezone=self.timezone),
                name=f"{self.job_name}_once_{int(base_time.timestamp())}",
                max_instances=1,
                # 本地补丁：上游没设宽限期，任务只要晚 1 秒执行就会被 APScheduler 直接跳过，
                # 而“下一轮”是在本轮执行完才排的 —— 于是整条任务链永久断掉、静默失效。
                # 给 1 小时宽限，错过也补跑。
                misfire_grace_time=3600,
                coalesce=True,
            )
        except Exception as e:
            if self._terminated:
                logger.debug(
                    f"[{self.job_name}] 调度器终止后跳过 add_job：{type(e).__name__}: {e}"
                )
                return
            logger.error(f"[{self.job_name}] 添加调度任务失败：{e}")

    async def _run_task_wrapper(self):
        logger.info(f"[{self.job_name}] 开始执行任务")
        try:
            await self.do_task()
        except Exception as e:
            logger.exception(f"[{self.job_name}] 任务执行失败: {e}")
        finally:
            if not self._terminated:
                self._schedule_next_job()
            logger.info(f"[{self.job_name}] 本轮任务完成")

    async def do_task(self):
        raise NotImplementedError

    async def terminate(self):
        if self._terminated:
            return
        self._terminated = True
        self.scheduler.remove_all_jobs()
        try:
            self.scheduler.shutdown(wait=False)
        except Exception as e:
            logger.debug(f"[{self.job_name}] 关闭调度器时忽略异常：{e}")
        logger.info(f"[{self.job_name}] 已停止")


class AutoComment(AutoRandomCronTask):
    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
    ):
        cron = config.trigger.comment_cron
        timezone = config.timezone
        offset = config.trigger.comment_offset
        super().__init__("AutoComment", cron, timezone, offset)
        self.cfg = config
        self.service = service
        self.sender = sender

    async def do_task(self):
        # 本地补丁：纯点赞模式下不自动评论好友说说（点赞交给 AutoLike 任务）
        if getattr(self.cfg.trigger, "like_only", False):
            logger.info("[AutoComment] like_only 已开启，跳过本轮自动评论")
            return

        posts = await self.service.query_feeds(
            pos=0,
            num=20,
            no_self=True,
            no_commented=True,
        )
        for post in posts:
            try:
                await self.service.comment_posts(post)
                if self.cfg.trigger.like_when_comment:
                    await self.service.like_posts(post)
                await self.sender.send_admin_post(post, message="定时读说说")
            except Exception as e:
                logger.exception(
                    f"[{self.job_name}] 跳过说说评论失败: tid={post.tid}, uin={post.uin}, name={post.name}, error={e}"
                )


class AutoPublish(AutoRandomCronTask):
    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
    ):
        cron = config.trigger.publish_cron
        timezone = config.timezone
        offset = config.trigger.publish_offset
        super().__init__("AutoPublish", cron, timezone, offset)
        self.service = service
        self.sender = sender

    async def do_task(self):
        try:
            text = await self.service.llm.generate_post()
        except Exception as e:
            logger.error(f"自动生成内容失败：{e}")
            return
        post = await self.service.publish_post(text=text)
        await self.sender.send_admin_post(post, message="定时发说说")


class AutoLike(AutoRandomCronTask):
    """
    本地补丁新增：定时扫描好友动态流并自动点赞。

    与官方 AutoComment 的区别：
    - 只点赞，不评论（不需要 LLM）
    - 用本地 JSON 记录已点赞的 tid 去重，不会每轮重复点赞同一条
    - 每条点赞之间有随机间隔，降低风控概率
    """

    _STORE_LIMIT = 2000

    def __init__(
        self,
        config: PluginConfig,
        service: PostService,
        sender: Sender,
    ):
        cron = config.trigger.like_cron
        timezone = config.timezone
        offset = config.trigger.like_offset
        super().__init__("AutoLike", cron, timezone, offset)
        self.cfg = config
        self.service = service
        self.sender = sender
        self._store_path = Path(config.data_dir) / "liked_tids.json"
        self._liked: list[str] = self._load_liked()

    def _load_liked(self) -> list[str]:
        try:
            if self._store_path.exists():
                data = json.loads(self._store_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [str(t) for t in data if t]
        except Exception as e:
            logger.warning(f"[AutoLike] 已点赞记录读取失败，按空处理：{e}")
        return []

    def _save_liked(self) -> None:
        try:
            if len(self._liked) > self._STORE_LIMIT:
                self._liked = self._liked[-self._STORE_LIMIT :]
            self._store_path.write_text(
                json.dumps(self._liked, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[AutoLike] 已点赞记录写入失败：{e}")

    async def do_task(self):
        num = max(int(self.cfg.trigger.like_num or 20), 1)
        delay_min = max(int(self.cfg.trigger.like_delay_min or 0), 0)
        delay_max = max(int(self.cfg.trigger.like_delay_max or 0), delay_min)

        # 注意：这里不带 no_commented，避免为每条动态额外请求详情接口
        posts = await self.service.query_feeds(pos=0, num=num, no_self=True)

        liked = 0
        skipped = 0
        for post in posts:
            if not post.tid:
                continue
            if post.tid in self._liked:
                skipped += 1
                continue
            try:
                await self.service.like_posts(post)
                self._liked.append(post.tid)
                liked += 1
                if delay_max:
                    await asyncio.sleep(random.randint(delay_min, delay_max))
            except Exception as e:
                logger.exception(
                    f"[AutoLike] 点赞失败 tid={post.tid} uin={post.uin} 昵称={post.name}：{e}"
                )

        if liked:
            self._save_liked()

        logger.info(
            f"[AutoLike] 本轮检查 {len(posts)} 条动态，新点赞 {liked} 条，"
            f"跳过已赞 {skipped} 条，去重库共 {len(self._liked)} 条"
        )
