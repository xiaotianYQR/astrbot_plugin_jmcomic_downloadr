# JMComic 下载器

**AstrBot 插件**，基于 [jmcomic (JMComic-Crawler-Python)](https://github.com/hect0x7/JMComic-Crawler-Python) 构建，提供禁漫本子的**下载、搜索、详情查询**能力——后台下载、自动打包，完成后直接发送 ZIP 压缩包。

![版本](https://img.shields.io/badge/版本-v1.6.0-blue)
![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.13.0-brightgreen)
![许可](https://img.shields.io/badge/License-AGPL--3.0-orange)
![Stars](https://img.shields.io/github/stars/xiaotianYQR/astrbot_jmcomic_downloader)

> ⚠️ **免责声明**：内容涉及成人向漫画，仅供学习交流，请遵守当地法律法规。同时注意不要对禁漫服务器造成过大压力（一次不要下载太多本子）。

## ✨ 功能特性

- 🚀 **后台下载**：指令发出后立即返回，下载与打包在后台线程执行，全程主动推送进度
- 📦 **自动打包发送**：下载完成自动生成 ZIP 压缩包并发送，省去手动整理
- 🔐 **AES-256 加密压缩包**：可选解压密码，加密压缩包在 QQ 等平台不易触发文件发送限制
- 🧠 **车号智能解析**：`123`、`JM123`、完整 URL 均可，一次可下载多个本子与章节
- 🔍 **详情与搜索**：`/jm info` 查看本子详情（标题/作者/标签/章节列表），`/jm search` 站内搜索
- 📋 **任务管理**：`/jm status` 查看本会话任务，`/jm cancel` 取消下载
- 💾 **磁盘缓存**：重复下载自动跳过已存在文件，取消任务不会丢失已下载内容
- ⚙️ **高度可配置**：并发数、代理、图片解码、打包/发送行为、权限等均可调整

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
3. 依赖 `jmcomic`、`pyzipper` 会在加载时自动安装，也可手动执行：

```bash
pip install jmcomic pyzipper
```

> 💡 安装后在 WebUI 插件管理页点击插件「配置」，按需修改下载目录、代理、权限等。

## 🚀 快速开始

| 指令 | 说明 |
| --- | --- |
| `/jm 123` | 下载本子 123（后台执行，完成后发送 ZIP） |
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

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `permission` | string | `admin` | 命令权限：`admin`=仅管理员，`everyone`=所有人 |
| `download_dir` | string | 空 | 下载保存目录，留空使用插件数据目录 |
| `zip_dir` | string | 空 | ZIP 输出目录，留空使用插件数据目录 |
| `client_impl` | string | `api` | 客户端实现：`api`=APP 端（不限 IP），`html`=网页端（效率高） |
| `image_decode` | bool | `true` | 还原禁漫的混淆图片 |
| `image_suffix` | string | 空 | 图片格式转换（如 `.jpg`、`.png`，留空不转换） |
| `image_threads` | int | `30` | 同时下载图片的线程数 |
| `photo_threads` | int | `0` | 同时下载章节数，`0`=按 CPU 核数自动 |
| `retry_times` | int | `5` | 请求失败重试次数 |
| `proxy` | string | 空 | 代理地址（如 `127.0.0.1:7890`），留空使用系统代理 |
| `zip_after_download` | bool | `true` | 下载完成后打包为 ZIP |
| `send_file` | bool | `true` | 尝试发送 ZIP 文件，关闭后仅返回保存路径 |
| `delete_zip_after_send` | bool | `false` | 发送后删除本地 ZIP |
| `zip_password` | string | 空 | 压缩包解压密码（AES-256 加密），留空不加密 |
| `send_progress` | bool | `true` | 下载过程中发送进度消息 |
| `max_concurrent` | int | `2` | 每个会话同时运行的最大下载任务数 |
| `search_max` | int | `5` | 搜索结果显示的最大条数 |
| `finish_reply` | string | `你的本子下载完成，已发送给你` | 下载完成后引用回复的文案 |

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
└── zip/
    └── JM12345.zip              # 压缩包文件名只保留车号
```

> 每个本子独立一个文件夹；打包时只包含该本子。ZIP 内部以 `车号-本子名称/` 作为顶层目录，解压后即是一个完整的本子文件夹。

## 📚 使用流程

1. 发送 `/jm 335492`（不回复任何消息）
2. 获取到本子后，机器人提示 `✅ 本子获取成功: JM335492 📥 开始下载…`
3. 下载完成后，机器人提示 `✅ 本子下载完成: JM335492`，然后**直接发送本子的压缩包**（无多余文字）
4. 最后**引用回复**发送者：`✅ 你的本子下载完成，已发送给你`
5. 如果下载出错，机器人会提示 `❌ 下载错误，请重试或联系管理员`

## 🔧 常见问题

**Q：提示「jmcomic 未安装」？**

执行 `pip install jmcomic` 后重启 AstrBot；插件加载时也会尝试自动安装依赖。

**Q：提示「⛔ 你没有权限」？**

默认 `permission: admin` 仅管理员可用。可将配置改为 `everyone`，或在平台适配器配置中设置管理员（如 aiocqhttp 的 `admin_id`）。

**Q：收不到压缩包？**

部分平台不支持文件消息，插件会退回发送文件保存路径；也可将 `send_file` 配置为 `false`，只返回路径。

**Q：解压需要密码？**

配置 `zip_password` 后压缩包使用 AES-256 加密，发送时会在引用回复中附带解压密码；不需要加密留空即可。

**Q：下载很慢或经常失败？**

可尝试将 `client_impl` 改为 `html`、调大 `image_threads` / `photo_threads`、配置 `proxy`；同时注意不要一次下载太多本子。

**Q：取消任务会删除已下载内容吗？**

不会。任务取消后已下载图片保留在磁盘缓存中，重试时会自动跳过已存在文件。

**Q：大文件发送超时？**

插件会自动调大 Telegram 等平台的文件上传超时；若仍失败，请检查网络或改用 `send_file: false` 获取本地路径。

## 📝 更新日志

- **v1.9.0**：新增 ZIP 压缩包 AES-256 加密（`zip_password`）；下载指令改为 `/jm` 直接调用并移除邮件反馈；本子目录改为「车号-标题」，ZIP 内含顶层文件夹，修复打包范围与并发消息重复问题。

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

⭐ 如果这个插件对你有帮助，欢迎给个 Star！
