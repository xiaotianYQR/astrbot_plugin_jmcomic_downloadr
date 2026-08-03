# JMComic 下载器

**AstrBot 插件**，基于 [jmcomic (JMComic-Crawler-Python)](https://github.com/hect0x7/JMComic-Crawler-Python) 构建，提供禁漫本子的**下载、搜索、详情查询**能力。指令发出后后台自动下载并打包，支持 **ZIP / PDF 双格式**发送，重复请求同一本子时**直接命中缓存秒发**。

![版本](https://img.shields.io/badge/版本-v1.12.0-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.13.0-brightgreen)
![许可](https://img.shields.io/badge/License-AGPL--3.0-orange)
![Stars](https://img.shields.io/github/stars/xiaotianYQR/astrbot_jmcomic_downloader)

> ⚠️ **免责声明**：内容涉及成人向漫画，仅供学习交流，请遵守当地法律法规。同时注意不要对禁漫服务器造成过大压力（一次不要下载太多本子）。

> **版权与许可 / Copyright and License**
>
> **作者 / Author**: xiaotianYQR - xiaotianyqr123@qq.com/xiaotianyqr123@gmail.com
>
> **许可协议 / License**: 本项目采用 [**AGPL-3.0**](LICENSE) 开源协议，基于 [AstrBot](https://github.com/AstrBotDevs/AstrBot)（同为 AGPL-3.0）插件框架开发，并调用 [jmcomic (JMComic-Crawler-Python)](https://github.com/hect0x7/JMComic-Crawler-Python) 作为核心下载库。基于本仓库的修改或衍生作品必须以相同协议分发。
>
> **License**: Licensed under [**AGPL-3.0**](LICENSE). Built on the [AstrBot](https://github.com/AstrBotDevs/AstrBot) plugin framework (also AGPL-3.0) and powered by [jmcomic (JMComic-Crawler-Python)](https://github.com/hect0x7/JMComic-Crawler-Python). Any modifications or derivative works must be distributed under the same license.

> [!WARNING]
> 本插件涉及成人向内容：请确认您所在地区与所在平台允许后再使用；下载内容请自行妥善保管，勿用于商业传播，遵守内容来源网站的使用规则。

<details>
<summary><strong>免责声明与用户协议</strong></summary>

使用本项目即表示您已阅读、理解并同意以下条款：

1. 本项目仅供学习、研究和合法用途使用，严禁用于任何违反当地法律法规、平台服务条款或侵犯他人权益的场景。
2. 使用者应遵守内容来源网站的规则，不得对其服务器造成过大压力（如一次批量下载过多本子），由此导致的访问受限、封禁等后果由使用者自行承担。
3. 本项目按"原样"提供，开发者不对数据丢失、打包文件损坏、账号或设备异常、平台风控或任何直接、间接损失承担责任。
4. 因用户违规使用（如传播成人内容、用于商业用途）导致的法律纠纷，由用户自行承担全部责任。
5. 本协议可能随项目更新而调整，继续使用即表示接受更新后的条款。

</details>

## ✨ 功能特性

- 🚀 **后台下载**：指令发出后立即返回，下载与打包在后台执行，全程主动推送进度
- 📦 **ZIP / PDF 双格式**：`pack_format` 二选一，PDF 把全部图片合成一册，随时切换
- ⚡ **缓存秒发**：CSV 索引记录打包文件，重复请求同一本子直接发送，跳过下载
- 🔐 **AES-256 加密**：ZIP 解压密码 / PDF 打开密码，加密文件在 QQ 等平台不易触发发送限制
- 🧠 **车号智能解析**：`123`、`JM123`、完整 URL 均可，一次可下载多个本子与章节
- 🔍 **详情与搜索**：`/jm info` 查看本子详情（标题/作者/标签/章节），`/jm search` 站内搜索
- 📋 **任务管理**：`/jm status` 查看任务，`/jm cancel` 取消下载
- 💾 **磁盘缓存**：重复下载自动跳过已存在文件，取消任务不丢已下载内容
- 🛡️ **平台适配**：仅支持 Telegram / OneBot / QQ 官方机器人，其他平台无法使用
- ⚙️ **高度可配置**：并发数、代理、图片解码、缓存策略、权限等均可调整

## 📖 目录

- [功能特性](#-功能特性)
- [安装](#-安装)
- [快速开始](#-快速开始)
- [配置](#-配置)
- [下载目录结构](#-下载目录结构)
- [使用流程](#-使用流程)
- [常见问题](#-常见问题)
- [更新日志](#-更新日志)
- [贡献者](#-贡献者)
- [协议](#-协议)
- [鸣谢](#-鸣谢)

## 📦 安装

### 方式一：从链接安装（推荐）

打开 AstrBot WebUI → 插件管理 → 安装插件 → **从链接安装**，填入：

```
https://github.com/xiaotianYQR/astrbot_jmcomic_downloader
```

### 方式二：手动安装

1. 将本插件目录放到 AstrBot 的 `data/plugins/` 下（目录名为 `jmcomic_downloader`）
2. 重启 AstrBot，或到 WebUI 插件管理页加载插件
3. 依赖 `jmcomic`、`pyzipper`、`pymupdf`（PDF 打包）、`pillow`（webp 桥接）会在加载时自动安装，也可手动执行：

```bash
pip install jmcomic pyzipper pymupdf pillow
```

> 💡 安装后在 WebUI 插件管理页点击插件「配置」，按需修改下载目录、代理、权限等。

> 📱 **平台支持**：本插件仅支持 **Telegram**、**OneBot（aiocqhttp）**、**QQ 官方机器人（websocket）**，其他平台无法使用。

## 🚀 快速开始

| 指令 | 说明 |
| --- | --- |
| `/jm 123` | 下载本子 123（后台执行，完成后发送 ZIP/PDF） |
| `/jm 123 456` | 一次下载多个本子 |
| `/jm 123 p456` | 同时下载本子 123 和章节 456 |
| `/jm info 123` | 查看本子详情，不下载（别名：`i`、`查看`） |
| `/jm search 全彩 人妻` | 站内搜索（别名：`s`、`搜`） |
| `/jm status` | 查看本会话的下载任务 |
| `/jm cancel <任务id>` | 取消下载任务 |
| `/jm help` | 查看帮助 |

**车号支持任意文本**：`123`、`JM123`、`https://18comic.vip/album/123/` 均可；章节号以 `p` 开头（如 `p456`）。

## ⚙️ 配置

所有配置项在 WebUI 插件管理页 → 插件配置中修改：

### 权限与搜索

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `permission` | string | `admin` | 命令权限：`admin`=仅管理员，`everyone`=所有人 |
| `search_max` | int | `5` | 搜索结果显示的最大条数 |

### 下载设置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `download_dir` | string | 空 | 下载保存目录，留空使用插件数据目录 |
| `client_impl` | string | `api` | 客户端实现：`api`=APP 端（不限 IP），`html`=网页端（效率高） |
| `image_decode` | bool | `true` | 还原禁漫的混淆图片 |
| `image_suffix` | string | 空 | 图片格式转换（如 `.jpg`、`.png`，留空不转换） |
| `image_threads` | int | `30` | 同时下载图片的线程数 |
| `photo_threads` | int | `0` | 同时下载章节数，`0`=按 CPU 核数自动 |
| `retry_times` | int | `5` | 请求失败重试次数 |
| `proxy` | string | 空 | 代理地址（如 `127.0.0.1:7890`），留空使用系统代理 |

### 打包与发送

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `zip_dir` | string | 空 | ZIP 输出目录，留空使用插件数据目录 |
| `pdf_dir` | string | 空 | PDF 输出目录（独立于 zip），留空使用插件数据目录 |
| `pack_format` | string | `zip` | 发送格式：`zip`/`pdf` 二选一；`pdf`=所有图片合成一册（未安装 pymupdf 自动回退 zip） |
| `pack_mode` | string | `csv_cache` | 打包文件处理模式：`csv_cache`=保留并用 CSV 索引缓存（重复请求直接发送，超时自动删除）；`delete_after_send`=发送后立即删除本地打包文件与原图目录 |
| `zip_password` | string | 空 | 打包文件密码：zip 解压密码 / PDF 打开密码（AES-256），留空不加密 |
| `send_progress` | bool | `true` | 下载过程中发送进度消息 |
| `max_concurrent` | int | `2` | 每个会话同时运行的最大下载任务数 |
| `finish_reply` | string | `你的本子下载完成，已发送给你` | 下载完成后引用回复的文案 |

### 缓存策略

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `cache_ttl_hours` | int | `24` | 缓存过期时间（小时），距最近一次发送超过该时长即删除 |
| `cache_cleanup_interval_minutes` | int | `30` | 后台缓存清理检查间隔（分钟），最小 1 分钟 |
| `cache_delete_raw` | bool | `true` | 缓存过期时是否同时删除原图目录 |
| `cache_csv_path` | string | 空 | 缓存索引 CSV 路径，留空使用默认 `cache_index.csv` |
| `cache_hit_reply` | string | `✅ 命中缓存…` | 缓存命中回复文案，支持 `{id}` `{count}` `{first}` `{last}` 占位符 |

> `pack_mode` 二选一：`csv_cache` 模式下打包文件保留在本地并被 `cache_index.csv` 索引，重复请求直接发送已有文件，超过 `cache_ttl_hours` 无人请求自动删除；`delete_after_send` 模式下发送完成后立即删除打包文件与原图目录，不做任何缓存记录。

## 🗂️ 下载目录结构

默认保存在 AstrBot 的 `data/plugin_data/jmcomic_downloader/` 下：

```text
plugin_data/jmcomic_downloader/
├── download/
│   └── 12345-我是本子/          # 车号-本子名称
│       ├── 1/                   # 章节序号
│       │   └── 00001.jpg
│       └── 2/
│           └── 00001.jpg
├── zip/
│   └── JM12345.zip              # zip 打包文件名只保留车号
├── pdf/
│   └── JM12345.pdf              # pdf 单独存放，命名规则与 zip 一致
└── cache_index.csv              # 缓存索引表：记录车号、文件路径、首次下载/最近发送时间、发送次数
```

> 每个本子独立一个文件夹；打包时只包含该本子。ZIP 内部以 `车号-本子名称/` 作为顶层目录，解压后即是一个完整的本子文件夹；PDF 则把所有图片按章节/页序合成为一册，打开密码与 ZIP 解压密码同源（`zip_password`）。

## 📚 使用流程

1. 发送 `/jm 335492`（不回复任何消息）
2. 获取到本子后，机器人提示 `✅ 本子获取成功: JM335492 📥 开始下载…`
3. 下载完成后，机器人提示 `✅ 本子下载完成: JM335492`，然后**直接发送本子的打包文件（zip/pdf）**（无多余文字）
4. 最后**引用回复**发送者：`✅ 你的本子下载完成，已发送给你`
5. 如果下载出错，机器人会提示 `❌ 下载错误，请重试或联系管理员`

> 如果 CSV 缓存里已有该本子的打包文件（且未过期），机器人会**跳过下载**，直接发送文件并引用回复 `✅ 命中缓存，直接发送: JMxxx（累计发送 N 次）`；超过 `cache_ttl_hours`（默认 24 小时）无人请求后，该本子的打包文件与原图目录会被自动删除。

## 🔧 常见问题

**Q：提示「jmcomic 未安装」？**

执行 `pip install jmcomic` 后重启 AstrBot；插件加载时也会尝试自动安装依赖。

**Q：提示「⛔ 你没有权限」？**

默认 `permission: admin` 仅管理员可用。可将配置改为 `everyone`，或在平台适配器配置中设置管理员（如 aiocqhttp 的 `admin_id`）。

**Q：收不到打包文件？**

本插件仅支持 Telegram、OneBot（aiocqhttp）、QQ 官方机器人（websocket）平台，其他平台无法使用，请改用上述平台。

**Q：文件需要密码？**

配置 `zip_password` 后，ZIP 使用 AES-256 加密、PDF 设置打开密码，发送时会在引用回复中附带密码；不需要加密留空即可。

**Q：PDF 生成失败或提示未安装 pymupdf？**

执行 `pip install pymupdf pillow` 后重启 AstrBot。未安装 pymupdf 时插件会自动回退发送 ZIP；`pillow` 用于解码 webp 等图片，缺了它包含 webp 的本子无法生成 PDF。

**Q：下载很慢或经常失败？**

可尝试将 `client_impl` 改为 `html`、调大 `image_threads` / `photo_threads`、配置 `proxy`；同时注意不要一次下载太多本子。

**Q：取消任务会删除已下载内容吗？**

不会。任务取消后已下载图片保留在磁盘缓存中，重试时会自动跳过已存在文件。

**Q：大文件发送超时？**

插件会自动调大 Telegram 等平台的文件上传超时；若仍失败，请检查网络后重试。

> 💡 如果遇到其他问题，欢迎前往 [Issues](https://github.com/xiaotianYQR/astrbot_jmcomic_downloader/issues) 反馈。

## 📝 更新日志

### v1.12.0（2026-08-03）

- ✨ **新增 CSV 打包缓存索引**：`cache_index.csv` 记录车号、文件路径、首次下载/最近发送时间与累计发送次数
- ⚡ **缓存秒发**：重复请求同一本子直接发送已有打包文件，跳过下载；多车号混查时命中部分直接发、未命中部分照常下载
- 🗑️ **自动清理**：超过 `cache_ttl_hours`（默认 24h）无人请求自动删除打包文件，可配置是否连带删除原图目录
- 🔁 **启动补录**：自动扫描 `zip/`、`pdf/` 目录已有文件写入索引，旧文件立即可复用
- ⚙️ **新增 `pack_mode`**：`csv_cache`（默认）/ `delete_after_send` 二选一，取代原 `delete_zip_after_send` 开关
- 🐛 **修复**：插件启动即挂起后台清理任务；文件删除失败（如被占用）保留记录并在下次清理时重试

<details>
<summary>📜 查看往期更新日志</summary>

### v1.11.0（2026-08-03）

- 🛡️ **新增平台检测**：仅 Telegram、OneBot（aiocqhttp）、QQ 官方机器人（websocket）支持发送打包文件，其他平台下载前即提示
- 🗑️ **移除 `send_file`、`zip_after_download` 配置**：下载完成后强制打包（zip/pdf）并发送，不再提供关闭开关
- 📝 所有下发消息不再返回本地保存路径

### v1.10.0（2026-08-03）

- ✨ **新增 PDF 打包**：`pack_format` 支持 `zip`/`pdf` 二选一，PDF 把所有图片合成为一册
- 📄 PDF 基于 PyMuPDF 实现（jpg/png 无损嵌入），webp 等格式自动经 Pillow 桥接解码
- 🔐 PDF 支持 AES-256 打开密码，文件名与 ZIP 同规则（`JM<车号>.pdf`）
- 📁 PDF 单独存放于 `pdf/` 目录，新增独立配置 `pdf_dir`（与 `zip_dir` 分开）
- 🔁 未安装 pymupdf 时自动回退发送 ZIP 并提示

### v1.9.0（2026-08-03）

- 📝 重构并美化 README：新增功能特性、配置项表格、常见问题、贡献者与致谢
- 📝 更新插件元数据与版本徽章

### v1.6.0（2026-08-03）

- ✨ 新增 ZIP 压缩包 AES-256 加密，支持配置解压密码（`zip_password`），加密包在 QQ 等平台不易触发文件发送限制
- ✨ 新增 pyzipper 加密依赖
- 🔄 下载指令改为 `/jm` 直接调用并移除邮件反馈
- 📁 本子目录改为「车号-标题」，ZIP 内含顶层文件夹，解压即完整本子目录
- 🐛 修复 ZIP 打包范围与并发消息重复问题
- 📝 优化回复信息与插件描述

### v1.0.0（2026-08-02）

- 🎉 首个正式版本：基于 jmcomic 的本子下载、搜索、详情查询
- 📦 后台下载并打包 ZIP 发送
- 📝 采用 AGPL-3.0 协议并补充 README

</details>

## 👥 贡献者

感谢每一位为本项目贡献代码、提交 Issue 和提供反馈的开发者：

[![贡献者](https://contrib.rocks/image?repo=xiaotianYQR/astrbot_jmcomic_downloader)](https://github.com/xiaotianYQR/astrbot_jmcomic_downloader/graphs/contributors)

## 📄 协议

本项目采用 **AGPL-3.0** 协议开源，详见 [LICENSE](LICENSE)。

## 🙏 鸣谢

本项目基于或参考了以下开源项目：

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) - 机器人框架
- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) - JMComic 库
- [pyzipper](https://github.com/danifus/pyzipper) - 加密 ZIP 库
- [PyMuPDF](https://github.com/pymupdf/PyMuPDF) - PDF 打包库

⭐ 如果这个插件对你有帮助，欢迎给个 Star！
