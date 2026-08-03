"""
JMComic 下载插件 (astrbot_plugin_jmcomic)
==========================================

基于 jmcomic (JMComic-Crawler-Python) 的 AstrBot 插件，提供：

- /jm <车号...>            后台下载本子/章节，完成后打包 zip/pdf 发送
- /jm info <车号>          查看本子详情（不下载）
- /jm search <关键词>      站内搜索
- /jm status               查看本会话的后台下载任务
- /jm cancel <任务id>      取消任务（不删除已下载文件）
- /jm help                 帮助

说明：
- 车号支持任意文本，例如 123、JM123、https://18comic.vip/album/123/；
  章节号以 p 开头，例如 p456。
- 下载任务在后台运行，完成后会主动推送 zip/pdf 打包文件（仅支持 Telegram、
  OneBot、QQ 官方机器人（websocket）平台，其他平台会提示不支持）。
- 数据默认存放在 AstrBot 的 data/plugin_data/jmcomic_downloader/ 下，
  可通过插件配置修改。
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
import shutil
import threading
import time
import zipfile
try:
    import pyzipper
except Exception:  # pragma: no cover - 可选依赖，未安装时仅加密功能不可用
    pyzipper = None  # type: ignore
try:
    import pymupdf as fitz
except Exception:  # pragma: no cover - 可选依赖，未安装时 PDF 功能不可用
    try:
        import fitz  # type: ignore
    except Exception:  # pragma: no cover
        fitz = None  # type: ignore
try:
    from PIL import Image
except Exception:  # pragma: no cover - 可选依赖，用于解码 webp 等 MuPDF 不支持的图片
    Image = None  # type: ignore
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
PDF_SUBDIR = "pdf"
PDF_BRIDGE_JPEG_QUALITY = 90  # webp 等图片经 Pillow 桥接转为 JPEG 的质量
PACK_FORMATS = ("zip", "pdf")  # 发送文件格式，二选一
CACHE_CSV_FILENAME = "cache_index.csv"
CACHE_CSV_FIELDS = [
    "album_id",
    "title",
    "pack_format",
    "file_path",
    "first_download_at",
    "last_sent_at",
    "send_count",
]
CACHE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 支持直接发送文件消息的平台类型（AstrBot 平台适配器注册名）
FILE_SEND_PLATFORMS = ("telegram", "aiocqhttp", "qq_official", "qqofficial")


def _platform_supports_file(platform_name: str) -> bool:
    """当前平台是否支持发送文件消息（仅 Telegram / OneBot / QQ 官方机器人 websocket）。"""
    name = (platform_name or "").replace("_", "").replace("-", "").lower()
    return name in {
        p.replace("_", "").replace("-", "").lower() for p in FILE_SEND_PLATFORMS
    }


def _event_platform_name(event: Any) -> str:
    """获取消息事件所属平台类型名（如 telegram / aiocqhttp / qq_official）。"""
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            return str(getter() or "")
        except Exception:
            pass
    return str(getattr(getattr(event, "platform", None), "name", "") or "")

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
    platform_name: str = ""
    dedup_key: tuple = ()


def _safe_filename(text: str, max_len: int = 60) -> str:
    """把标题清洗成可用于文件名的字符串。"""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len] or "untitled"


def _natural_key(path: str) -> list:
    """按路径中的数字段自然排序：保证 2 排在 10 前面、02 章排在 10 章前面。"""
    parts: list = []
    for seg in path.split("/"):
        for chunk in re.split(r"(\d+)", seg):
            if chunk.isdigit():
                parts.append((1, int(chunk)))
            elif chunk:
                parts.append((0, chunk))
    return parts


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
        # 打包文件缓存：内存 dict 为准，CSV 只做持久化快照
        self._cache_rows: dict[tuple[str, str], dict] = {}
        self._cache_loaded = False
        self._cache_lock = threading.RLock()
        self._cache_cleanup_task: Optional[asyncio.Task] = None
        self._cache_sending: set[str] = set()
        # 启动时补录旧打包文件，并清理已过期/失效的记录
        try:
            self._cache_backfill()
            self._cache_sweep()
        except Exception:
            self.logger.exception("启动时初始化缓存索引失败")
        # 常驻定时清理：即使没有新的下载指令也会自动清理过期缓存
        self._ensure_cache_cleanup()

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

    def _pack_mode(self) -> str:
        """打包文件处理模式：csv_cache=使用 CSV 索引缓存；delete_after_send=发送后删除。"""
        mode = str(self._cfg("pack_mode", "csv_cache") or "csv_cache").lower()
        return mode if mode in ("csv_cache", "delete_after_send") else "csv_cache"

    def _download_root(self) -> Path:
        custom = self._cfg("download_dir", "")
        if custom:
            return Path(os.path.abspath(os.path.expanduser(str(custom))))
        return Path(get_astrbot_plugin_data_path()) / PLUGIN_DATA_DIR / DEFAULT_DOWNLOAD_SUBDIR

    def _pack_root(self) -> Path:
        """ZIP 输出目录；配置键为 zip_dir。"""
        custom = self._cfg("zip_dir", "")
        if custom:
            return Path(os.path.abspath(os.path.expanduser(str(custom))))
        return Path(get_astrbot_plugin_data_path()) / PLUGIN_DATA_DIR / ZIP_SUBDIR

    def _pdf_root(self) -> Path:
        """PDF 输出目录，与 ZIP 分开存放；配置键为 pdf_dir。"""
        custom = self._cfg("pdf_dir", "")
        if custom:
            return Path(os.path.abspath(os.path.expanduser(str(custom))))
        return Path(get_astrbot_plugin_data_path()) / PLUGIN_DATA_DIR / PDF_SUBDIR

    # ------------------------------------------------------------------
    # 打包文件缓存（CSV 索引 + 过期清理）
    # ------------------------------------------------------------------

    def _cache_csv_path(self) -> Path:
        """缓存索引 CSV 路径；默认放在插件数据目录下。"""
        custom = self._cfg("cache_csv_path", "")
        if custom:
            return Path(os.path.abspath(os.path.expanduser(str(custom))))
        return Path(get_astrbot_plugin_data_path()) / PLUGIN_DATA_DIR / CACHE_CSV_FILENAME

    def _cache_ttl_seconds(self) -> float:
        try:
            hours = int(self._cfg("cache_ttl_hours", 24))
        except (TypeError, ValueError):
            hours = 24
        return max(0.0, float(hours * 3600))

    def _cache_now_str(self) -> str:
        return time.strftime(CACHE_TIME_FORMAT)

    @staticmethod
    def _cache_parse_time(text: str) -> Optional[float]:
        """把 CSV 里的时间字符串转成时间戳；解析失败返回 None。"""
        try:
            return time.mktime(time.strptime(text, CACHE_TIME_FORMAT))
        except (TypeError, ValueError, OverflowError):
            return None

    def _cache_load(self) -> None:
        """把 CSV 读入内存（只读一次，之后以内存为准）。"""
        with self._cache_lock:
            if self._cache_loaded:
                return
            self._cache_rows = {}
            path = self._cache_csv_path()
            try:
                if path.is_file():
                    with open(path, "r", encoding="utf-8-sig", newline="") as f:
                        for row in csv.DictReader(f):
                            aid = (row.get("album_id") or "").strip()
                            fmt = (row.get("pack_format") or "").strip()
                            if aid and fmt and row.get("file_path"):
                                self._cache_rows[(aid, fmt)] = row
            except Exception:
                self.logger.exception(f"读取缓存索引失败，已备份为 .bak: {path}")
                try:
                    os.replace(str(path), str(path.with_suffix(".csv.bak")))
                except OSError:
                    pass
                self._cache_rows = {}
            self._cache_loaded = True

    def _cache_save(self) -> None:
        """把内存索引原子写回 CSV（临时文件 + os.replace，避免读到半截文件）。"""
        with self._cache_lock:
            path = self._cache_csv_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=CACHE_CSV_FIELDS)
                    writer.writeheader()
                    for row in sorted(
                        self._cache_rows.values(),
                        key=lambda r: (r.get("album_id", ""), r.get("pack_format", "")),
                    ):
                        writer.writerow({k: row.get(k, "") for k in CACHE_CSV_FIELDS})
                os.replace(str(tmp), str(path))
            except Exception:
                self.logger.exception(f"保存缓存索引失败: {path}")

    def _cache_find(self, album_id: str, pack_format: str) -> Optional[dict]:
        """按 (车号, 格式) 查缓存记录；未启用缓存时返回 None。"""
        if self._pack_mode() != "csv_cache":
            return None
        self._cache_load()
        with self._cache_lock:
            row = self._cache_rows.get((album_id, pack_format))
            return dict(row) if row else None

    def _cache_upsert(self, album_id: str, title: str, pack_format: str, file_path: str) -> None:
        """发送成功后登记/更新一行；首次下载时间只在首次写入。"""
        if self._pack_mode() != "csv_cache":
            return
        self._cache_load()
        now = self._cache_now_str()
        with self._cache_lock:
            row = self._cache_rows.get((album_id, pack_format))
            if row is None:
                row = {k: "" for k in CACHE_CSV_FIELDS}
                row["album_id"] = album_id
                row["pack_format"] = pack_format
                row["first_download_at"] = now
                row["send_count"] = "0"
                self._cache_rows[(album_id, pack_format)] = row
            if title:
                row["title"] = title
            row["file_path"] = os.path.abspath(str(file_path))
            row["last_sent_at"] = now
            try:
                row["send_count"] = str(int(row.get("send_count") or 0) + 1)
            except (TypeError, ValueError):
                row["send_count"] = "1"
            self._cache_save()

    def _cache_remove(self, album_id: str, pack_format: str) -> None:
        with self._cache_lock:
            if self._cache_rows.pop((album_id, pack_format), None) is not None:
                self._cache_save()

    def _delete_cached_files(self, album_id: str, file_path: str) -> bool:
        """删除过期本子的打包文件；按配置决定是否连同原图目录删除。

        返回是否删除成功；失败时（如文件被占用）由调用方保留记录以便下次重试。
        """
        ok = True
        try:
            if file_path and os.path.isfile(file_path):
                os.remove(file_path)
        except OSError as e:
            ok = False
            self.logger.warning(f"删除缓存文件失败: {file_path}: {e}")
        if ok and self._cfg("cache_delete_raw", True):
            try:
                for folder in self._download_root().glob(f"{album_id}-*"):
                    if folder.is_dir():
                        shutil.rmtree(str(folder))
            except OSError as e:
                ok = False
                self.logger.warning(f"删除原图目录失败: {album_id}: {e}")
        return ok

    @staticmethod
    def _prune_empty_dirs(path: str, root: str) -> None:
        """逐级删除空的上级目录（到 download_root 为止），删除失败即停止。"""
        parent = os.path.dirname(path)
        while parent and os.path.abspath(parent) != os.path.abspath(root):
            try:
                if not os.path.isdir(parent) or os.listdir(parent):
                    break
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)

    def _cache_sweep(self) -> int:
        """清理过期/失效行：距最近发送超过 TTL 的文件删除并移除记录。"""
        if self._pack_mode() != "csv_cache":
            return 0
        self._cache_load()
        now = time.time()
        ttl = self._cache_ttl_seconds()
        removed = []
        with self._cache_lock:
            for (aid, fmt), row in list(self._cache_rows.items()):
                path = row.get("file_path") or ""
                if path and os.path.abspath(path) in self._cache_sending:
                    continue  # 正在发送的文件不删
                last = self._cache_parse_time(row.get("last_sent_at") or "")
                expired = ttl > 0 and last is not None and now - last >= ttl
                file_exists = bool(path) and os.path.isfile(path)
                if expired:
                    # 删除成功才移除记录；删除失败（如文件被占用）留到下次重试
                    if self._delete_cached_files(aid, path):
                        removed.append((aid, fmt, path))
                        self._cache_rows.pop((aid, fmt), None)
                elif not file_exists:
                    # 文件已丢失的记录直接清掉，避免残留脏行
                    removed.append((aid, fmt, path))
                    self._cache_rows.pop((aid, fmt), None)
            if removed:
                self._cache_save()
        for aid, fmt, path in removed:
            self.logger.info(f"缓存清理: JM{aid} [{fmt}] -> {path}")
        return len(removed)

    def _cache_backfill(self) -> None:
        """启动时把 zip/pdf 目录里已存在的打包文件补录进索引（按文件修改时间）。"""
        if self._pack_mode() != "csv_cache":
            return
        self._cache_load()
        changed = False
        with self._cache_lock:
            for fmt, root in (("zip", self._pack_root()), ("pdf", self._pdf_root())):
                try:
                    files = list(root.glob(f"JM*.{fmt}"))
                except OSError:
                    continue
                for p in files:
                    m = re.match(r"JM(\d+)\.", p.name)
                    if not m:
                        continue
                    aid = m.group(1)
                    key = (aid, fmt)
                    if key in self._cache_rows:
                        continue
                    try:
                        mtime = time.strftime(
                            CACHE_TIME_FORMAT, time.localtime(p.stat().st_mtime)
                        )
                    except OSError:
                        continue
                    self._cache_rows[key] = {
                        "album_id": aid,
                        "title": "",
                        "pack_format": fmt,
                        "file_path": str(p),
                        "first_download_at": mtime,
                        "last_sent_at": mtime,
                        "send_count": "0",
                    }
                    changed = True
            if changed:
                self._cache_save()

    async def _cache_cleanup_loop(self) -> None:
        """后台定时清理：启动后立即清理一次，之后按「清理间隔」与 TTL 中较小值定期检查过期缓存。"""
        while True:
            try:
                self._cache_sweep()
            except Exception:
                self.logger.exception("缓存清理任务出错")
            try:
                try:
                    interval = max(
                        60, int(self._cfg("cache_cleanup_interval_minutes", 30)) * 60
                    )
                except (TypeError, ValueError):
                    interval = 30 * 60
                ttl = self._cache_ttl_seconds()
                if ttl > 0:
                    interval = min(interval, max(60.0, ttl))
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    def _ensure_cache_cleanup(self) -> None:
        """启动常驻清理任务；没有运行中的事件循环时等待下一次命令再启动。"""
        if self._pack_mode() != "csv_cache":
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 当前没有运行中的事件循环，等待下一次命令再启动
        if self._cache_cleanup_task is None or self._cache_cleanup_task.done():
            self._cache_cleanup_task = asyncio.create_task(
                self._cache_cleanup_loop()
            )
            self.logger.info(
                "打包文件缓存清理任务已启动（默认每 %s 分钟检查一次过期缓存）",
                self._cfg("cache_cleanup_interval_minutes", 30),
            )

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

    def _zip_dir(
        self, src_dir: str, zip_path: str, arc_root: str = "", password: str = ""
    ) -> None:
        """把整个文件夹打包为 zip，zip 内以 arc_root 作为顶层目录名。

        password 非空时使用 pyzipper 的 AES-256 加密
        （加密压缩包在 QQ 等平台不易触发文件发送限制）。
        """
        src_dir = os.path.abspath(src_dir)
        zip_path_abs = os.path.abspath(zip_path)
        password = (password or "").strip()
        if password:
            if pyzipper is None:
                raise RuntimeError(
                    "未安装 pyzipper，无法加密压缩包，请执行 pip install pyzipper"
                )
            zf_cls = pyzipper.AESZipFile
            zf_kwargs = {"encryption": pyzipper.WZ_AES}
        else:
            zf_cls = zipfile.ZipFile
            zf_kwargs = {}
        with zf_cls(zip_path, "w", zipfile.ZIP_DEFLATED, **zf_kwargs) as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))
            for root, _, files in os.walk(src_dir):
                for name in files:
                    full = os.path.join(root, name)
                    # 防止把输出 zip 本身打进去（zip_dir 配置在下载目录内时）
                    if os.path.abspath(full) == zip_path_abs:
                        continue
                    rel = os.path.relpath(full, src_dir)
                    arc = os.path.join(arc_root, rel) if arc_root else rel
                    zf.write(full, arc.replace("\\", "/"))

    def _pdf_dir(
        self, src_dir: str, pdf_path: str, password: str = ""
    ) -> tuple[int, int]:
        """把文件夹内的所有图片按章节/页序合成为一个 PDF。

        优先用 PyMuPDF 直接读取图片（jpg/png 无损嵌入）；
        webp 等 MuPDF 不支持的格式先用 Pillow 解码成 JPEG 字节再交给 PyMuPDF。
        password 非空时使用 AES-256 加密（PDF 打开密码）。
        返回 (成功页数, 失败页数)。
        """
        if fitz is None:
            raise RuntimeError(
                "未安装 pymupdf，无法生成 PDF，请执行 pip install pymupdf"
            )

        src_dir = os.path.abspath(src_dir)
        image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
        images: list[str] = []
        for root, _, files in os.walk(src_dir):
            for name in files:
                if os.path.splitext(name)[1].lower() in image_exts:
                    images.append(os.path.join(root, name))
        if not images:
            raise RuntimeError("未找到图片文件，无法生成 PDF")
        images.sort(
            key=lambda p: _natural_key(
                os.path.relpath(p, src_dir).replace("\\", "/")
            )
        )

        password = (password or "").strip()
        doc = fitz.open()
        ok = 0
        failed = 0
        try:
            for full in images:
                try:
                    try:
                        img = fitz.open(full)
                    except Exception:
                        # MuPDF 不支持的格式（如 webp）：Pillow 解码后转 JPEG 字节
                        if Image is None:
                            raise RuntimeError(
                                "未安装 Pillow，无法解码图片，请执行 pip install pillow"
                            )
                        im = Image.open(full)
                        if im.mode != "RGB":
                            im = im.convert("RGB")
                        buf = io.BytesIO()
                        im.save(
                            buf,
                            "JPEG",
                            quality=PDF_BRIDGE_JPEG_QUALITY,
                        )
                        img = fitz.open(stream=buf.getvalue(), filetype="jpeg")
                    pdfbytes = img.convert_to_pdf()
                    img.close()
                    sub = fitz.open("pdf", pdfbytes)
                    doc.insert_pdf(sub)
                    sub.close()
                    ok += 1
                except Exception:
                    if Image is None:
                        raise  # Pillow 缺失属于环境问题，直接上报而不是跳过
                    failed += 1
                    continue
            if ok == 0:
                raise RuntimeError("所有图片均无法写入 PDF")
            if password:
                doc.save(
                    pdf_path,
                    encryption=fitz.PDF_ENCRYPT_AES_256,
                    owner_pw=password,
                    user_pw=password,
                    permissions=fitz.PDF_PERM_ACCESSIBILITY,
                )
            else:
                doc.save(pdf_path)
        finally:
            doc.close()
        return ok, failed

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

            # 逐条处理：统计成功/失败，按配置格式打包（zip/pdf 二选一）
            summary: list[str] = []
            failed_any = False
            pack_files: list[tuple[str, str, str, str]] = []  # (name, path, album_id, title)
            pack_dirs: list[str] = []  # 与 pack_files 一一对应的原图目录
            zip_password = str(self._cfg("zip_password", "") or "").strip()
            pack_format = str(self._cfg("pack_format", "zip") or "zip").lower()
            if pack_format not in PACK_FORMATS:
                pack_format = "zip"
            if pack_format == "pdf" and fitz is None:
                await send_text(
                    "⚠️ 未安装 pymupdf，无法生成 PDF，本次已自动回退为发送 ZIP"
                    "（请执行 pip install pymupdf）"
                )
                pack_format = "zip"
            pwd_hint = ""
            if zip_password:
                label = "打开密码" if pack_format == "pdf" else "解压密码"
                pwd_hint = f"\n🔑 {label}: {zip_password}"
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

                if os.path.isdir(target_dir):
                    pack_root = (
                        self._pdf_root()
                        if pack_format == "pdf"
                        else self._pack_root()
                    )
                    pack_root.mkdir(parents=True, exist_ok=True)
                    # 打包文件名只保留车号，不包含本子名称（zip/pdf 命名规则一致）
                    pack_name = f"JM{entity_id}.{pack_format}"
                    pack_path = os.path.join(str(pack_root), pack_name)
                    try:
                        if pack_format == "pdf":
                            _, failed_pages = self._pdf_dir(
                                target_dir, pack_path, zip_password
                            )
                            if failed_pages:
                                summary.append(
                                    f"  ⚠️ {failed_pages} 张图片未能写入 PDF"
                                )
                        else:
                            # zip 内顶层目录 = 本子文件夹（车号-标题），
                            # 单章下载时保留 车号-标题/章节序号 的层级
                            arc_root = os.path.relpath(
                                target_dir, self._download_root()
                            )
                            self._zip_dir(
                                target_dir, pack_path, arc_root, zip_password
                            )
                        pack_files.append((pack_name, pack_path, entity_id, title))
                        pack_dirs.append(target_dir)
                    except Exception as e:
                        self.logger.exception(f"打包失败: {target_dir}")
                        summary.append(f"  ⚠️ 打包失败: {e}")

            # 1. 发送本子的打包文件（下载后必须打包并发送，仅支持文件消息的平台）
            if pack_files:
                if not _platform_supports_file(task.platform_name):
                    await send_text(
                        "🚫 当前消息平台不支持发送打包文件，请使用 Telegram、OneBot、"
                        "QQ 官方机器人（websocket）消息平台。"
                    )
                else:
                    for pack_name, pack_path, album_id, title in pack_files:
                        file_chain = MessageChain()
                        file_chain.chain.append(File(name=pack_name, file=pack_path))
                        self._bump_platform_timeouts()
                        sent = False
                        self._cache_sending.add(os.path.abspath(pack_path))
                        try:
                            await self.context.send_message(umo, file_chain)
                            sent = True
                        except Exception as e:
                            # 大文件上传超时（Timed out）时，文件往往已送达，
                            # 此时不发送“发送失败”的误导提示，仅记录日志。
                            if _looks_like_timeout(e):
                                self.logger.warning(
                                    f"打包文件发送超时（可能已送达）: {pack_name}, {e}"
                                )
                                sent = True
                            else:
                                self.logger.exception("发送打包文件失败")
                                await send_text(
                                    f"📦 打包文件已生成，但发送失败: {e}{pwd_hint}"
                                )
                        finally:
                            self._cache_sending.discard(os.path.abspath(pack_path))
                        if sent:
                            # 发送成功后登记/更新 CSV 缓存索引
                            self._cache_upsert(
                                album_id, title, pack_format, pack_path
                            )

            # 2. 引用回复发送者：下载完成
            reply_text = str(self._cfg("finish_reply", "你的本子下载完成，已发送给你"))
            reply_chain = MessageChain()
            if task.reply_message_id:
                mid: Any = task.reply_message_id
                # Telegram 等平台要求回复的消息 ID 为 int
                if str(mid).isdigit():
                    mid = int(mid)
                reply_chain.chain.append(Reply(id=mid))
            if pack_files and zip_password:
                hint_label = "打开密码" if pack_format == "pdf" else "解压密码"
                # 完成文案与密码合并为同一条文本，保证出现在同一条引用回复中
                reply_chain.message(f"✅ {reply_text}\n🔑 {hint_label}: {zip_password}")
            else:
                reply_chain.message(f"✅ {reply_text}")
            if failed_any:
                reply_chain.message(
                    "⚠️ 部分内容下载失败，可重试下载（已缓存图片会跳过）。"
                )
            if not pack_files:
                reply_chain.message("📁 文件已保存到本地。")
            try:
                await self.context.send_message(umo, reply_chain)
            except Exception as e:
                self.logger.exception("发送下载完成消息失败")
                await send_text(
                    f"✅ {reply_text}{pwd_hint}\n（消息发送失败: {e}）"
                )

            # 发送后删除模式：打包文件与原图目录发送后立即删除，不做 CSV 缓存
            if self._pack_mode() == "delete_after_send":
                root = str(self._download_root())
                for (_, pack_path, _, _), target_dir in zip(pack_files, pack_dirs):
                    try:
                        if pack_path and os.path.isfile(pack_path):
                            os.remove(pack_path)
                        if target_dir and os.path.isdir(target_dir):
                            shutil.rmtree(target_dir)
                    except OSError as e:
                        self.logger.warning(f"发送后删除失败: {target_dir}: {e}")
                    self._prune_empty_dirs(target_dir, root)

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

    # 统一指令入口：/jm <参数>。
    # 注意：只注册一个根指令 /jm，不再注册 command_group，
    # 否则 AstrBot 会把“指令组 jm”与“裸指令 jm”识别为重名指令。
    # 首词分发：help/info/i/查看/search/s/搜/status/cancel 走对应子功能，
    # 其余内容一律当作车号直接下载。
    @filter.command("jm")
    async def jm(self, event: AstrMessageEvent, args: GreedyStr) -> None:
        """JMComic 指令入口：/jm <车号> 直接下载，/jm help 查看帮助。"""
        text = args.strip()
        parts = text.split(maxsplit=1) if text else []
        cmd = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("", "help"):
            async for r in self.help_cmd(event):
                yield r
        elif cmd in ("info", "i", "查看"):
            async for r in self.info(event, rest):
                yield r
        elif cmd in ("search", "s", "搜"):
            async for r in self.search(event, rest):
                yield r
        elif cmd == "status":
            async for r in self.status(event):
                yield r
        elif cmd == "cancel":
            async for r in self.cancel(event, rest):
                yield r
        else:
            async for r in self.download(event, text):
                yield r

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
            "下载完成后机器人会主动发送打包文件 zip/pdf（仅支持 Telegram、"
            "OneBot、QQ 官方机器人（websocket）平台）。"
        )
        yield event.plain_result(help_text)

    async def download(self, event: AstrMessageEvent, ids: GreedyStr) -> None:
        """下载本子/章节（后台执行，完成后发送文件）"""
        if not self._check_permission(event):
            yield event.plain_result("⛔ 你没有权限使用下载功能（仅管理员可用，"
                                     "或到插件配置把 permission 改为 everyone）。")
            return
        if not _platform_supports_file(_event_platform_name(event)):
            yield event.plain_result(
                "🚫 当前消息平台不支持发送打包文件，请使用 Telegram、OneBot、"
                "QQ 官方机器人（websocket）消息平台。"
            )
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

        # ---- 缓存命中：整本请求先查 CSV，有打包文件就直接发，不创建下载任务 ----
        self._ensure_cache_cleanup()
        pack_format = str(self._cfg("pack_format", "zip") or "zip").lower()
        if pack_format not in PACK_FORMATS:
            pack_format = "zip"
        if pack_format == "pdf" and fitz is None:
            pack_format = "zip"  # 与 _download_job 的回退逻辑保持一致
        hit_packs: list[tuple[str, str, dict]] = []  # (album_id, file_path, row)
        miss_albums: list[str] = []
        for aid in albums:
            row = self._cache_find(aid, pack_format)
            path = row.get("file_path", "") if row else ""
            if row and path and os.path.isfile(path):
                last = self._cache_parse_time(row.get("last_sent_at") or "")
                ttl = self._cache_ttl_seconds()
                expired = (
                    ttl > 0
                    and last is not None
                    and time.time() - last >= ttl
                )
                if expired:
                    # 已超过 TTL 无人请求：删除文件并走全新下载
                    self._delete_cached_files(aid, path)
                    self._cache_remove(aid, pack_format)
                    miss_albums.append(aid)
                else:
                    hit_packs.append((aid, path, row))
            else:
                if row:
                    # 记录在但文件丢失，清掉脏行后重新下载
                    self._cache_remove(aid, pack_format)
                miss_albums.append(aid)

        if hit_packs:
            reply_mid: Any = str(
                getattr(getattr(event, "message_obj", None), "message_id", "") or ""
            )
            for aid, path, row in hit_packs:
                sent = False
                self._cache_sending.add(os.path.abspath(path))
                file_chain = MessageChain()
                file_chain.chain.append(File(name=os.path.basename(path), file=path))
                try:
                    await self.context.send_message(session_key, file_chain)
                    sent = True
                except Exception as e:
                    if _looks_like_timeout(e):
                        self.logger.warning(
                            f"缓存文件发送超时（可能已送达）: {path}, {e}"
                        )
                        sent = True
                    else:
                        self.logger.exception("发送缓存文件失败")
                        await self.context.send_message(
                            session_key,
                            MessageChain().message(f"📦 缓存文件发送失败: {e}"),
                        )
                finally:
                    self._cache_sending.discard(os.path.abspath(path))
                if not sent:
                    continue
                # 发送成功后更新 CSV 里的最近发送时间与累计次数
                self._cache_upsert(aid, row.get("title", ""), pack_format, path)
                new_row = self._cache_find(aid, pack_format) or row
                template = str(
                    self._cfg(
                        "cache_hit_reply",
                        "✅ 命中缓存，直接发送: JM{id}（累计发送 {count} 次）",
                    )
                )
                text = (
                    template.replace("{id}", aid)
                    .replace("{count}", new_row.get("send_count", ""))
                    .replace("{first}", new_row.get("first_download_at", ""))
                    .replace("{last}", new_row.get("last_sent_at", ""))
                )
                reply_chain = MessageChain()
                if reply_mid:
                    mid: Any = reply_mid
                    if str(mid).isdigit():
                        mid = int(mid)
                    reply_chain.chain.append(Reply(id=mid))
                reply_chain.message(text)
                try:
                    await self.context.send_message(session_key, reply_chain)
                except Exception as e:
                    self.logger.exception("发送缓存命中提示失败: {e}")

        if not miss_albums and not photos:
            event.stop_event()  # 全部命中，不再创建下载任务
            return
        albums = miss_albums

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
            platform_name=_event_platform_name(event),
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

    async def info(self, event: AstrMessageEvent, album_id: str) -> None:
        """查看本子详情（不下载）"""
        if not album_id.strip():
            yield event.plain_result("❌ 请输入车号，示例: /jm info 123")
            return
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

    async def cancel(self, event: AstrMessageEvent, task_id: str) -> None:
        """取消下载任务"""
        if not task_id.strip():
            yield event.plain_result("❌ 请提供任务 ID，示例: /jm cancel <任务id>")
            return
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
        if self._cache_cleanup_task:
            self._cache_cleanup_task.cancel()
            self._cache_cleanup_task = None
