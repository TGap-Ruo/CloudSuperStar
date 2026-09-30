# chaoxing_core（内置核心）

本目录是超星学习通 API 的核心实现，**改造自 GPL-3.0 开源项目
[Samueli924/chaoxing](https://github.com/Samueli924/chaoxing)**，
保留原文件结构与主要逻辑，方便后续跟进上游更新。

## 与上游的差异（有意为之，改动越小越安全）

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `logger.py` | 日志文件路径/级别改由 `CX_LOG_FILE` / `CX_LOG_LEVEL` 环境变量控制，并新增 `reconfigure()`；目录不可写时自动退化为仅控制台输出 | 服务端需要把日志写到账号目录，且不能在只读环境中崩溃 |
| `cxsecret_font.py` | `resource_path()` 改为按「PyInstaller 解包目录 → 包目录 → 项目根目录 → 当前工作目录」查找，并同时兼容 `resource/` 与 `resources/` 两种目录名 | 字体映射表实际位于 `chaoxing_core/resources/`，而上游按 `resource/`（项目根）查找；定位失败会导致字体加密题解不出来、题目显示为怪字 |
| `__init__.py` | 补充说明性文档字符串 | 明确来源与改动范围 |
| `base.py` | 章节检测判分改用「对错标记 → 本次成绩 → 可见答案比较」三级判定，并新增 `WorkNotAnswerable`（页面已批阅/不可作答时不再判失败）；答案比较做归一化 | 修复「成绩 100 分却被判 N/N 题答错」导致的无意义重做与章节误判失败 |
| `decode.py` | `decode_questions_info()` 在页面没有 `<form>`（已批阅页面）时返回空题目列表而不抛异常 | 配合上面的判分修复 |
| `answer.py` | 新增 `set_usage_hook()` / `_report_usage()`，在 `AI` 与 `SiliconFlow` 每次调用大模型后回调一次（answer 与连接检查都记） | 服务端需要统计"刷一个账号花了多少 token / 多少钱"，见 `server/usage.py` |

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
