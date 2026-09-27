# chaoxing_core（内置核心）

本目录是超星学习通 API 的核心实现，**改造自 GPL-3.0 开源项目
[Samueli924/chaoxing](https://github.com/Samueli924/chaoxing)**，
保留原文件结构与主要逻辑，方便后续跟进上游更新。

## 与上游的差异（有意为之，改动越小越安全）

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `logger.py` | 日志文件路径/级别改由 `CX_LOG_FILE` / `CX_LOG_LEVEL` 环境变量控制，并新增 `reconfigure()`；目录不可写时自动退化为仅控制台输出 | 服务端需要把日志写到账号目录，且不能在只读环境中崩溃 |
| `cxsecret_font.py` | `resource_path()` 优先用项目目录定位 `resource/font_map_table.json`，其次才回退当前工作目录 | 服务端会把工作目录切到账号目录，字体映射表仍需能找到 |
| `__init__.py` | 补充说明性文档字符串 | 明确来源与改动范围 |

> 除此之外（登录、课程/章节解析、视频进度上报、作业答题、题库对接、直播任务等）
> 均与上游保持一致，因此跟进上游更新时只需要重新核对上面三处改动。

## 模块职责

- `base.py`：`Chaoxing` 客户端（登录、课程、章节、任务点、视频/文档/阅读/作业）
- `answer.py`：题库 provider（AI/SiliconFlow/TikuGo/TikuYanxi/TikuLike/TikuAdapter）与答案缓存
- `decode.py`：课程/章节/任务点/题目页面解析
- `cipher.py`：登录参数 AES 加密
- `cxsecret_font.py`、`font_decoder.py`：超星字体加密题目解码
- `captcha.py`：验证码自动识别（需额外安装 `ddddocr`，可选）
- `live.py`、`live_process.py`：直播回放任务
- `notification.py`：上游自带的通知实现（服务端默认使用 `server/notify.py`）

## 授权

由于继承自 GPL-3.0 项目，本目录及其衍生作品均按 **GPL-3.0** 授权，
详见项目根目录 `LICENSE`。
