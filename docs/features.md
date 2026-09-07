# 功能介绍

TG-SignPulse 是 Telegram 多账号自动化管理面板，把签到、消息交互、关键词监听和 AI 辅助验证收敛到同一套 Web 控制台。

## 核心能力

### 账号管理

- 验证码登录 / 扫码登录
- 代理与会话模式（file / string）
- 设备管理、设备保活、官方消息查看
- 批量状态检查

详见 [账号管理](/guide/accounts)。

### 任务编排（sign-tasks）

- 固定 Cron 或时间段执行
- 多账号共享同一套动作流程
- 发送文本、骰子、点按钮、AI 识图/计算、关键词监听等动作
- 批量启用/停用/触发
- 失败分类与历史日志

> 面板与 API 请使用 **`/api/sign-tasks`**。旧版 ORM `/api/tasks` 已移除。

详见 [任务编排](/guide/tasks)。

### AI 验证

- 图片 OCR / 选按钮
- 计算题回复
- 计算后点击
- OpenAI 兼容接口（含本地 LLM）

详见 [AI 动作](/guide/ai)。

### 关键词监听

- exact / contains / regex
- 命中后推送、转发、继续动作
- 多实例可配置监听分片

详见 [关键词监听](/guide/keyword-monitor)。

### 通知与设置

- 签到间隔、任务超时/冷却/流程重试、AI 视觉参数可在面板「系统设置」覆盖
- Bot 测试发送、任务成功通知（全局 + 任务级开关）、静默时段
- 监听推送支持 Server酱（任务表单通道）

### 漫画采集子模块

- 在同一套 SignPulse 控制台配置并启停 Telegram 漫画监听
- 采集频道可按组选择「讨论群相册」或「Telegraph 页面」；讨论群留空时默认跟正文里的 telegra.ph 链接拉整话
- 讨论群相册：频道与讨论群一一对应；讨论群可留空，启动时解析频道绑定的讨论组
- 采集号直接使用 `/accounts` 里的 SignPulse 账号（登录、代理、会话统一维护）
- 同一讨论帖后续相册会追加到已有章节，避免 10 张一组发完后只留下前 30 页
- 支持讨论区回复过滤和发送者白名单
- 自动过滤普通用户评论、压缩文件、音频、下载推广等非漫画内容；源频道/讨论区的视频可按频道开关 + 关键词/时长过滤后转发到运营频道（漫画预览在前、视频在后），不经过本机下载，也不会进漫画主站
- 解析 `[作者] 标题 #标签`，标题去除方括号内容，第一张上传图片自动作为封面
- 图片上传到 ImgBed 后删除本地临时文件，再通过 `POST /api/manga/publish` 发布到 AnimeStream
- 独立漫画 SQLite 目录，不占用签到任务的数据库

### 运维与可观测

- `/healthz`、`/readyz`（含调度锁、旧 API 只读状态）
- 版本信息与更新检查（Settings / 侧栏；`/api/ops/version`）
- Dashboard SSE 实时日志
- 任务 WebSocket 日志（含运行 phase：等锁 / 冷却 / 执行中）
- 任务运行状态：`GET /api/sign-tasks/runs/active`、`/run/status`、`/run/cancel`（`state` + `phase` + `failure_category`）
- Dashboard 活跃运行摘要与失败分类聚合（可点筛选日志 / 会话失效跳转账号）
- 任务列表：冷却倒计时、多账号 phase、停止运行、账号失效提示
- 配置/数据备份导出；可选定时自动备份（本地 / WebDAV）
- WebDAV：测试连接、列出远端、流式下载、自动备份失败 Bot 通知
- 配置导入预览（dry-run）
- 任务克隆与内置模板（文本/按钮/时段/骰子/关键词监听；预填动作后须选会话）
- 系统设置未保存提示与「保存全部」
- 旧任务 ORM 残留盘点：`tools/check_legacy_tasks.py`

详见 [WebDAV 备份与恢复](/guide/backup-webdav)、[运维手册](/reference/ops)。

## 适合场景

- 多账号机器人签到、日常打卡
- 验证码、诗句填空、数学题、按钮验证
- 群组/频道长期监听与通知
- VPS / Docker 长期托管

## 不适合 / 注意

- 多副本**共享同一 Telegram session 文件**（易损坏会话）
- 把面板直接裸奔在公网且无 HTTPS / 访问控制
- 依赖已移除的旧 `/api/tasks` 接口

## 下一步

- [快速开始](/guide/quick-start)
- [WebDAV 备份与恢复](/guide/backup-webdav)
- [Docker 部署](/deploy/docker)
- [配置参考](/reference/configuration)
