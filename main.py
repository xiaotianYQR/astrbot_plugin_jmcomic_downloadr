"""
JMComic 下载插件 (astrbot_plugin_jmcomic)
==========================================

基于 jmcomic (JMComic-Crawler-Python) 的 AstrBot 插件，提供：

- /jm <车号...>            后台下载本子/章节，完成后打包 zip 发送
- /jm info <车号>          查看本子详情（不下载）
- /jm search <关键词>      站内搜索
- /jm status               查看本会话的后台下载任务
- /jm cancel <任务id>      取消任务（不删除已下载文件）
- /jm help                 帮助

说明：
- 车号支持任意文本，例如 123、JM123、https://18comic.vip/album/123/；
  章节号以 p 开头，例如 p456。
- 下载任务在后台运行，完成后会主动推送压缩包（部分平台不支持文件消息，
  会退回发送保存路径文本）。
- 数据默认存放在 AstrBot 的 data/plugin_data/jmcomic_downloader/ 下，
  可通过插件配置修改。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain, Reply
from astrbot.api.star import Context, Star

try:
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover - 兼容旧版本 AstrBot
    GreedyStr = str  # type: ignore

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except Exception:  # pragma: no cover - 兼容旧版本 AstrBot
    def get_astrbot_plugin_data_path() -> str:  # type: ignore
        return os.path.realpath(os.path.join(os.getcwd(), "data", "plugin_data"))


JM_LOGGER_NAME = "jmcomic"
PLUGIN_DATA_DIR = "jmcomic_downloader"
DEFAULT_DOWNLOAD_SUBDIR = "download"
ZIP_SUBDIR = "zip"

# 需要转发给用户的 jmcomic 日志 topic
PROGRESS_TOPICS = {
    "album.before",
    "album.after",
    "photo.failed",
    "image.failed",
}
# 每个任务最多发送的进度消息数，防止刷屏
MAX_PROGRESS_MESSAGES = 40
# 相同会话+相同车号的下载指令去重窗口（秒）
CMD_DEDUP_WINDOW = 60

# 模块级指令去重表：key -> 认领时间。
# 用模块级而不是实例级，即使插件被多次加载/指令被重复派发，
# 同一会话同一批车号也只会启动一个下载任务。
_cmd_dedup: dict[tuple, float] = {}


def _claim_cmd_dedup(dedup_key: tuple) -> bool:
    """尝试认领一个下载指令；若已有未完成/刚启动的相同指令则返回 False。"""
    now = time.time()
    # 惰性清理过期记录
    stale = [k for k, t in _cmd_dedup.items() if now - t > CMD_DEDUP_WINDOW]
    for k in stale:
        _cmd_dedup.pop(k, None)
    if now - _cmd_dedup.get(dedup_key, 0) < CMD_DEDUP_WINDOW:
        return False
    _cmd_dedup[dedup_key] = now
    return True


def _release_cmd_dedup(dedup_key: tuple) -> None:
    _cmd_dedup.pop(dedup_key, None)


# 并发下载任务共享同一个 jmcomic logger，日志可能被多个任务重复转发，
# 导致同一条消息被发送两次。用 (会话, 文本) 短窗口去重兜底；
# 会话内最后一个任务结束时清空记录，避免误伤顺序执行的后续任务。
SEND_DEDUP_WINDOW = 30.0
_send_dedup: dict[tuple, float] = {}


def _check_send_dedup(key: tuple) -> bool:
    """尝试认领一次消息发送；短窗口内已有相同消息则返回 False（不再发送）。"""
    now = time.time()
    stale = [k for k, t in _send_dedup.items() if now - t > SEND_DEDUP_WINDOW]
    for k in stale:
        _send_dedup.pop(k, None)
    if now - _send_dedup.get(key, 0) < SEND_DEDUP_WINDOW:
        return False
    _send_dedup[key] = now
    return True


def _clear_session_send_dedup(session_key: str) -> None:
    """清空某个会话的去重记录（会话内已无运行中的任务时调用）。"""
    for k in [k for k in _send_dedup if k[0] == session_key]:
        _send_dedup.pop(k, None)


def _looks_like_timeout(e: BaseException) -> bool:
    """判断异常是否是超时类错误（大文件上传超时通常实际已送达）。"""
    name = type(e).__name__.lower()
    msg = str(e).lower()
    if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
        return True
    return "timeout" in name or "timed out" in msg


class JmLogForwarder(logging.Handler):
    """把 jmcomic 的日志转发到 asyncio 队列（线程安全）。"""

    def __init__(self, sink) -> None:
        super().__init__(level=logging.INFO)
        self._sink = sink  # callable(topic, message)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            topic = getattr(record, "topic", "") or ""
            self._sink(topic, record.getMessage())
        except Exception:
            pass


@dataclass
class DownloadTask:
    """一个后台下载任务。"""

    task_id: str
    session_key: str
    albums: list[str]
    photos: list[str]
    created_at: float
    status: str = "running"  # running | done | failed | cancelled
    message: str = ""
    asyncio_task: Optional[asyncio.Task] = None
    results: list = field(default_factory=list)
    # 本子信息获取是否成功（用于区分“获取成功但下载出错”）
    album_fetched: bool = False
    fetched_album_id: str = ""
    fetched_title: str = ""
    reply_message_id: str = ""
    dedup_key: tuple = ()


def _safe_filename(text: str, max_len: int = 60) -> str:
    """把标题清洗成可用于文件名的字符串。"""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len] or "untitled"


def _album_folder_name(album) -> str:
    """生成本子文件夹名：车号-标题，如 12345-我是本子。"""
    title = getattr(album, "title", "") or getattr(album, "name", "") or ""
    return f"{album.id}-{_safe_filename(title, 60)}"


class JmcomicPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None) -> None:
        super().__init__(context)
        self.config: dict = config or {}
        self._tasks: dict[str, DownloadTask] = {}
        self._jm: Any = None
        self._bump_platform_timeouts()

    def _bump_platform_timeouts(self) -> None:
        """尽力调大平台适配器（Telegram 等）的文件上传超时。

        python-telegram-bot 22.x 默认 media_write_timeout 只有 20 秒，
        较大的压缩包上传会超时（文件其实已送达，但请求抛 Timed out）。
        这里把 Telegram bot 的请求对象写超时调大到 600 秒。
        不同版本内部结构不同，全部 try/except 兜底，失败不影响插件。
        """
        try:
            platform_manager = getattr(self.context, "platform_manager", None)
            insts = getattr(platform_manager, "platform_insts", []) or []
            for inst in insts:
                try:
                    meta = getattr(inst, "meta", None)
                    platform_name = (meta() or type("M", (), {"name": ""})()).name
                except Exception:
                    continue
                if platform_name != "telegram":
                    continue
                client = getattr(inst, "client", None)
                raw = getattr(client, "_request", None)
                req_list = list(raw) if isinstance(raw, (tuple, list)) else [raw]
                for req in req_list:
                    if req is None:
                        continue
                    try:
                        if hasattr(req, "_media_write_timeout"):
                            req._media_write_timeout = max(
                                float(req._media_write_timeout or 0), 600
                            )
                    except Exception:
                        pass
        except Exception as e:
            self.logger.debug(f"调整平台上传超时失败（可忽略）: {e}")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _get_jm(self):
        """懒加载 jmcomic 模块；未安装时返回 None。"""
        if self._jm is None:
            try:
                import jmcomic  # noqa: PLC0415

                self._jm = jmcomic
                jm_logger = logging.getLogger(JM_LOGGER_NAME)
                jm_logger.setLevel(logging.INFO)
                # 移除 jmcomic 自带的控制台 handler，避免日志重复刷到 stdout
                for h in list(jm_logger.handlers):
                    jm_logger.removeHandler(h)
            except ImportError:
                self.logger.error("未安装 jmcomic，请执行 pip install jmcomic")
                self._jm = False
        return self._jm or None

    def _cfg(self, key: str, default=None):
        if not isinstance(self.config, dict):
            return default
        return self.config.get(key, default)

    def _download_root(self) -> Path:
        custom = self._cfg("download_dir", "")
        if custom:
            return Path(os.path.abspath(os.path.expanduser(str(custom))))
        return Path(get_astrbot_plugin_data_path()) / PLUGIN_DATA_DIR / DEFAULT_DOWNLOAD_SUBDIR

    def _zip_root(self) -> Path:
        custom = self._cfg("zip_dir", "")
        if custom:
            return Path(os.path.abspath(os.path.expanduser(str(custom))))
        return Path(get_astrbot_plugin_data_path()) / PLUGIN_DATA_DIR / ZIP_SUBDIR

    def _check_permission(self, event: AstrMessageEvent) -> bool:
        mode = self._cfg("permission", "admin")
        if mode == "everyone":
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _build_option(self, jm):
        """根据插件配置构造 jmcomic 的 JmOption。"""
        suffix = self._cfg("image_suffix", "") or None
        proxy = self._cfg("proxy", "") or "system"
        # photo 并发数：0 表示自动（不传该键，使用 jmcomic 默认的 CPU 核数）
        # 注意不能显式传 None，否则会覆盖 jmcomic 默认值，导致
        # execute_on_condition 中 count_batch(None) >= count_real(int) 崩溃。
        threading_cfg: dict = {
            "image": max(1, int(self._cfg("image_threads", 30))),
        }
        photo_threads = int(self._cfg("photo_threads", 0) or 0)
        if photo_threads > 0:
            threading_cfg["photo"] = photo_threads
        # 注册自定义本子目录字段：Aalbum_dirname -> "车号-标题"（如 12345-我是本子）。
        # jmcomic 自 2.4.5 起支持从 JmModuleConfig.AFIELD_ADVICE 取自定义目录名。
        try:
            jm.JmModuleConfig.AFIELD_ADVICE["album_dirname"] = _album_folder_name
        except Exception:
            pass  # 注册失败时退回默认规则，不影响下载
        return jm.JmOption.construct(
            {
                "dir_rule": {
                    # 本子目录 = 车号-标题（Aalbum_dirname），章节 = 章节序号（Pindex）。
                    # Aalbum_dirname 以 A 开头，保证 decide_album_root_dir() 返回本子级目录，
                    #    打包 zip 时只包含当前本子，不会把整个下载缓存目录打进去；
                    # 标题中的 Windows 非法字符会被 jmcomic 自动替换，中文可正常保留。
                    "rule": "Bd/Aalbum_dirname/Pindex",
                    "base_dir": str(self._download_root()),
                    "normalize_zh": None,
                },
                "download": {
                    "cache": True,
                    "image": {
                        "decode": bool(self._cfg("image_decode", True)),
                        "suffix": suffix,
                    },
                    "threading": threading_cfg,
                },
                "client": {
                    "impl": self._cfg("client_impl", "api"),
                    "retry_times": max(0, int(self._cfg("retry_times", 5))),
                    "postman": {
                        "type": "curl_cffi",
                        "meta_data": {"impersonate": "chrome", "proxies": proxy},
                    },
                },
                "plugins": {"valid": "log"},
                "log": True,
            }
        )

    def _parse_ids(self, jm, text: str) -> tuple[list[str], list[str]]:
        """解析用户输入，返回 (album_ids, photo_ids)。"""
        albums: list[str] = []
        photos: list[str] = []
        for token in re.split(r"[\s,，;；]+", text.strip()):
            if not token:
                continue
            if token.lower().startswith("p") and token[1:].strip():
                raw = token[1:].strip()
                photos.append(str(jm.JmcomicText.parse_to_jm_id(raw)))
            else:
                albums.append(str(jm.JmcomicText.parse_to_jm_id(token)))
        return albums, photos

    def _active_task_count(self, session_key: str) -> int:
        return sum(
            1
            for t in self._tasks.values()
            if t.session_key == session_key and t.status == "running"
        )

    # ------------------------------------------------------------------
    # 下载执行（同步部分，运行在后台线程）
    # ------------------------------------------------------------------

    def _run_download(self, jm, option, albums, photos) -> list[tuple[str, Any]]:
        """同步执行下载（在 asyncio.to_thread 中运行）。"""
        results: list[tuple[str, Any]] = []
        for aid in albums:
            results.append(
                ("album", jm.download_album(aid, option, check_exception=False))
            )
        for pid in photos:
            results.append(
                ("photo", jm.download_photo(pid, option, check_exception=False))
            )
        return results

    def _result_target_dir(self, option, kind: str, ret) -> str:
        """计算下载结果所在的文件夹。"""
        if kind == "album":
            album = ret.detail
            return option.dir_rule.decide_album_root_dir(album)
        photo = ret.detail
        return option.dir_rule.decide_image_save_dir(photo.from_album, photo)

    def _zip_dir(self, src_dir: str, zip_path: str, arc_root: str = "") -> None:
        """把整个文件夹打包为 zip，zip 内以 arc_root 作为顶层目录名。"""
        src_dir = os.path.abspath(src_dir)
        zip_path_abs = os.path.abspath(zip_path)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(src_dir):
                for name in files:
                    full = os.path.join(root, name)
                    # 防止把输出 zip 本身打进去（zip_dir 配置在下载目录内时）
                    if os.path.abspath(full) == zip_path_abs:
                        continue
                    rel = os.path.relpath(full, src_dir)
                    arc = os.path.join(arc_root, rel) if arc_root else rel
                    zf.write(full, arc.replace("\\", "/"))

    # ------------------------------------------------------------------
    # 后台任务（异步）
    # ------------------------------------------------------------------

    async def _download_job(
        self, task: DownloadTask, umo: str, albums: list[str], photos: list[str]
    ) -> None:
        jm = self._get_jm()
        if jm is None:
            task.status = "failed"
            task.message = "jmcomic 未安装"
            try:
                await self.context.send_message(
                    umo, MessageChain().message("❌ jmcomic 未安装，无法下载。")
                )
            except Exception:
                pass
            return
        option = self._build_option(jm)
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def sink(topic: str, msg: str) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, (topic, msg))
            except RuntimeError:
                pass  # 事件循环已关闭

        forwarder = JmLogForwarder(sink)
        jm_logger = logging.getLogger(JM_LOGGER_NAME)
        # 确保 INFO 级别日志能被转发（不依赖 jmcomic 是否已初始化 logger）
        jm_logger.setLevel(logging.INFO)
        jm_logger.addHandler(forwarder)
        progress_sent = 0

        async def send_text(text: str) -> None:
            try:
                # 并发任务会把同一条 jmcomic 日志转发到多个任务队列，去重后只发一次
                if not _check_send_dedup((umo, text)):
                    return
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as e:
                self.logger.warning(f"进度消息发送失败: {e}")

        async def handle_progress(topic: str, msg: str) -> None:
            nonlocal progress_sent
            if topic not in PROGRESS_TOPICS:
                return
            if not self._cfg("send_progress", True):
                return
            if progress_sent >= MAX_PROGRESS_MESSAGES:
                return
            progress_sent += 1
            if topic == "album.before":
                # 记录本子信息获取成功，用于区分“获取成功但下载出错”
                m = re.search(
                    r"本子获取成功: \[(\d+)\].*?标题: \[(.*?)\]", msg, re.S
                )
                if m:
                    # 只认领本任务的 album 日志，避免并发任务互相污染状态和重复发送
                    if m.group(1) not in albums:
                        return
                    task.album_fetched = True
                    task.fetched_album_id = m.group(1)
                    task.fetched_title = m.group(2)
                    await send_text(
                        f"✅ 本子获取成功: JM{task.fetched_album_id}"
                        f"\n📥 开始下载…"
                    )
                else:
                    await send_text(f"✅ {msg}\n📥 开始下载…")
            elif topic == "album.after":
                m = re.search(r"\[(\d+)\]", msg)
                mid = m.group(1) if m else ""
                if mid and mid not in albums:
                    return  # 其他并发任务的日志
                await send_text(
                    f"✅ 本子下载完成: JM{mid}" if mid else f"✅ {msg}"
                )
            elif topic in ("photo.failed", "image.failed"):
                await send_text(f"⚠️ {msg}")

        try:
            future = asyncio.ensure_future(
                asyncio.to_thread(self._run_download, jm, option, albums, photos)
            )

            # 边下载边转发进度
            while not future.done():
                try:
                    topic, msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await handle_progress(topic, msg)

            # 任务结束后再排空一次队列，防止任务瞬间完成/失败时进度消息丢失
            await asyncio.sleep(0)  # 让尚未执行的线程回调先入队
            while not queue.empty():
                try:
                    topic, msg = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await handle_progress(topic, msg)

            results = future.result()
            task.results = results

            # 逐条处理：统计成功/失败，打包 zip
            summary: list[str] = []
            failed_any = False
            zip_files: list[tuple[str, str]] = []  # (name, abs path)
            for kind, ret in results:
                dler = ret.downloader
                target_dir = self._result_target_dir(option, kind, ret)
                entity = ret.detail
                entity_id = entity.id
                title = entity.name

                failed_count = len(dler.download_failed_image) + len(
                    dler.download_failed_photo
                )
                photo_count = sum(
                    len(v) for v in dler.download_success_dict.values()
                )
                if failed_count:
                    failed_any = True
                summary.append(
                    f"• JM{entity_id}《{_safe_filename(title)}》"
                    f"({photo_count} 个章节，失败 {failed_count} 张)"
                )

                if self._cfg("zip_after_download", True) and os.path.isdir(target_dir):
                    zip_root = self._zip_root()
                    zip_root.mkdir(parents=True, exist_ok=True)
                    # 压缩包文件名只保留车号，不包含本子名称
                    zip_name = f"JM{entity_id}.zip"
                    zip_path = os.path.join(str(zip_root), zip_name)
                    try:
                        # zip 内顶层目录 = 本子文件夹（车号-标题），
                        # 单章下载时保留 车号-标题/章节序号 的层级
                        arc_root = os.path.relpath(
                            target_dir, self._download_root()
                        )
                        self._zip_dir(target_dir, zip_path, arc_root)
                        zip_files.append((zip_name, zip_path))
                    except Exception as e:
                        self.logger.exception(f"打包失败: {target_dir}")
                        summary.append(f"  ⚠️ 打包失败: {e}")

            # 1. 先发送本子的压缩包
            if self._cfg("zip_after_download", True) and zip_files:
                for zip_name, zip_path in zip_files:
                    if self._cfg("send_file", True):
                        file_chain = MessageChain()
                        file_chain.chain.append(File(name=zip_name, file=zip_path))
                        self._bump_platform_timeouts()
                        try:
                            await self.context.send_message(umo, file_chain)
                        except Exception as e:
                            # 大文件上传超时（Timed out）时，文件往往已送达，
                            # 此时不发送“发送失败”的误导提示，仅记录日志。
                            if _looks_like_timeout(e):
                                self.logger.warning(
                                    f"压缩包发送超时（可能已送达）: {zip_name}, {e}"
                                )
                                continue
                            self.logger.exception("发送压缩包失败")
                            await send_text(
                                f"📦 压缩包已生成，但发送失败: {e}\n路径: {zip_path}"
                            )
                    else:
                        await send_text(f"📦 压缩包路径: {zip_path}")

            # 2. 引用回复发送者：下载完成
            reply_text = str(self._cfg("finish_reply", "你的本子下载完成，已发送给你"))
            reply_chain = MessageChain()
            if task.reply_message_id:
                mid: Any = task.reply_message_id
                # Telegram 等平台要求回复的消息 ID 为 int
                if str(mid).isdigit():
                    mid = int(mid)
                reply_chain.chain.append(Reply(id=mid))
            reply_chain.message(f"✅ {reply_text}")
            if failed_any:
                reply_chain.message(
                    "⚠️ 部分内容下载失败，可重试下载（已缓存图片会跳过）。"
                )
            if not (self._cfg("zip_after_download", True) and zip_files):
                reply_chain.message("📁 文件保存在: " + str(self._download_root()))
            try:
                await self.context.send_message(umo, reply_chain)
            except Exception as e:
                self.logger.exception("发送下载完成消息失败")
                await send_text(
                    f"✅ {reply_text}\n下载结果: {self._download_root()}\n"
                    f"（消息发送失败: {e}）"
                )

            # 可选：发送后删除 zip
            if self._cfg("delete_zip_after_send", False):
                for _, zip_path in zip_files:
                    try:
                        os.remove(zip_path)
                    except OSError:
                        pass

            task.status = "done"
            task.message = "\n".join(summary)

        except asyncio.CancelledError:
            task.status = "cancelled"
            task.message = "任务已取消"
            raise
        except Exception as e:
            task.status = "failed"
            task.message = str(e)
            self.logger.exception("下载任务失败")
            await send_text("❌ 下载错误，请重试或联系管理员。")
        finally:
            jm_logger.removeHandler(forwarder)
            if task.dedup_key:
                _release_cmd_dedup(task.dedup_key)
            # 会话内没有其他运行中的任务时，清空该会话的消息去重记录，
            # 避免误伤用户随后顺序发起的下载
            if not any(
                t.session_key == umo and t.status == "running"
                for t in self._tasks.values()
            ):
                _clear_session_send_dedup(umo)

    # ------------------------------------------------------------------
    # 指令组
    # ------------------------------------------------------------------

    @filter.command_group("jm")
    def jm(self) -> None:
        """JMComic：下载 / 搜索 / 查看本子。输入 /jm help 查看帮助。"""

    @jm.command("help")
    async def help_cmd(self, event: AstrMessageEvent) -> None:
        """查看 JMComic 插件帮助"""
        help_text = (
            "🛠 JMComic 插件使用说明\n"
            "• /jm <车号...> — 下载本子，支持多个车号，"
            "章节号加 p 前缀，如: /jm 123 p456\n"
            "• /jm info <车号> — 查看本子详情\n"
            "• /jm search <关键词> — 站内搜索\n"
            "• /jm status — 查看本会话下载任务\n"
            "• /jm cancel <任务id> — 取消下载任务\n"
            "车号支持直接粘贴文本/链接，如 JM350234、"
            "https://18comic.vip/album/350234/\n"
            "下载完成后机器人会主动发送压缩包（部分平台不支持文件消息）。"
        )
        yield event.plain_result(help_text)

    # 根指令：/jm <车号...> 直接下载（不再需要 download 前缀）。
    # 命令过滤器保证需要唤醒前缀（/jm、@机器人 等），
    # 正则负向断言排除 help/info/search/status/cancel 等子命令。
    @filter.command("jm")
    @filter.regex(r"^(?!jm\s+(?:help|info|i|查看|search|s|搜|status|cancel)(?:\s|$))")
    async def download(self, event: AstrMessageEvent, ids: GreedyStr) -> None:
        """下载本子/章节（后台执行，完成后发送文件）"""
        if not self._check_permission(event):
            yield event.plain_result("⛔ 你没有权限使用下载功能（仅管理员可用，"
                                     "或到插件配置把 permission 改为 everyone）。")
            return

        jm = self._get_jm()
        if jm is None:
            yield event.plain_result(
                "❌ jmcomic 未安装或导入失败。请执行: pip install jmcomic"
            )
            return

        try:
            albums, photos = self._parse_ids(jm, ids)
        except Exception as e:
            yield event.plain_result(f"❌ 车号解析失败: {e}")
            return

        if not albums and not photos:
            yield event.plain_result("❌ 没有识别到车号，示例: /jm 123 p456")
            return

        session_key = event.unified_msg_origin
        max_concurrent = max(1, int(self._cfg("max_concurrent", 2)))
        if self._active_task_count(session_key) >= max_concurrent:
            yield event.plain_result(
                f"⚠️ 当前会话已有 {max_concurrent} 个任务在运行，请稍后再试。"
            )
            return

        # 去重：同一会话同一批车号的指令只启动一个任务
        dedup_key = (session_key, tuple(sorted(albums)), tuple(sorted(photos)))
        if not _claim_cmd_dedup(dedup_key):
            event.stop_event()  # 重复指令静默忽略，同时阻止事件流向 LLM
            return

        task = DownloadTask(
            task_id=f"{int(time.time())}{len(self._tasks) % 100:02d}",
            session_key=session_key,
            albums=albums,
            photos=photos,
            created_at=time.time(),
            reply_message_id=str(
                getattr(getattr(event, "message_obj", None), "message_id", "") or ""
            ),
            dedup_key=dedup_key,
        )
        self._tasks[task.task_id] = task
        task.asyncio_task = asyncio.create_task(
            self._download_job(task, session_key, albums, photos)
        )
        # 成功启动任务后不回复任何消息，后续进度由后台任务发送。
        # 必须标记事件已处理，否则 AstrBot 会把这条指令消息继续交给 LLM，
        # 导致大模型也回复一条消息。
        event.stop_event()

    @jm.command("info", alias={"i", "查看"})
    async def info(self, event: AstrMessageEvent, album_id: str) -> None:
        """查看本子详情（不下载）"""
        if not self._check_permission(event):
            yield event.plain_result("⛔ 你没有权限使用本指令。")
            return
        jm = self._get_jm()
        if jm is None:
            yield event.plain_result("❌ jmcomic 未安装或导入失败。")
            return
        try:
            aid = str(jm.JmcomicText.parse_to_jm_id(album_id))
            option = self._build_option(jm)
            client = option.new_jm_client()
            album = client.get_album_detail(aid)
            yield event.plain_result(self._format_album(album))
        except Exception as e:
            self.logger.exception("查询本子详情失败")
            yield event.plain_result(f"❌ 查询失败: {e}")

    @jm.command("search", alias={"s", "搜"})
    async def search(self, event: AstrMessageEvent, keyword: GreedyStr) -> None:
        """站内搜索本子"""
        if not self._check_permission(event):
            yield event.plain_result("⛔ 你没有权限使用本指令。")
            return
        jm = self._get_jm()
        if jm is None:
            yield event.plain_result("❌ jmcomic 未安装或导入失败。")
            return
        keyword = keyword.strip()
        if not keyword:
            yield event.plain_result("❌ 请输入关键词，示例: /jm search 全彩 人妻")
            return
        try:
            option = self._build_option(jm)
            client = option.new_jm_client()
            page = client.search_site(keyword, page=1)
            limit = max(1, int(self._cfg("search_max", 5)))
            lines = [f"🔍 搜索 [{keyword}] 结果 (共 {page.total} 条):"]
            for i, (aid, title, tags) in enumerate(page.iter_id_title_tag(), 1):
                if i > limit:
                    lines.append(f"… 共 {page.total} 条，仅显示前 {limit} 条")
                    break
                tag_str = ", ".join(tags[:5]) if tags else ""
                lines.append(f"{i}. JM{aid}《{title}》")
                if tag_str:
                    lines.append(f"   🏷 {tag_str}")
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            self.logger.exception("搜索失败")
            yield event.plain_result(f"❌ 搜索失败: {e}")

    @jm.command("status")
    async def status(self, event: AstrMessageEvent) -> None:
        """查看本会话的后台下载任务"""
        session_key = event.unified_msg_origin
        tasks = [
            t for t in self._tasks.values() if t.session_key == session_key
        ]
        if not tasks:
            yield event.plain_result("📭 当前会话没有下载任务。")
            return
        lines = ["📋 当前会话的下载任务:"]
        for t in tasks[-10:]:
            ids = (t.albums or []) + [f"p{p}" for p in t.photos]
            lines.append(
                f"• [{t.task_id}] {'/'.join(ids)} → {t.status}"
                + (f" ({t.message[:60]})" if t.message else "")
            )
        yield event.plain_result("\n".join(lines))

    @jm.command("cancel")
    async def cancel(self, event: AstrMessageEvent, task_id: str) -> None:
        """取消下载任务"""
        task = self._tasks.get(task_id.strip())
        if task is None or task.session_key != event.unified_msg_origin:
            yield event.plain_result("❌ 找不到该任务（只能取消自己会话的任务）。")
            return
        if task.status != "running":
            yield event.plain_result(f"任务 [{task_id}] 状态为 {task.status}，无需取消。")
            return
        if task.asyncio_task:
            task.asyncio_task.cancel()
        yield event.plain_result(
            f"🛑 已请求取消任务 [{task_id}]。"
            "（底层线程无法强制中断，已下载图片会被缓存，重试会自动跳过）"
        )

    # ------------------------------------------------------------------
    # 格式化
    # ------------------------------------------------------------------

    @staticmethod
    def _format_album(album) -> str:
        authors = ", ".join(album.authors) if album.authors else album.author
        lines = [
            f"📖 标题: {album.name}",
            f"🆔 ID: JM{album.album_id}",
            f"✍️ 作者: {authors}",
            f"📅 发布: {album.pub_date}  更新: {album.update_date}",
            f"📄 总页数: {album.page_count}  章节数: {len(album)}",
            f"👀 观看: {album.views}  ❤️ {album.likes}  💬 {album.comment_count}",
        ]
        if album.tags:
            lines.append(f"🏷️ 标签: {', '.join(album.tags)}")
        if album.actors:
            lines.append(f"🎭 人物: {', '.join(album.actors)}")
        if album.works:
            lines.append(f"📚 作品: {', '.join(album.works)}")
        lines.append(f"📑 章节 ({len(album)}):")
        for idx, (pid, _pindex, pname, _pdate) in enumerate(album.episode_list, 1):
            lines.append(f"  {idx}. {pname} (id: {pid})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def terminate(self) -> None:
        """插件卸载/停用时取消所有后台任务。"""
        for task in self._tasks.values():
            if task.status == "running" and task.asyncio_task:
                task.asyncio_task.cancel()
        self._tasks.clear()
