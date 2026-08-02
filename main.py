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
- 直接发送 /jm <车号> 即可开始下载，例如 /jm 123 或 /jm 123 p456。
  车号支持任意文本，例如 123、JM123、https://18comic.vip/album/123/；
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
import shutil
import smtplib
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
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
    import pyzipper

    PYZIPPER_AVAILABLE = True
except Exception:  # pragma: no cover - 未安装 pyzipper 时退回普通 zip
    pyzipper = None  # type: ignore
    PYZIPPER_AVAILABLE = False

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
    error_trace: str = ""
    ticket_id: str = ""
    reply_message_id: str = ""
    dedup_key: tuple = ()


def _safe_filename(text: str, max_len: int = 60) -> str:
    """把标题清洗成可用于文件名的字符串。"""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len] or "untitled"


class JmcomicPlugin(Star):
    def __init__(self, context: Context, config: Optional[dict] = None) -> None:
        super().__init__(context)
        self.config: dict = config or {}
        self._tasks: dict[str, DownloadTask] = {}
        self._tickets: dict[str, dict] = {}
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
        return jm.JmOption.construct(
            {
                "dir_rule": {
                    "rule": "Bd_Pname",
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

    def _zip_dir(self, src_dir: str, zip_path: str, password: str = "") -> None:
        """把整个文件夹打包为 zip；配置密码且安装了 pyzipper 时生成 AES 加密 zip。"""
        src_dir = os.path.abspath(src_dir)
        zip_path = os.path.abspath(zip_path)
        password = str(password or "")
        if password and PYZIPPER_AVAILABLE:
            with pyzipper.AESZipFile(
                zip_path,
                "w",
                compression=pyzipper.ZIP_DEFLATED,
                encryption=pyzipper.WZ_AES,
            ) as zf:
                zf.setpassword(password.encode("utf-8"))
                self._zip_write_tree(zf, src_dir, zip_path)
        else:
            if password:
                self.logger.warning(
                    "已配置 zip 加密密码，但未安装 pyzipper，将退回普通 zip。"
                    "请执行: pip install pyzipper"
                )
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                self._zip_write_tree(zf, src_dir, zip_path)

    @staticmethod
    def _zip_write_tree(zf, src_dir: str, zip_path: str) -> None:
        """把 src_dir 下所有文件写入 zip，排除 zip 输出文件自身（防止自打包）。"""
        src_dir = os.path.abspath(src_dir)
        zip_path = os.path.abspath(zip_path)
        for root, _, files in os.walk(src_dir):
            for name in files:
                full = os.path.abspath(os.path.join(root, name))
                if full == zip_path:
                    continue
                arc = os.path.relpath(full, src_dir)
                zf.write(full, arc)

    # ------------------------------------------------------------------
    # 报错反馈（工单 + 邮件）
    # ------------------------------------------------------------------

    def _register_ticket(
        self, task: DownloadTask, umo: str, albums: list[str], photos: list[str]
    ) -> str:
        """登记一个报错工单，返回工单号。只有报错时才会创建，/jm report 才能用。"""
        ticket_id = f"T{int(time.time())}{len(self._tickets) % 100:02d}"
        self._tickets[ticket_id] = {
            "ticket_id": ticket_id,
            "session_key": umo,
            "created_at": time.time(),
            "album_ids": list(albums),
            "photo_ids": list(photos),
            "fetched_album_id": task.fetched_album_id,
            "fetched_title": task.fetched_title,
            "error": task.message,
            "traceback": task.error_trace,
        }
        # 简单清理，最多保留 100 个工单
        if len(self._tickets) > 100:
            oldest = min(self._tickets, key=lambda k: self._tickets[k]["created_at"])
            self._tickets.pop(oldest, None)
        return ticket_id

    @staticmethod
    def _extract_history_text(content) -> str:
        """把平台消息历史 content 字段还原成文本。"""
        try:
            if isinstance(content, dict):
                parts = content.get("message", [])
            else:
                parts = content or []
        except Exception:
            return ""
        texts: list[str] = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type", "")
            if ptype in ("plain", "text"):
                t = str(p.get("text", "") or "")
            elif ptype == "at":
                t = f"@{p.get('name') or p.get('user_id') or '?'}"
            elif ptype == "reply":
                t = f"[回复 {p.get('sender_name')}] {p.get('text') or ''}"
            else:
                t = f"[{ptype}]"
            if t:
                texts.append(t)
        return " ".join(texts).strip()

    async def _get_chat_history(self, event: AstrMessageEvent, limit: int = 50) -> str:
        """从 AstrBot 平台消息历史中读取该会话最近的聊天记录。"""
        try:
            mgr = getattr(self.context, "message_history_manager", None)
            if mgr is None or getattr(mgr, "db", None) is None:
                return ""
            records = await mgr.get(
                platform_id=event.get_platform_id(),
                user_id=event.unified_msg_origin,
                page=1,
                page_size=max(1, int(limit)),
            )
        except Exception as e:
            self.logger.warning(f"获取对话记录失败: {e}")
            return ""
        lines: list[str] = []
        for r in records:
            name = str(r.sender_name or r.sender_id or "?").strip()
            text = self._extract_history_text(getattr(r, "content", None))
            if not text:
                continue
            ts = getattr(r, "created_at", None)
            t = ts.strftime("%m-%d %H:%M:%S") if ts else ""
            lines.append(f"[{t}] {name}: {text}")
        return "\n".join(lines)

    def _build_report_body(self, ticket: dict, history: str) -> str:
        """构造反馈邮件正文。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            "JMComic 插件报错反馈",
            "=" * 40,
            f"反馈时间: {now}",
            f"工单号: {ticket['ticket_id']}",
            f"会话: {ticket['session_key']}",
            f"下载任务: album={ticket['album_ids'] or '无'}, "
            f"photo={ticket['photo_ids'] or '无'}",
        ]
        if ticket.get("fetched_album_id"):
            lines.append(
                f"本子: JM{ticket['fetched_album_id']}"
                f"《{ticket.get('fetched_title') or '未知标题'}》"
            )
        lines += [
            "",
            "===== 错误信息 =====",
            str(ticket.get("error") or "未知错误"),
            "",
            "===== 错误堆栈 =====",
            str(ticket.get("traceback") or "（无）"),
            "",
            "===== 对话记录 =====",
            history
            or "（未获取到对话记录：群聊需在 WebUI 平台设置中开启群聊消息记录）",
        ]
        return "\n".join(lines)

    def _send_email(self, subject: str, body: str) -> None:
        """同步发送邮件（在 asyncio.to_thread 中运行）。"""
        host = str(self._cfg("smtp_host", "") or "").strip()
        port = int(self._cfg("smtp_port", 465) or 465)
        user = str(self._cfg("smtp_user", "") or "").strip()
        password = str(self._cfg("smtp_password", "") or "")
        to_addr = str(self._cfg("report_email", "") or "").strip()
        if not host or not user or not to_addr:
            raise RuntimeError(
                "未配置 SMTP 或接收邮箱，请到插件配置填写 "
                "smtp_host / smtp_user / smtp_password / report_email。"
            )

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to_addr
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(body)

        use_ssl = bool(self._cfg("smtp_use_ssl", True))
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            try:
                server.starttls()
            except smtplib.SMTPNotSupportedError:
                pass
        try:
            server.login(user, password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass

    async def _send_report(
        self, event: AstrMessageEvent, ticket: dict
    ) -> tuple[bool, str]:
        """发送报错反馈邮件，返回 (是否成功, 用户可见消息)。"""
        try:
            history = await self._get_chat_history(
                event, int(self._cfg("report_history_lines", 50))
            )
        except Exception as e:
            history = ""
            self.logger.warning(f"获取对话记录失败: {e}")
        subject = str(self._cfg("report_email_subject", "JMComic 插件报错反馈"))
        body = self._build_report_body(ticket, history)
        try:
            await asyncio.to_thread(self._send_email, subject, body)
            return True, "✅ 报错反馈已发送给管理员邮箱，感谢反馈！"
        except Exception as e:
            self.logger.exception("反馈邮件发送失败")
            return False, f"❌ 邮件发送失败: {e}"

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
            zipped_dirs: dict[str, str] = {}  # zip name -> 对应的原始下载目录
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
                    # 安全校验：只打包下载目录内的子目录，防止目录配置异常时
                    # 把整个下载根目录（含其他车号/他人的文件）打包进去
                    abs_target = os.path.abspath(target_dir)
                    download_root = os.path.abspath(str(self._download_root()))
                    try:
                        common = os.path.commonpath([download_root, abs_target])
                    except ValueError:
                        common = ""
                    if common != download_root or abs_target == download_root:
                        self.logger.warning(
                            f"跳过打包（不在下载目录内）: {abs_target}"
                        )
                        summary.append("  ⚠️ 跳过打包: 目标目录不在下载目录内")
                    else:
                        zip_root = self._zip_root()
                        zip_root.mkdir(parents=True, exist_ok=True)
                        # 压缩包文件名只保留车号+时间戳+随机后缀，不含本子名称；
                        # 时间戳/随机后缀保证不同任务、不同会话下载同一车号时
                        # 不会互相覆盖同名 zip，避免发送错文件
                        zip_name = (
                            f"JM{entity_id}_{int(time.time())}"
                            f"_{uuid.uuid4().hex[:8]}.zip"
                        )
                        zip_path = os.path.join(str(zip_root), zip_name)
                        try:
                            self._zip_dir(
                                target_dir,
                                zip_path,
                                str(self._cfg("zip_password", "") or ""),
                            )
                            zip_files.append((zip_name, zip_path))
                            zipped_dirs[zip_name] = target_dir
                        except Exception as e:
                            self.logger.exception(f"打包失败: {target_dir}")
                            summary.append(f"  ⚠️ 打包失败: {e}")

            # 1. 先发送本子的压缩包
            sent_zip_names: list[str] = []  # 已成功发送（含超时视为已送达）的压缩包
            if self._cfg("zip_after_download", True) and zip_files:
                for zip_name, zip_path in zip_files:
                    if self._cfg("send_file", True):
                        file_chain = MessageChain()
                        file_chain.chain.append(File(name=zip_name, file=zip_path))
                        self._bump_platform_timeouts()
                        try:
                            await self.context.send_message(umo, file_chain)
                            sent_zip_names.append(zip_name)
                        except Exception as e:
                            # 大文件上传超时（Timed out）时，文件往往已送达，
                            # 此时不发送“发送失败”的误导提示，仅记录日志。
                            if _looks_like_timeout(e):
                                self.logger.warning(
                                    f"压缩包发送超时（可能已送达）: {zip_name}, {e}"
                                )
                                sent_zip_names.append(zip_name)
                                continue
                            self.logger.exception("发送压缩包失败")
                            await send_text(
                                f"📦 压缩包已生成，但发送失败: {e}\n路径: {zip_path}"
                            )
                    else:
                        await send_text(f"📦 压缩包路径: {zip_path}")

            # 1.1 发送完成后删除原始下载文件（压缩包已发给用户，原图不再需要）。
            #     只在压缩包确实发送成功（或超时视为已送达）时删除；
            #     发送失败时保留原图，避免用户既没收到包也拿不到文件。
            if sent_zip_names:
                download_root = os.path.abspath(str(self._download_root()))
                for zip_name in sent_zip_names:
                    target_dir = zipped_dirs.get(zip_name)
                    if not target_dir:
                        continue
                    abs_dir = os.path.abspath(target_dir)
                    # 安全校验：只删除插件下载目录下的文件
                    try:
                        common = os.path.commonpath([download_root, abs_dir])
                    except ValueError:
                        continue
                    if common != download_root or abs_dir == download_root:
                        self.logger.warning(
                            f"跳过删除（不在下载目录内）: {abs_dir}"
                        )
                        continue
                    try:
                        shutil.rmtree(abs_dir)
                        self.logger.info(f"已删除发送后的原始下载目录: {abs_dir}")
                    except Exception as e:
                        self.logger.warning(f"删除原始下载目录失败: {abs_dir}: {e}")

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
            task.error_trace = traceback.format_exc()
            self.logger.exception("下载任务失败")
            ticket_id = self._register_ticket(task, umo, albums, photos)
            task.ticket_id = ticket_id
            if task.album_fetched:
                await send_text(
                    f"⚠️ 本子获取成功（JM{task.fetched_album_id}），但下载出错，"
                    f"请联系管理员解决。\n"
                    f"如需反馈，请发送：/jm report {ticket_id}"
                )
            else:
                await send_text(
                    f"❌ 下载出错，请联系管理员解决。\n"
                    f"如需反馈，请发送：/jm report {ticket_id}"
                )
        finally:
            jm_logger.removeHandler(forwarder)
            if task.dedup_key:
                _release_cmd_dedup(task.dedup_key)

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
            "章节号加 p 前缀，如: /jm 123 或 /jm 123 p456\n"
            "• /jm info <车号> — 查看本子详情\n"
            "• /jm search <关键词> — 站内搜索\n"
            "• /jm status — 查看本会话下载任务\n"
            "• /jm cancel <任务id> — 取消下载任务\n"
            "• /jm report <工单号> — 下载报错后，把错误信息和对话记录"
            "发送到管理员邮箱（仅在报错时可用）\n"
            "车号支持直接粘贴文本/链接，如 JM350234、"
            "https://18comic.vip/album/350234/\n"
            "下载完成后机器人会主动发送压缩包（部分平台不支持文件消息）。"
        )
        yield event.plain_result(help_text)

    @filter.regex(
        r"(?i)^jm\s+(?!(?:help|info|search|status|cancel|report|"
        r"download|d|dl|下|i|s|查看|搜|反馈|bug)\b)\S.*$"
    )
    async def download(self, event: AstrMessageEvent) -> None:
        """默认下载：/jm <车号...>，例如 /jm 123、/jm 123 p456（后台执行）"""
        # 正则过滤器不受 wake_prefix 制约，这里与标准指令保持一致：
        # 只有通过唤醒（/jm、@机器人、私聊）的消息才处理。
        if not getattr(event, "is_at_or_wake_command", False):
            return

        # 去掉 "jm" 前缀后按车号解析，例如 "123 p456" -> 本子 123 + 章节 456
        raw = (getattr(event, "message_str", "") or "").strip()
        ids = re.sub(r"^jm\s+", "", raw, flags=re.IGNORECASE).strip()

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
            yield event.plain_result("❌ 没有识别到车号，示例: /jm 123 或 /jm 123 p456")
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

    @jm.command("report", alias={"反馈", "bug"})
    async def report(self, event: AstrMessageEvent, ticket_id: str) -> None:
        """下载报错后，发送报错信息与对话记录到管理员邮箱（仅报错时可用）"""
        if not self._check_permission(event):
            yield event.plain_result("⛔ 你没有权限使用本指令。")
            return
        ticket = self._tickets.get(ticket_id.strip())
        if ticket is None or ticket["session_key"] != event.unified_msg_origin:
            yield event.plain_result(
                "❌ 无效的反馈指令：没有找到对应的报错记录。"
                "该指令只在下载出错后可用。"
            )
            return
        yield event.plain_result(f"📧 正在发送报错反馈（工单 {ticket_id}）…")
        ok, msg = await self._send_report(event, ticket)
        yield event.plain_result(msg)

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
