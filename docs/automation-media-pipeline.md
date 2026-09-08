# Tsumugu 自动化媒体下载改造设计

## 1. 目标

将 tsumugu 从“依赖网页手动操作的 YouTube 下载器”改造成一个可被 Hermes/Telegram 稳定调用的 NAS 媒体处理服务，同时保留现有 Web 界面作为人工操作入口。

最终使用方式：

```text
Telegram 消息 / CLI / Web UI
          ↓
      统一任务接口
          ↓
    媒体处理核心
          ↓
  yt-dlp → ffmpeg → 拆分 → NAS
```

自动化调用方不应模拟网页点击，也不应复制 yt-dlp、ffmpeg、NAS 路径和拆分逻辑。

## 2. 当前系统基线

- 项目：FastAPI + Jinja2 + HTMX + SQLite
- 下载：yt-dlp；直链使用 wget
- 转码：ffmpeg
- 任务：内存中的 DownloadManager，数据库保存历史
- NAS 根目录：配置表中的 `nas_root`
- 当前实例实际配置：`/tmp/nas_mnt/NAS`
- tsumugu 应用根：NAS 的 `Media` 子目录
- 默认音乐相对目录：`/music`（实际路径为 NAS `Media/music`）
- 非秘密运行配置：项目根目录 `config.toml`
- NAS 密码：只保存在本机数据库/凭据存储，不进入 Git
- 运行入口：`run.py`，默认端口通过 `PORT` 环境变量控制，当前默认 8005
- 当前未提交用户改动：`run.py` 的端口改动，必须保留

## 3. 设计原则

### 3.1 单一业务核心

下载、转码、任务状态、文件定位、拆分和路径安全只能有一套实现。

Web、CLI、Hermes 技能都是适配层，不在适配层实现媒体业务逻辑。

### 3.2 自动化优先

Telegram 调用不依赖预览卡片、浏览器 localStorage、WebSocket 或页面状态。

提交任务后必须获得稳定的 `job_id`，之后可以通过机器可读接口查询状态和结果。

### 3.3 任务隔离

每个任务必须拥有明确的输入文件和工作目录，不能通过“目标目录中任意一个音频文件”猜测输入文件。并发任务不得互相拆分或覆盖。

### 3.4 NAS 路径集中管理

外部调用方只传应用根下的相对路径，例如 `/music`。真实路径必须统一经过
`resolve_within_nas()`，禁止自动化接口绕过路径边界检查。应用根固定为 NAS 的
`Media` 子目录，不能浏览或下载到共享根下的其他目录。

### 3.5 可恢复、可观察

任务状态、阶段、错误和生成文件必须可查询。进程重启后不能把任务伪装成仍在运行；如果不能恢复，就必须明确标记为 interrupted/failed。

### 3.6 配置边界

地址、共享名、账号名、挂载点、媒体默认目录、默认格式和拆分策略统一写在
`config.toml`。代码、CLI、Web 默认值和 Hermes 技能不得各自复制这些值。
密码不是普通配置，继续放在本机数据库/凭据管理中，禁止写入这个文件或提交到仓库。

### 3.7 兼容现有 Web

现有页面继续可用，但页面请求改为调用统一核心。网页可以保留“手动选择拆分”的交互，Telegram 使用独立的自动化默认策略。

## 4. 目标模块结构

首期不强行移动全部文件，采用渐进式拆分：

```text
app/
├── media/
│   ├── __init__.py
│   ├── models.py          # 请求、任务、阶段、结果模型
│   ├── naming.py          # 文件名清理与确定性输出名
│   ├── downloader.py      # yt-dlp / wget 适配
│   ├── transcoder.py      # ffmpeg 音频格式转换
│   ├── splitter.py        # chapter / silence / fallback 策略
│   ├── jobs.py            # 任务队列、状态、事件、取消
│   └── service.py         # 面向调用方的统一服务
├── routers/youtube.py     # HTTP 适配层
└── download_manager.py    # 兼容层，逐步委托 app.media.jobs

bin/ 或 app/cli/
└── tsumugu_media.py       # JSON CLI：submit/status/cancel

tests/
├── test_media_paths.py
├── test_media_naming.py
├── test_media_splitter.py
└── test_media_cli.py
```

如果渐进式阶段中保留旧模块名更安全，可以先让 `download_manager.py` 和 `audio_splitter.py` 成为兼容 facade，再删除重复实现。

## 5. 统一任务模型

任务至少包含：

```text
job_id
source_url
source_type: youtube | playlist | channel | direct
requested_format: mp3 | m4a | flac
nas_relative_dir
resolved_output_dir
split_policy
keep_original
status
stage
progress
current_file
error
files[]
created_at
started_at
completed_at
```

状态：

```text
queued
preparing
downloading
converting
splitting
completed
failed
cancelled
interrupted
```

阶段和最终状态必须分开，避免 Telegram 只能看到笼统的 `downloading`。

## 6. 自动化默认策略

Hermes/Telegram 默认使用：

```text
format: mp3
save_path: /music
keep_original: false
split_policy: auto
```

`auto` 策略：

1. 单曲且时长较短：不拆分。
2. 明显为合集或长音频：先尝试章节拆分。
3. 没有有效章节时，再尝试静音检测。
4. 拆分成功：删除原始长文件（除非明确要求保留）。
5. 拆分失败：保留原文件，任务标记为 failed 或 completed_with_warning，不能静默删除。

