# astrbot_plugin_jmcomic_downloadr
基于 [jmcomic (JMComic-Crawler-Python)](https://github.com/hect0x7/JMComic-Crawler-Python) 的 AstrBot 插件，提供禁漫本子的下载、搜索、详情查询能力。

> ⚠️ 内容涉及成人向漫画，请遵守当地法律法规，并注意不要对禁漫服务器造成过大压力（一次不要下载太多本子）。

## 安装

1. 将本插件目录放到 AstrBot 的 `data/plugins/` 下（目录名 `jmcomic_downloader`）。
2. 重启 AstrBot 或到 WebUI 插件管理页加载插件。插件依赖 `jmcomic` 会在加载时自动安装（也可手动 `pip install jmcomic`）。
3. 在 WebUI 插件管理页点击插件配置，按需修改下载目录、代理、权限等。

## 使用

| 指令 | 说明 |
| --- | --- |
| `/jm 123` | 下载本子 123（后台执行，完成后发送 zip） |
| `/jm 123 p456` | 同时下载本子 123 和章节 456 |
| `/jm info 123` | 查看本子详情（不下载） |
| `/jm search 全彩 人妻` | 站内搜索 |
| `/jm status` | 查看本会话的下载任务 |
| `/jm cancel <任务id>` | 取消下载任务 |
| `/jm report <工单号>` | 下载报错后，把错误信息和对话记录邮件发送给管理员（仅报错时可用） |
| `/jm help` | 查看帮助 |

车号支持任意文本：`123`、`JM123`、`https://18comic.vip/album/123/` 均可；章节号以 `p` 开头（如 `p456`）。

## 使用流程

1. 发送 `/jm 335492`（不回复任何消息）。
2. 获取到本子后，机器人提示"✅ 本子获取成功: JM335492 📥 开始下载…"（不显示本子名称）。
3. 下载完成后，机器人提示"✅ 本子下载完成: JM335492"，然后**直接发送本子的压缩包**（无多余文字）。
4. 最后**引用回复**发送者"✅ 你的本子下载完成，已发送给你"。
5. 压缩包发送成功后，插件会**自动删除已下载的原始图片文件**，只保留压缩包（zip 是否删除由 `delete_zip_after_send` 控制）。
6. 如果本子获取成功但下载出错，机器人会提示"下载出错，请联系管理员解决"，并附上一个反馈指令 `/jm report <工单号>`。
7. 发送者发送该指令后，插件会通过邮件把**报错代码（堆栈）和最近的对话记录**发送到管理员邮箱。

> 反馈指令只在出现报错且工单有效时可用，且只能由原会话发送者使用。

> zip 压缩包文件名带时间戳与随机后缀（如 `JM123_1780000000_a1b2c3d4.zip`），
> 不同任务/会话下载同一车号时不会互相覆盖；配置 `zip_password` 后会用
> [pyzipper](https://github.com/danifus/pyzipper) 生成 AES 加密压缩包。

## 配置项

- `permission`：`admin`（默认，仅管理员）/ `everyone`（所有人）
- `download_dir` / `zip_dir`：下载目录与压缩包输出目录，默认在 AstrBot 的 `data/plugin_data/jmcomic_downloader/` 下
- `client_impl`：`api`（APP 端，不限 IP）/ `html`（网页端，效率高）
- `image_decode`：是否还原混淆图片（默认开）
- `image_suffix`：图片格式转换（如 `.jpg`、`.png`，留空不转换）
- `image_threads` / `photo_threads`：并发数
- `proxy`：代理，留空使用系统代理
- `zip_after_download` / `send_file` / `delete_zip_after_send`：打包与发送行为
  - 压缩包发送成功后会自动删除原始下载文件（仅保留压缩包）；
  - `delete_zip_after_send` 为 `true` 时，压缩包发送后也会被删除。
- `zip_password`：zip 压缩包加密密码（留空不加密）。设置后需安装 `pyzipper`
  （`pip install pyzipper`），未安装时自动退回普通 zip 并记录警告。
- `send_progress`：是否发送下载进度
- `max_concurrent`：每个会话最多同时运行的任务数
- `search_max`：搜索结果条数
- `finish_reply`：下载完成后引用回复发送者的文案（默认"你的本子下载完成，已发送给你"）

### 报错反馈邮件（SMTP）

要让 `/jm report` 生效，需要在插件配置中填写：

- `smtp_host`：SMTP 服务器地址（如 `smtp.qq.com`）
- `smtp_port`：端口（465=SSL，587=STARTTLS）
- `smtp_use_ssl`：是否使用 SSL
- `smtp_user`：发件邮箱账号
- `smtp_password`：SMTP 授权码（QQ 邮箱需用授权码，不是登录密码）
- `report_email`：接收报错反馈的管理员邮箱
- `report_history_lines`：邮件中附带的对话记录条数（默认 50）

对话记录来自 AstrBot 的群聊消息记录，若为群聊需在 WebUI 平台设置中开启
`group_message_history_enable`；私聊若没有记录，邮件中会注明。

## 说明

- 下载在后台线程执行，任务结束前机器人会主动推送进度与结果；部分平台不支持文件消息，会退回发送文件保存路径。
- 图片有磁盘缓存，重复下载会跳过已存在文件；取消任务不会删除已下载内容。
- 默认只有管理员可用（`permission: admin`）。AstrBot 管理员需在平台适配器配置中设置（如 aiocqhttp 的 `admin_id`）。
