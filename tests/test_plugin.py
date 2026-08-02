"""冒烟测试：用桩模块模拟 AstrBot API，验证 astrbot_plugin_jmcomic 核心逻辑。"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# 1. 注入 astrbot 桩模块
# ---------------------------------------------------------------------------


def _module(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class FakeFilter:
    def __init__(self) -> None:
        self.registered = []

    def regex(self, pattern, **kwargs):
        def deco(fn):
            self.registered.append(("regex", pattern, None, fn))
            return fn

        return deco

    def command(self, name, alias=None, **kwargs):
        def deco(fn):
            self.registered.append(("command", name, alias, fn))
            return fn

        return deco

    def command_group(self, name, **kwargs):
        def deco(fn):
            self.registered.append(("command_group", name, None, fn))
            return FakeCommandGroup(name, self)

        return deco


filter_mod = FakeFilter()


class FakeCommandGroup:
    def __init__(self, name: str, filter_mod: FakeFilter) -> None:
        self.name = name
        self.filter_mod = filter_mod

    def command(self, name, alias=None, **kwargs):
        def deco(fn):
            self.filter_mod.registered.append(("command", name, alias, fn))
            return fn

        return deco

    def group(self, name, **kwargs):
        return FakeCommandGroup(f"{self.name}.{name}", self.filter_mod)


class FakeEvent:
    def __init__(
        self,
        umo="session://test/1",
        admin=True,
        message_str="jm 123",
    ) -> None:
        self.unified_msg_origin = umo
        self._admin = admin
        self.message_str = message_str
        self.is_at_or_wake_command = True
        self.message_obj = SimpleNamespace(message_id="msg123")
        self._stopped = False

    def stop_event(self) -> None:
        self._stopped = True

    def get_message_str(self) -> str:
        return self.message_str

    def is_stopped(self) -> bool:
        return self._stopped

    def is_admin(self) -> bool:
        return self._admin

    def plain_result(self, text: str):
        return ("plain", text)


class FakeMessageChain:
    def __init__(self, chain=None) -> None:
        self.chain = chain if chain is not None else []

    def message(self, text: str):
        self.chain.append(("plain", text))
        return self


class FakeContext:
    def __init__(self) -> None:
        self.sent = []  # (session, chain)

    async def send_message(self, session, message_chain) -> bool:
        self.sent.append((session, message_chain))
        return True


class TimeoutFileContext(FakeContext):
    """文件消息总是抛超时（模拟 Telegram 大文件上传超时但实际已送达）。"""

    async def send_message(self, session, message_chain) -> bool:
        if any(hasattr(c, "name") for c in message_chain.chain):
            raise TimeoutError("Timed out")
        return await super().send_message(session, message_chain)


class FakeStar:
    def __init__(self, context, config=None) -> None:
        self.context = context
        self.logger = logging.getLogger("jmtest_plugin")


_module("astrbot")
_module("astrbot.api")
_module(
    "astrbot.api.event",
    AstrMessageEvent=FakeEvent,
    MessageChain=FakeMessageChain,
    filter=filter_mod,
)
class Plain:
    def __init__(self, text: str) -> None:
        self.text = text


class File:
    def __init__(self, name: str = "", file: str = "", url: str = "") -> None:
        self.name = name
        self.file = file
        self.url = url


class Reply:
    def __init__(self, id: str = "") -> None:
        self.id = id


_module(
    "astrbot.api.message_components",
    Plain=Plain,
    File=File,
    Reply=Reply,
)
_module("astrbot.api.star", Context=FakeContext, Star=FakeStar)
_module("astrbot.core")
_module("astrbot.core.star")
_module("astrbot.core.star.filter")
_module("astrbot.core.star.filter.command", GreedyStr=str)
_module("astrbot.core.utils")
_module(
    "astrbot.core.utils.astrbot_path",
    get_astrbot_plugin_data_path=lambda: "D:/fake/data/plugin_data",
)


class FakeAESZipFile:
    """用标准库真实写 zip 的 pyzipper.AESZipFile 桩，并记录密码。"""

    def __init__(self, mod, path, mode="w", compression=zipfile.ZIP_DEFLATED, encryption=99):
        self._mod = mod
        self._zf = zipfile.ZipFile(path, mode, compression)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._zf.close()
        return False

    def setpassword(self, password: bytes) -> None:
        self._mod.last_password = password

    def write(self, filename, arcname=None) -> None:
        self._zf.write(filename, arcname)

    def close(self) -> None:
        self._zf.close()


class FakePyzipper:
    """pyzipper 桩模块：加密分支调用时记录密码，实际写入普通 zip。"""

    ZIP_DEFLATED = zipfile.ZIP_DEFLATED
    WZ_AES = 99

    def __init__(self) -> None:
        self.last_password: bytes | None = None

    def AESZipFile(
        self,
        path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        encryption=99,
    ):
        return FakeAESZipFile(self, path, mode, compression, encryption)


fake_pyzipper = FakePyzipper()
sys.modules["pyzipper"] = fake_pyzipper

# ---------------------------------------------------------------------------
# 2. 加载被测插件
# ---------------------------------------------------------------------------

PLUGIN_MAIN = str(Path(__file__).resolve().parent.parent / "main.py")
spec = importlib.util.spec_from_file_location("jmcomic_downloader", PLUGIN_MAIN)
main = importlib.util.module_from_spec(spec)
sys.modules["jmcomic_downloader"] = main
assert spec.loader is not None
spec.loader.exec_module(main)


# ---------------------------------------------------------------------------
# 3. 假 jmcomic
# ---------------------------------------------------------------------------


class FakeJmText:
    @staticmethod
    def parse_to_jm_id(text) -> str:
        """模拟真实 JmcomicText.parse_to_jm_id 的解析规则。"""
        text = str(text)
        if text.isdigit():
            return text
        if len(text) >= 2 and text[0].lower() == "j" and text[1].lower() == "m":
            return text[2:]
        for pat in (r"(?:photos?|albums?)/(\d+)", r"id=(\d+)"):
            m = re.search(pat, text)
            if m:
                return m.group(1)
        raise ValueError(f"无法解析: {text}")


class FakeOption:
    def __init__(self, d: dict, album_dir: str, photo_dir: str) -> None:
        self.d = d
        self.dir_rule = SimpleNamespace(
            decide_album_root_dir=lambda album: album_dir,
            decide_image_save_dir=lambda album, photo: photo_dir,
        )

    def new_jm_client(self):
        return FakeClient()


class FakeAlbum:
    def __init__(self) -> None:
        self.album_id = "123"
        self.name = "测试本子"
        self.authors = ["作者A", "作者B"]
        self.author = "作者A"
        self.pub_date = "2024-01-01"
        self.update_date = "2024-02-02"
        self.page_count = 30
        self.views = "2M"
        self.likes = "77K"
        self.comment_count = 9801
        self.tags = ["tag1", "tag2"]
        self.actors = ["角色A"]
        self.works = ["作品X"]
        self.episode_list = [("123", "1", "第1話 上", "2024-01-01")]

    def __len__(self) -> int:
        return len(self.episode_list)


class FakeClient:
    def __init__(self) -> None:
        self.album = FakeAlbum()

    def get_album_detail(self, aid):
        assert str(aid) == "123"
        return self.album

    def search_site(self, query, page=1):
        return SimpleNamespace(
            total=3,
            iter_id_title_tag=lambda: [
                ("111", "结果一", ["a", "b"]),
                ("222", "结果二", []),
            ],
        )


class FakeJm:
    JmcomicText = FakeJmText

    def __init__(self, album_dir: str, photo_dir: str) -> None:
        self._album_dir = album_dir
        self._photo_dir = photo_dir
        self.downloaded = []

    def JmOption_construct(self, d: dict) -> FakeOption:
        self.last_option_dict = d
        return FakeOption(d, self._album_dir, self._photo_dir)

    @property
    def JmOption(self):
        return type(
            "JmOption",
            (),
            {"construct": staticmethod(self.JmOption_construct)},
        )

    def download_album(self, aid, option, check_exception=True):
        self.downloaded.append(("album", str(aid)))
        logging.getLogger("jmcomic").info(
            f"本子获取成功: [{aid}], 作者: [作者A], 章节数: [1], "
            f"总页数: [10], 标题: [测试本子], 关键词: []",
            extra={"topic": "album.before"},
        )
        logging.getLogger("jmcomic").info(
            f"章节下载完成: [{aid}] ({aid}[1/1])",
            extra={"topic": "photo.after"},
        )
        logging.getLogger("jmcomic").info(
            f"本子下载完成: [{aid}]",
            extra={"topic": "album.after"},
        )
        detail = SimpleNamespace(
            id=str(aid),
            name="测试本子",
            authors=["作者A"],
            author="作者A",
            album_id=str(aid),
            tags=[],
            works=[],
            actors=[],
            pub_date="",
            update_date="",
            page_count=10,
            views="",
            likes="",
            comment_count=0,
            episode_list=[],
            __len__=lambda self: 1,
        )
        return SimpleNamespace(detail=detail, downloader=FakeDler())

    def download_photo(self, pid, option, check_exception=True):
        self.downloaded.append(("photo", str(pid)))
        logging.getLogger("jmcomic").info(
            f"章节下载完成: [{pid}]",
            extra={"topic": "photo.after"},
        )
        photo = SimpleNamespace(
            id=str(pid),
            name="章节一",
            from_album=SimpleNamespace(),
        )
        return SimpleNamespace(detail=photo, downloader=FakeDler())


class FakeDler:
    def __init__(self) -> None:
        self.download_failed_image = []
        self.download_failed_photo = []
        self.download_success_dict = {"album": {"photo": ["img1.jpg"]}}


# ---------------------------------------------------------------------------
# 4. 测试
# ---------------------------------------------------------------------------

passed = 0
failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


async def main_test() -> None:
    print("== 指令注册 ==")
    names = [r[1] for r in filter_mod.registered]
    check("注册了 command_group jm", ("command_group", "jm", None) in [(r[0], r[1], r[2]) for r in filter_mod.registered])
    for cmd in ("info", "search", "status", "cancel", "help"):
        check(f"注册了子指令 {cmd}", any(r[0] == "command" and r[1] == cmd for r in filter_mod.registered))
    check(
        "注册了默认下载入口（regex）",
        any(
            r[0] == "regex"
            and r[1].startswith(r"(?i)^jm")
            for r in filter_mod.registered
        ),
        str([r for r in filter_mod.registered if r[0] == "regex"]),
    )

    print("== 配置与路径 ==")
    ctx = FakeContext()
    cfg = {
        "permission": "admin",
        "download_dir": "",
        "zip_dir": "",
        "client_impl": "api",
        "image_decode": True,
        "image_suffix": ".png",
        "image_threads": 5,
        "photo_threads": 0,
        "retry_times": 3,
        "proxy": "",
        "zip_after_download": True,
        "send_file": True,
        "delete_zip_after_send": False,
        "send_progress": True,
        "max_concurrent": 2,
        "search_max": 5,
    }
    plugin = main.JmcomicPlugin(ctx, cfg)
    check("下载根目录为默认 plugin_data 路径", str(plugin._download_root()) == r"D:\fake\data\plugin_data\jmcomic_downloader\download")

    print("== 车号解析 ==")
    albums, photos = plugin._parse_ids(FakeJm("", ""), "123 456 p789 JM999 https://18comic.vip/album/888/")
    check("album 解析", albums == ["123", "456", "999", "888"], str(albums))
    check("photo 解析", photos == ["789"], str(photos))
    try:
        plugin._parse_ids(FakeJm("", ""), "abc")
        check("无效车号抛异常", False)
    except ValueError:
        check("无效车号抛异常", True)

    print("== option 构造 ==")
    tmp = Path(tempfile.mkdtemp(prefix="jmtest_"))
    album_dir = tmp / "album"
    photo_dir = tmp / "photo"
    album_dir.mkdir()
    photo_dir.mkdir()
    (album_dir / "01.jpg").write_bytes(b"fake")
    fake_jm = FakeJm(str(album_dir), str(photo_dir))
    option = plugin._build_option(fake_jm)
    d = fake_jm.last_option_dict
    check("client.impl=api", d["client"]["impl"] == "api")
    check("image.suffix=.png", d["download"]["image"]["suffix"] == ".png")
    check("threading.image=5", d["download"]["threading"]["image"] == 5)
    check("proxies=system", d["client"]["postman"]["meta_data"]["proxies"] == "system")
    check(
        "photo_threads=0 时不传 photo 键（避免 None 覆盖默认值）",
        "photo" not in d["download"]["threading"],
        str(d["download"]["threading"]),
    )
    cfg_threaded = dict(cfg, photo_threads=3)
    plugin_t = main.JmcomicPlugin(ctx, cfg_threaded)
    plugin_t._build_option(fake_jm)
    dt = fake_jm.last_option_dict["download"]["threading"]
    check("photo_threads=3 时 photo=3", dt.get("photo") == 3, str(dt))

    print("== 权限 ==")
    ev_admin = FakeEvent(admin=True)
    ev_normal = FakeEvent(admin=False)
    check("admin 可用", plugin._check_permission(ev_admin))
    check("普通用户被拒", not plugin._check_permission(ev_normal))
    cfg2 = dict(cfg, permission="everyone")
    plugin2 = main.JmcomicPlugin(ctx, cfg2)
    check("everyone 放行", plugin2._check_permission(ev_normal))

    print("== 详情格式化 ==")
    text = plugin._format_album(FakeClient().album)
    check("详情包含标题", "测试本子" in text)
    check("详情包含章节", "第1話 上" in text)

    print("== info / search 指令 ==")
    ev = FakeEvent()
    fake_jm2 = FakeJm(str(tmp / "info_album"), str(tmp / "info_photo"))
    plugin._jm = fake_jm2
    results = [r async for r in plugin.info(ev, "JM123")]
    check("info 返回详情", any("测试本子" in r[1] for r in results))
    results = [r async for r in plugin.search(ev, "全彩 人妻")]
    check("search 返回结果", any("结果一" in r[1] for r in results))

    print("== 后台下载任务（进度/zip/文件消息） ==")
    dl_album = tmp / "download" / "album"
    dl_photo = tmp / "download" / "photo"
    dl_album.mkdir(parents=True)
    dl_photo.mkdir(parents=True)
    (dl_album / "01.jpg").write_bytes(b"fake")
    fake_jm_dl = FakeJm(str(dl_album), str(dl_photo))
    plugin_dl = main.JmcomicPlugin(
        ctx,
        {
            **cfg,
            "download_dir": str(tmp / "download"),
            "zip_dir": str(tmp / "zip"),
        },
    )
    plugin_dl._jm = fake_jm_dl
    task = main.DownloadTask(
        task_id="t1",
        session_key="session://test/1",
        albums=["123"],
        photos=[],
        created_at=0.0,
        reply_message_id="12345",
    )
    await plugin_dl._download_job(task, "session://test/1", ["123"], [])
    check("任务状态 done", task.status == "done", task.status)
    check("fake_jm 收到 album 下载", fake_jm_dl.downloaded == [("album", "123")], str(fake_jm_dl.downloaded))
    sent = plugin_dl.context.sent
    all_texts = " ".join(
        c[1] if isinstance(c, tuple) else "" for _, ch in sent for c in ch.chain
    )
    check("提示了本子获取成功", "本子获取成功" in all_texts and "开始下载" in all_texts, all_texts[:200])
    check("提示了本子获取成功附带ID", "JM123" in all_texts, all_texts[:200])
    check("不出现章节下载完成进度", "章节下载完成" not in all_texts, all_texts[:200])
    check("不出现下载统计摘要", "个章节" not in all_texts, all_texts[:300])
    file_idx = next(
        (i for i, (_, ch) in enumerate(sent) if any(hasattr(c, "name") for c in ch.chain)),
        -1,
    )
    reply_idx = next(
        (
            i
            for i, (_, ch) in enumerate(sent)
            if any(isinstance(c, tuple) and "你的本子下载完成" in c[1] for c in ch.chain)
        ),
        -1,
    )
    check("压缩包先于回复文案发送", 0 <= file_idx < reply_idx, f"file={file_idx}, reply={reply_idx}")
    check("回复了发送者文案", "你的本子下载完成，已发送给你" in all_texts, all_texts[:300])
    file_chain_text = " ".join(
        c[1] if isinstance(c, tuple) else "" for c in sent[file_idx][1].chain
    )
    check("压缩包消息无文字前缀", file_chain_text.strip() == "", repr(file_chain_text))
    fetched_msg = next(
        (
            " ".join(c[1] for c in ch.chain if isinstance(c, tuple))
            for _, ch in sent
            if any(isinstance(c, tuple) and "本子获取成功" in c[1] for c in ch.chain)
        ),
        "",
    )
    check("获取成功提示不显示本子名称", "《" not in fetched_msg and "测试本子" not in fetched_msg, fetched_msg[:200])
    reply_has_quote = any(
        any(isinstance(c, Reply) and c.id == 12345 for c in ch.chain)
        for _, ch in sent
    )
    check("回复引用了原指令", reply_has_quote)
    reply_id_is_int = any(
        any(isinstance(c, Reply) and isinstance(c.id, int) for c in ch.chain)
        for _, ch in sent
    )
    check("回复消息ID为int（兼容Telegram）", reply_id_is_int)
    zip_root = Path(plugin_dl._zip_root())
    zips = list(zip_root.glob("*.zip"))
    check("生成了 zip", len(zips) == 1, str(zips))
    check(
        "zip 文件名只含ID+时间戳不含标题",
        zips and zips[0].name.startswith("JM123_") and zips[0].name.endswith(".zip"),
        str(zips),
    )
    check("zip 非空", zips and zips[0].stat().st_size > 0)
    check("发送后已删除原始下载目录", not dl_album.exists(), str(dl_album))
    check("压缩包仍然保留", zips and zips[0].exists(), str(zips))

    print("== 默认下载指令 /jm 123（成功启动不回复任何消息） ==")
    ctx_h = FakeContext()
    h_album = tmp / "h_dl" / "album"
    h_photo = tmp / "h_dl" / "photo"
    h_album.mkdir(parents=True)
    h_photo.mkdir(parents=True)
    (h_album / "01.jpg").write_bytes(b"fake")
    fake_jm_h = FakeJm(str(h_album), str(h_photo))
    plugin_h = main.JmcomicPlugin(
        ctx_h,
        {
            **cfg,
            "download_dir": str(tmp / "h_dl"),
            "zip_dir": str(tmp / "h_zip"),
        },
    )
    plugin_h._jm = fake_jm_h
    ev_h = FakeEvent()
    results = [r async for r in plugin_h.download(ev_h)]
    check("成功启动不 yield 任何消息", results == [], str(results))
    check("事件已标记停止（阻止LLM回复）", ev_h.is_stopped())
    h_task = next(iter(plugin_h._tasks.values()))
    await asyncio.wait_for(asyncio.shield(h_task.asyncio_task), timeout=10)
    h_texts = " ".join(
        c[1] if isinstance(c, tuple) else "" for _, ch in ctx_h.sent for c in ch.chain
    )
    check("不出现“已开始后台下载”", "已开始后台下载" not in h_texts, h_texts[:200])
    check("后台仍正常完成", "你的本子下载完成" in h_texts, h_texts[:300])

    print("== 重复指令去重 ==")
    ctx_dd = FakeContext()
    dd_album = tmp / "dd_dl" / "album"
    dd_photo = tmp / "dd_dl" / "photo"
    dd_album.mkdir(parents=True)
    dd_photo.mkdir(parents=True)
    (dd_album / "01.jpg").write_bytes(b"fake")
    fake_jm_dd = FakeJm(str(dd_album), str(dd_photo))
    plugin_dd = main.JmcomicPlugin(
        ctx_dd,
        {
            **cfg,
            "download_dir": str(tmp / "dd_dl"),
            "zip_dir": str(tmp / "dd_zip"),
        },
    )
    plugin_dd._jm = fake_jm_dd
    _ = [r async for r in plugin_dd.download(FakeEvent())]
    check("第一个指令启动任务", len(plugin_dd._tasks) == 1, str(len(plugin_dd._tasks)))
    ev_dup = FakeEvent()
    _ = [r async for r in plugin_dd.download(ev_dup)]
    check("相同指令被去重", len(plugin_dd._tasks) == 1, str(len(plugin_dd._tasks)))
    check("重复指令事件已停止", ev_dup.is_stopped())
    dd_task = next(iter(plugin_dd._tasks.values()))
    await asyncio.wait_for(asyncio.shield(dd_task.asyncio_task), timeout=10)
    _ = [r async for r in plugin_dd.download(FakeEvent())]
    check("任务完成后可再次下载", len(plugin_dd._tasks) == 2, str(len(plugin_dd._tasks)))

    print("== 文件发送超时容错 ==")
    check("超时异常识别", main._looks_like_timeout(TimeoutError("Timed out")))
    check("非超时异常不误判", not main._looks_like_timeout(RuntimeError("连接被拒绝")))
    ctx_to = TimeoutFileContext()
    to_album = tmp / "to_dl" / "album"
    to_photo = tmp / "to_dl" / "photo"
    to_album.mkdir(parents=True)
    to_photo.mkdir(parents=True)
    (to_album / "01.jpg").write_bytes(b"fake")
    fake_jm_to = FakeJm(str(to_album), str(to_photo))
    plugin_to = main.JmcomicPlugin(
        ctx_to,
        {
            **cfg,
            "download_dir": str(tmp / "to_dl"),
            "zip_dir": str(tmp / "to_zip"),
        },
    )
    plugin_to._jm = fake_jm_to
    to_task = main.DownloadTask(
        task_id="t3",
        session_key="session://test/1",
        albums=["123"],
        photos=[],
        created_at=0.0,
        reply_message_id="12345",
    )
    await plugin_to._download_job(to_task, "session://test/1", ["123"], [])
    to_texts = " ".join(
        c[1] if isinstance(c, tuple) else "" for _, ch in ctx_to.sent for c in ch.chain
    )
    check("超时不提示发送失败", "发送失败" not in to_texts, to_texts[:300])
    check("超时后仍回复完成", "你的本子下载完成" in to_texts, to_texts[:300])

    print("== 加密 zip 打包（pyzipper） ==")
    enc_album = tmp / "enc_dl" / "album"
    enc_photo = tmp / "enc_dl" / "photo"
    enc_album.mkdir(parents=True)
    enc_photo.mkdir(parents=True)
    (enc_album / "01.jpg").write_bytes(b"fake")
    fake_jm_enc = FakeJm(str(enc_album), str(enc_photo))
    ctx_enc = FakeContext()
    plugin_enc = main.JmcomicPlugin(
        ctx_enc,
        {
            **cfg,
            "download_dir": str(tmp / "enc_dl"),
            "zip_dir": str(tmp / "enc_zip"),
            "zip_password": "secret123",
        },
    )
    plugin_enc._jm = fake_jm_enc
    enc_task = main.DownloadTask(
        task_id="tenc",
        session_key="session://test/enc",
        albums=["123"],
        photos=[],
        created_at=0.0,
        reply_message_id="12345",
    )
    await plugin_enc._download_job(enc_task, "session://test/enc", ["123"], [])
    check(
        "加密 zip 走 pyzipper 并设置密码",
        fake_pyzipper.last_password == b"secret123",
        repr(fake_pyzipper.last_password),
    )
    enc_zips = list(Path(plugin_enc._zip_root()).glob("*.zip"))
    check("加密任务生成了 zip", len(enc_zips) == 1, str(enc_zips))
    check("加密任务发送后原图已删除", not enc_album.exists(), str(enc_album))

    print("== 同一车号重复打包不互相覆盖 ==")
    conc_album = tmp / "conc_dl" / "album"
    conc_photo = tmp / "conc_dl" / "photo"
    conc_album.mkdir(parents=True)
    conc_photo.mkdir(parents=True)
    (conc_album / "01.jpg").write_bytes(b"fake")
    ctx_c1 = FakeContext()
    plugin_c1 = main.JmcomicPlugin(
        ctx_c1,
        {
            **cfg,
            "download_dir": str(tmp / "conc_dl"),
            "zip_dir": str(tmp / "conc_zip"),
        },
    )
    plugin_c1._jm = FakeJm(str(conc_album), str(conc_photo))
    c1_task = main.DownloadTask(
        task_id="c1",
        session_key="session://test/c1",
        albums=["123"],
        photos=[],
        created_at=0.0,
        reply_message_id="12345",
    )
    await plugin_c1._download_job(c1_task, "session://test/c1", ["123"], [])
    # 第一次任务发送后已删除原图，重建目录模拟第二次下载
    conc_album.mkdir(parents=True)
    (conc_album / "01.jpg").write_bytes(b"fake")
    ctx_c2 = FakeContext()
    plugin_c2 = main.JmcomicPlugin(
        ctx_c2,
        {
            **cfg,
            "download_dir": str(tmp / "conc_dl"),
            "zip_dir": str(tmp / "conc_zip"),
        },
    )
    plugin_c2._jm = FakeJm(str(conc_album), str(conc_photo))
    c2_task = main.DownloadTask(
        task_id="c2",
        session_key="session://test/c2",
        albums=["123"],
        photos=[],
        created_at=0.0,
        reply_message_id="12345",
    )
    await plugin_c2._download_job(c2_task, "session://test/c2", ["123"], [])
    conc_zips = list(Path(plugin_c1._zip_root()).glob("JM123_*.zip"))
    check(
        "同一车号两次打包生成两个独立 zip",
        len(conc_zips) == 2,
        str([z.name for z in conc_zips]),
    )
    check(
        "两个 zip 文件名互不相同（无覆盖）",
        len({z.name for z in conc_zips}) == 2,
        str([z.name for z in conc_zips]),
    )
    for z in conc_zips:
        with zipfile.ZipFile(z) as zf:
            check(
                "每个 zip 内容都只有自己的 01.jpg",
                zf.namelist() == ["01.jpg"],
                str(zf.namelist()),
            )

    print("== 取消/状态 ==")
    plugin_dl._tasks["t1"] = task
    ev2 = FakeEvent()
    results = [r async for r in plugin_dl.status(ev2)]
    check("status 显示任务", any("t1" in r[1] for r in results))
    results = [r async for r in plugin_dl.cancel(FakeEvent(), "t1")]
    check("已完成任务不可取消", any("无需取消" in r[1] for r in results))

    print("== 下载出错 → 工单与反馈指令 ==")

    class FakeJmFails(FakeJm):
        def download_album(self, aid, option, check_exception=True):
            self.downloaded.append(("album", str(aid)))
            logging.getLogger("jmcomic").info(
                f"本子获取成功: [{aid}], 作者: [作者A], 章节数: [1], "
                f"总页数: [10], 标题: [测试本子], 关键词: []",
                extra={"topic": "album.before"},
            )
            raise RuntimeError("模拟下载失败")

    ctx_err = FakeContext()
    plugin_err = main.JmcomicPlugin(
        ctx_err,
        {**cfg, "download_dir": str(tmp / "err_dl"), "zip_dir": str(tmp / "err_zip")},
    )
    fake_fail = FakeJmFails(str(tmp / "fail_album"), str(tmp / "fail_photo"))
    plugin_err._jm = fake_fail
    err_task = main.DownloadTask(
        task_id="t2",
        session_key="session://test/1",
        albums=["123"],
        photos=[],
        created_at=0.0,
    )
    await plugin_err._download_job(err_task, "session://test/1", ["123"], [])
    check("任务状态 failed", err_task.status == "failed", err_task.status)
    check("记录到了本子获取成功", err_task.album_fetched is True)
    check("生成了工单", bool(plugin_err._tickets), str(plugin_err._tickets))
    err_texts = " ".join(
        c[1] if isinstance(c, tuple) else "" for _, ch in ctx_err.sent for c in ch.chain
    )
    check("报错提示联系管理员", "联系管理员" in err_texts, err_texts[:300])
    check("附带反馈指令", "/jm report" in err_texts, err_texts[:300])

    # 反馈指令：先禁用真实发信，验证工单校验与邮件内容组装
    plugin_err._send_email = lambda subject, body: None  # type: ignore
    ticket = next(iter(plugin_err._tickets.values()))
    results = [r async for r in plugin_err.report(FakeEvent(), ticket["ticket_id"])]
    check("反馈指令返回成功", any("已发送" in r[1] for r in results), str(results))
    results = [r async for r in plugin_err.report(FakeEvent(), "T99999")]
    check("无效工单被拒绝", any("无效的反馈指令" in r[1] for r in results), str(results))

    print("== 邮件内容组装 ==")
    body = plugin_err._build_report_body(
        ticket, "[01-01 12:00] 用户: 你好，帮我下载"
    )
    check("邮件包含错误信息", "模拟下载失败" in body, body[:200])
    check("邮件包含错误堆栈", "Traceback" in body)
    check("邮件包含工单号", ticket["ticket_id"] in body)
    check("邮件包含对话记录", "你好，帮我下载" in body)
    extracted = plugin_err._extract_history_text(
        {
            "type": "user",
            "message": [
                {"type": "plain", "text": "你好"},
                {"type": "at", "name": "小明"},
                {"type": "image"},
            ],
        }
    )
    check("对话记录文本提取", extracted == "你好 @小明 [image]", repr(extracted))

    print(f"\n结果: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main_test())