长音频阈值和标题关键词必须集中在配置/策略模块，不能散落在 Telegram 技能或前端 JS 中。

首期若 `completed_with_warning` 会扩大改动范围，可以先使用 `completed` + `warnings[]`，确保文件安全优先。

## 7. 输出与文件隔离

建议流程：

```text
NAS/.tsumugu/jobs/<job_id>/source/
NAS/.tsumugu/jobs/<job_id>/work/
NAS/Media/music/<最终文件>
```

- 下载输入先进入任务专属临时目录。
- 转码和拆分使用明确输入路径。
- 生成文件全部写入任务工作目录。
- 成功后原子移动到目标 NAS 目录。
- 任务完成后清理临时目录。
- 失败时保留必要日志和源文件路径，避免留下无法解释的半成品。

若 NAS 不适合保存临时文件，则临时目录使用本机临时目录，但最终移动前必须再次经过 NAS 路径解析和写入检查。

## 8. 对外接口

### 8.1 HTTP

保留现有接口兼容页面，并增加稳定任务接口：

```text
POST /api/media/jobs
GET  /api/media/jobs/{job_id}
POST /api/media/jobs/{job_id}/cancel
GET  /api/media/jobs/{job_id}/files
```

提交响应必须包含：

```json
{
  "job_id": "...",
  "status": "queued"
}
```

查询响应必须包含结构化状态、进度、阶段、错误和生成文件列表。

### 8.2 CLI

Hermes 优先调用 JSON CLI，避免依赖网页和脆弱的自然语言日志：

```text
python -m app.cli.media submit URL --format mp3 --path /music --split auto --json
python -m app.cli.media status JOB_ID --json
python -m app.cli.media cancel JOB_ID --json
```

CLI 的 stdout 只输出 JSON；人类日志写 stderr 或日志文件。

如果长任务需要常驻服务，CLI 提交任务后立即返回 job_id，状态通过 SQLite/HTTP 查询，不让 Hermes 长时间持有一个阻塞 shell。

## 9. Hermes 技能

新增技能 `youtube-to-nas-music`，技能只负责：

1. 从 Telegram 消息提取 URL 和用户选项。
2. 使用自动化默认策略。
3. 调用 tsumugu JSON CLI 或 HTTP 任务接口。
4. 定期查询 job 状态。
5. 将完成文件、拆分数量、警告和失败原因汇总回 Telegram。

技能不得：

- 自己实现 yt-dlp 命令；
- 自己拼接 NAS 真实路径；
- 自己判断或猜测输出文件；
- 直接删除原始文件；
- 依赖网页 DOM。

## 10. 必须修复的问题

1. `audio_splitter.py` 章节输出文件名使用字符串的 `:02d`，会导致章节拆分异常。
2. `_find_downloaded_file()` 通过目录中任意音频文件兜底，存在并发误匹配。
3. 拆分输出目前固定为 MP3，必须明确与任务目标格式的关系。
4. DownloadManager 的任务主要存在内存，服务重启后只能回收为失败；至少需要可靠的持久化任务状态和中断标记。
5. 播放列表/频道批量入口和单视频入口的参数默认值要统一。
6. Web API、CLI 和自动化接口必须共享路径安全校验。
7. 输出文件名需要确定性、去重和路径安全处理。

## 11. 验证要求

每个阶段都要执行：

```text
git diff --check
python -m compileall app
pytest -q                 # 如果测试依赖可用
mypy app --ignore-missing-imports  # 如果项目已有 mypy 环境
```

必须增加不依赖 YouTube 网络的测试：

- NAS 路径边界和 `..`/symlink 防逃逸；
- 文件名清理；
- 任务状态序列化；
- 章节命名；
- 静音拆分区间过滤；
- CLI JSON 输出；
- 并发任务输入文件隔离。

真实管线验证分层：

1. 本地合成音频 + ffprobe/ffmpeg，验证拆分。
2. 本地 HTTP 文件，验证直链下载和路径限制。
3. 真实 YouTube URL 只在依赖和网络可用时执行，且使用 NAS 目标目录下的测试目录。
4. 验证完成后清理测试产物，不删除用户已有音乐。

## 12. 提交策略

按阶段提交并立即推送：

1. `docs: document automated media pipeline design`
2. `refactor(media): extract reliable download pipeline`
3. `feat(media): add JSON job interface`
4. `feat(skill): add YouTube to NAS music workflow`
5. `test(media): verify local pipeline and CLI`

每次提交前检查工作树，只提交本阶段文件；保留用户已有的 `run.py` 修改，不覆盖、不混入无关改动。

## 13. 完成标准

只有同时满足以下条件才算完成：

- Web 下载功能仍然可用；
- CLI 可以提交、查询和取消任务；
- Hermes 技能可以调用 CLI/HTTP；
- 默认输出进入配置应用根下的 `/music`（NAS `Media/music`）；
- 长音频可以章节优先、静音兜底拆分；
- 章节拆分、并发输入隔离和路径安全测试通过；
- 至少一次真实下载或等价的完整本地管线验证通过；
- 每个阶段已有提交并已推送；
- 最终重新读取 `git status`、远端分支和测试结果后再报告。
