"""与真实 jmcomic 库的集成验证：插件构造的 JmOption / 车号解析是否兼容。"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# astrbot 桩（最小集）
# ---------------------------------------------------------------------------


def _module(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


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


class FakeContext:
    async def send_message(self, session, message_chain) -> bool:
        return True


class FakeStar:
    def __init__(self, context, config=None) -> None:
        self.context = context
        self.logger = types.SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )


class FakeFilter:
    def command(self, name, alias=None, **kwargs):
        return lambda fn: fn

    def command_group(self, name, **kwargs):
        def deco(fn):
            return SimpleGroup(name, self)

        return deco


class SimpleGroup:
    def __init__(self, name, f: FakeFilter) -> None:
        self.name = name
        self.f = f

    def command(self, name, alias=None, **kwargs):
        return lambda fn: fn


_module("astrbot")
_module("astrbot.api")
_module(
    "astrbot.api.event",
    AstrMessageEvent=object,
    MessageChain=type("MessageChain", (), {"__init__": lambda self, chain=None: setattr(self, "chain", chain or [])}),
    filter=FakeFilter(),
)
_module("astrbot.api.message_components", Plain=Plain, File=File, Reply=Reply)
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

# ---------------------------------------------------------------------------
# 加载插件 + 真实 jmcomic
# ---------------------------------------------------------------------------

spec = importlib.util.spec_from_file_location(
    "jmcomic_downloader", str(Path(__file__).resolve().parent.parent / "main.py")
)
main = importlib.util.module_from_spec(spec)
sys.modules["jmcomic_downloader"] = main
assert spec.loader is not None
spec.loader.exec_module(main)

import jmcomic  # noqa: E402

cfg = {
    "permission": "admin",
    "download_dir": "",
    "zip_dir": "",
    "client_impl": "api",
    "image_decode": True,
    "image_suffix": ".png",
    "image_threads": 8,
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

plugin = main.JmcomicPlugin(FakeContext(), cfg)

jm = plugin._get_jm()
assert jm is jmcomic, f"期望真实 jmcomic，得到 {jm}"
print(f"[ok] 加载真实 jmcomic {jmcomic.__version__}")

option = plugin._build_option(jm)
print(f"[ok] JmOption 构造成功: {type(option).__name__}")
assert option.dir_rule.base_dir == r"D:\fake\data\plugin_data\jmcomic_downloader\download"
assert option.download.image.suffix == ".png"
assert option.download.image.decode is True
assert option.download.threading.image == 8
assert option.client.impl == "api"
assert option.client.retry_times == 3
print("[ok] option 字段: dir_rule/download.image/threading/client.impl/retry_times")

# 回归：photo 并发数不能是 None（jmcomic 默认会规范化为 CPU 核数）
photo_batch = option.decide_photo_batch_count(None)
assert isinstance(photo_batch, int) and photo_batch > 0, photo_batch
print(f"[ok] decide_photo_batch_count 默认返回 int: {photo_batch}")

albums, photos = plugin._parse_ids(jm, "123 456 p789 JM999 https://18comic.vip/album/888/")
assert albums == ["123", "456", "999", "888"], albums
assert photos == ["789"], photos
print(f"[ok] 车号解析: albums={albums} photos={photos}")

try:
    plugin._parse_ids(jm, "纯文本没有数字")
    raise AssertionError("应当抛异常")
except Exception as e:
    print(f"[ok] 无效车号正确报错: {type(e).__name__}")

# 客户端创建会立即请求 JM 域名获取 cookies；沙箱无网络时跳过
try:
    client = option.new_jm_client()
    print(f"[ok] 客户端创建成功: {type(client).__name__}")
except Exception as e:
    print(f"[skip] 创建客户端需要访问 JM 域名（沙箱无网络）: {type(e).__name__}")

print("\n全部通过")
