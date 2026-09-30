# 超星学习通 · Web 控制台 + Ubuntu 一键部署

在云服务器上部署一个网页控制台：**浏览器里填学习通账号密码 → 立即开始刷课 / 自动答题 → 右侧实时看日志**。
支持多账号并行、批量粘贴、任务停止 / 删除 / 日志下载；也支持在 `config.yaml` 里配置定时 cron 账号。

> 基于 [lispringing/SuperStar](https://github.com/lispringing/SuperStar)（服务端刷课）、
> [Khihl-lucky/ChaoXing-AutoAnswer](https://github.com/Khihl-lucky/ChaoXing-AutoAnswer)（AI 答题）
> 与 [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing)（GPL-3.0 核心）实现。
> Web 控制台与一键部署的交互形态参考了 SafeStudy（Flask + 子进程任务管理 + SSE 实时日志）。

---

## 一行命令部署（国内服务器推荐 Gitee 源）

在 **Ubuntu 20.04+（推荐 22.04 / 24.04）** 服务器上以 root 执行：

```bash
cd /tmp && curl -fsSL -o cx.zip https://gitee.com/tgap/cloud-super-star/repository/archive/main.zip \
  && python3 -m zipfile -e cx.zip cx && sudo bash cx/cloud-super-star-main/deploy/install.sh
```

执行后脚本会**交互式提示你粘贴 DeepSeek API Key**（输入不显示在屏幕上），不用把 Key 写在命令里：

```
请粘贴 DeepSeek API Key
  · 申请地址：https://platform.deepseek.com （形如 sk-xxxxxxxx）
  · 输入内容不会显示在屏幕上，粘贴后直接按回车
DeepSeek API Key:
```

为什么是下载 zip 而不是 `.../raw/main/deploy/install.sh`？因为 Gitee 的内容审核会对本仓库里
含"刷课"字样的文件（`README.md`、`deploy/install.sh`）的 raw 链接返回 **451**，整包下载不受影响，
而且顺带省掉一次源码下载。用 python3 解压是因为 Ubuntu 自带 python3、不保证装了 `unzip`。

海外服务器（或 Gitee 不通）用 GitHub 整包下载 + 本地执行：

```bash
cd /tmp && curl -fsSL -o cx.zip https://github.com/TGap-Ruo/CloudSuperStar/archive/refs/heads/main.zip \
  && python3 -m zipfile -e cx.zip cx && sudo bash cx/CloudSuperStar-main/deploy/install.sh
```

也可以直接用 GitHub raw（脚本会自动下载源码，默认 auto 探测 Gitee/GitHub 并按各自仓库名拼接地址）：

```bash
curl -fsSL https://raw.githubusercontent.com/TGap-Ruo/CloudSuperStar/main/deploy/install.sh | sudo bash
```

脚本会自动完成：切换国内 apt 镜像（可选）→ 安装依赖 → 从 Gitee 下载源码 → 部署到 `/opt/chaoxing` → 校验 Key（`--skip-key-test` 可跳过）→ 写配置 `/etc/chaoxing/config.yaml` → 安装并启动 `chaoxing-web`（控制台）与 `chaoxing-serve`（定时调度）→ 放行防火墙端口 → 输出控制台地址与**初始管理员账号密码**。

国内环境适配点：

* 代码来源 `auto` 探测：Gitee（`tgap/cloud-super-star`）与 GitHub（`TGap-Ruo/CloudSuperStar`）会按各自正确的仓库名依次尝试，任一成功即继续；每个地址先用 wget、失败再用 curl 重试
* pip 默认走清华镜像；apt 源在检测到官方源且国内镜像可达时自动切换为阿里云（失败自动还原）
* Key 支持交互式输入，不必出现在命令行历史里
* 时间显示与定时任务时区可用 `--timezone Asia/Shanghai` 指定
* 部署命令不依赖 `raw.githubusercontent.com`（国内常被墙）与 `unzip`（用 python3 解压）

部署完成后终端会打印形如下面的地址，浏览器直接打开即可：

```
控制台地址：http://1.2.3.4:8765/
管理后台：http://1.2.3.4:8765/admin/
管理员账号：admin / 随机初始密码
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--deepseek-key sk-xxx` | 直接提供 Key（默认改为交互式输入，此项供自动化脚本使用） |
| `--model deepseek-flash` | 答题模型（官方当前为 deepseek-flash / deepseek-v4-pro） |
| `--port 8765` | 控制台端口 |
| `--timezone Asia/Shanghai` | 定时任务时区（服务器是 UTC 时会告警） |
| `--admin-path PATH` | 管理后台入口路径（默认 `/admin`，建议改成别人猜不到的） |
| `--admin-password PW` | 初始管理员密码（留空则随机生成并打印） |
| `--max-parallel 8` | 同时运行的任务数上限 |
| `--apt-mirror auto\|aliyun\|tsinghua\|none` | apt 源切换策略（默认 auto 自动判断） |
| `--pip-index URL` | pip 源，默认清华镜像 |
| `--gitee` / `--github` / `--repo owner/name` / `--branch main` | 代码来源（默认 gitee + tgap/cloud-super-star） |
| `--skip-key-test` | 跳过 Key 联网校验（离线或自建中转时使用） |
| `--no-firewall` | 不修改 ufw 规则 |
| `--uninstall` | 卸载服务（保留配置与数据） |

> 重复执行同一条命令 = 更新代码并重启服务，**保留** `/etc/chaoxing/config.yaml` 与 `/var/lib/chaoxing` 数据。

---

## Web 控制台

打开控制台地址后（**默认不需要登录**）：

* **授权码（选填）**：页面上有一个授权码输入框。填了有效授权码就能直接刷；不填则要用登录账号的额度，
  两者都没有会被拒绝。两者都有时**优先扣授权码**。
* **登录（可选）**：右上角「登录」进入登录页，用管理员分配的账号；登录后可看到自己的额度与任务。
* **单个账号（先选课再刷）**：填手机号 + 密码 → 点「开始刷课」→ 程序先登录并**弹出该账号的全部课程列表**（可搜索、全选/全不选、多选或单选）→ 勾完点「开始刷课」才真正执行。弹窗里不勾任何课程 = 刷全部课程；点「取消」会丢弃这次选择并清理临时任务。
* **批量并行**：切到「批量并行」，每行粘贴 `账号,密码`，一次启动多个任务，各自独立进程运行（批量模式不做课程选择，默认全部课程，可用高级设置里的课程 ID 限定）。
* **高级设置**（可折叠）：只刷指定课程 ID、视频倍速、并发章节数、答完是否自动提交、最低题库覆盖率、章节检测重做次数。
  在单个账号模式下，这里填过的课程 ID 会在弹窗里**自动勾选**。
* **任务列表**：运行中 / 已完成 / 失败 / 已停止 计数，支持停止、删除（同时清理日志与账号数据）、下载日志。
* **实时日志**：SSE 推送，按级别着色（错误红 / 警告黄 / 完成绿），可自动滚动、一键清空。
* **定时账号与历史运行**：表格展示 `config.yaml` 里的 cron 账号与最近运行记录（章节完成数、答题命中率、耗时）。
* **消耗确认弹窗**：每次点「开始刷课」都会弹窗说明——这次消耗的是**授权码**还是**账号额度**、
  当前剩余几次、消耗后还剩几次。任务失败（例如学习通账号密码错误）会自动退还次数。
* **Token 消耗按授权码记账**：任务列表里就能看到该任务花了多少 token / 多少钱。
* 顶部徽章显示 **DeepSeek 是否已配置**，未配置会明确提示。
* 管理后台**只在隐藏路径下**，前台不显示任何入口（用管理员账号访问 `/admin/` 即可）。

---

## 服务鉴权与后台管理

**授权码是主要方式，用户是可选补充**，两者互相独立（可以把授权码理解成一种特殊"用户"）。
详细说明见 [docs/admin.md](docs/admin.md)，要点：

* **授权码**：分 **单次授权 / 有限次数 / 无限次数** 三种类型；一张授权码跑一次刷课扣 1 次
  （一次里刷几门课都算 1 次）。支持批量生成、改次数、重置已用、禁用、删除、设置到期时间。
* **优先顺序**：同时提供授权码与登录额度时，先扣授权码；授权码**不能充值到用户账号上**。
* **失败不扣次数**：任务失败（账号密码错误、进程异常等）自动退还。
* **用户（可选）**：管理员可创建账号并配额度、每日任务上限、并发上限；绝大多数场景直接用授权码即可。
* **Token 绑定授权码**：用量记录里带授权码，后台能直接看"这张授权码花了多少钱"。
* **后台板块**：仪表盘、用户管理、授权码管理、任务管理、用量与费用、审计日志、系统设置；
  入口只在 `server.admin_path`（默认 `/admin`），**前台不暴露**。

后台入口默认为 `/admin`，建议在配置里改成别人猜不到的路径：

```yaml
server:
  auth_enabled: true
  admin_path: /cx-admin-9f2       # 可自定义
  admin_user: admin
  admin_password: ""              # 留空则首次启动随机生成并打印
```

---

## Token 用量与费用统计

「刷一个账号花了多少钱」在后台「用量与费用」里看得到，粒度为：**每个任务 / 每个学习通账号 /
每个用户 / 每天 / 每个模型**。

计价口径（人民币元 / 1M tokens，默认值取自 DeepSeek 官方定价页中文站）：

```
费用 = 缓存命中 tokens × 命中单价 + 缓存未命中 tokens × 未命中单价 + 输出 tokens × 输出单价
```

* 高峰价是非高峰价的 2 倍；高峰为 UTC 周一至周五 01:00-04:00 与 06:00-10:00（中国法定节假日除外）。
* DeepSeek 返回的 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 会被精确使用；
  其它兼容接口没有这两个字段时，输入按"未命中"计（略高估，不会低估）。
* 价格写在配置的 `pricing` 段，也可以在后台「系统设置 → 计费单价」直接改：

```yaml
pricing:
  deepseek-flash:   { cache_hit: 0.04, cache_miss: 2.0,  output: 8.0 }
  deepseek-v4-pro:  { cache_hit: 0.30, cache_miss: 9.0,  output: 27.0 }
```

> **实测（2026-09-30，人工智能通识）**：7 个章节（7 视频 + 7 章节检测、21 次 AI 调用）
> 共消耗 **9 078 tokens、¥0.0276**，平均每题 ¥0.0013 —— 一门课的 AI 成本约 **几毛钱**。
> 实现方式：`chaoxing_core` 每次调用大模型后回调一次，把接口返回的 `usage` 交给
> `server/usage.py` 记账（见 `chaoxing_core/README.md` 的改动清单）。与官方账单的对账方法
> 和允许的差异见 [docs/admin.md](docs/admin.md#五token-用量与费用是怎么算的)。

---

## 它能做什么

| 能力 | 说明 |
| --- | --- |
| 视频 / 音频任务 | 按设定倍速上报进度，自动处理 403、验证码拦截、`rt` 参数切换、音视频类型回退 |
| 文档 / PPT / 阅读任务 | 调用任务接口完成任务点 |
| 章节检测 / 作业 | 抓题（含超星字体加密题自动解码）→ 题库 / AI 搜题 → 三级匹配（精确 → 相似度 → 字母）→ 按覆盖率提交 |
| 答题重做 | 提交后检查得分，把错题反馈给 AI 重答，直到全对或达到重做上限 |
| 章节学习次数 | 可选，刷完课后刷够目标次数 |
| 自动签到 | 普通签到有效；手势 / 定位签到尽力而为 |
| 多账号并行 | 网页批量提交，每个账号独立子进程与独立数据目录 |
| 先选课再刷 | 登录后弹出课程列表，可单选/多选/搜索，确认后才开始执行 |
| 定时任务 | `config.yaml` 按 cron 配置账号，或 `systemctl start chaoxing-run@账号名` 手动触发 |
| 消息推送 | Server 酱 / Bark / Telegram / 钉钉 / Qmsg / 自定义 Webhook |
| 运行记录 | SQLite 记录每次运行（章节数、答题命中率、耗时、失败原因），控制台与 `status` 命令均可查 |

---

## 架构

```
        浏览器（server/templates/index.html 单页应用）
         │  填账号密码 / 批量粘贴 / 看实时日志
         ▼
   chaoxing-web  (Flask: server/web.py + server/tasks.py)
     │  POST /api/tasks        每个账号 → 一个独立子进程
     │  GET  /api/stream/<id>  SSE 实时输出
     │  GET  /api/status       定时账号 + 历史运行（读 SQLite）
     ▼
   python -m server.job  ──►  server.runner（登录 → 课程 → 章节 → 任务点）
     │                              │
     │                              ▼
     │                        chaoxing_core（GPL-3.0 核心：解析 / 上报 / 题库）
     ▼
   /var/lib/chaoxing
     ├── state.db                                 运行记录（网页任务与定时任务共用）
     ├── web_tasks/<任务ID>/{config.yaml,run.log}  任务私有配置与输出日志
     └── accounts/web-<任务ID>/{cookies.txt,cache.json,chaoxing.log}

   chaoxing-serve (APScheduler) ──► 同样的 server.job，按 config.yaml 的 cron 拉起
```

关键取舍：

* **每个任务一个进程**：核心里有进程级全局状态（会话单例、题库缓存路径、工作目录），进程隔离比改造成线程安全更稳，而且超时可以直接 kill，一个账号卡死不会影响其它账号。
* **每个任务一份私有配置**：网页提交的账号密码只写进 `web_tasks/<id>/config.yaml`（权限 600），并继承主配置里的 DeepSeek Key 与默认学习参数；任务接口不返回密码。
* **YAML 对外、INI 对内**：`config.yaml` 渲染成核心认识的 `config.ini`，核心代码与上游保持一致，便于跟进更新。

---

## 手动部署 / 命令行使用

不想用一键脚本时：

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone <仓库地址> chaoxing-server && cd chaoxing-server
python3 -m venv .venv && .venv/bin/pip install -e .

# 生成配置并写入 DeepSeek Key
.venv/bin/python deploy/write_config.py --config config.yaml \
    --deepseek-key sk-你的密钥 --data-dir ./data \
    --web-port 8765 --admin-password 你的管理员密码

.venv/bin/chaoxing --config config.yaml check         # 校验配置
.venv/bin/chaoxing --config config.yaml web           # 启动控制台
.venv/bin/chaoxing --config config.yaml run -a main   # 命令行直接跑指定账号
```

命令行速查：

```bash
P=".venv/bin/chaoxing --config config.yaml"
$P check [--login]         # 校验配置；--login 顺便验证账号
$P login -a main           # 登录并保存 Cookie（--cookie "k=v; k2=v2" 导入浏览器 Cookie）
$P courses -a main         # 查看课程 ID（--json 输出 JSON）
$P run -a main             # 立即执行一次（--dry-run 只登录读课程，--course ID 指定课程）
$P tasks                   # 查看网页端提交过的任务
$P status                  # 定时账号 + 最近运行记录
$P serve [--run-on-start]  # 常驻定时调度
$P web --port 8765         # 控制台
```

`/etc/chaoxing/config.yaml` 的 `accounts` 段用于定时任务，示例：

```yaml
accounts:
  - name: main
    username: "13800000000"
    password_env: CHAOXING_MAIN_PASSWORD   # 也可直接写 password
    enabled: true
    schedule: "0 8 * * *"                   # 每天 08:00
```

> 只用网页控制台时，`accounts` 可以留空（只要 `server.web_enabled: true` 就能通过校验）。

---

## 目录结构

```
.
├── chaoxing_core/             # 内置超星核心（改造自 Samueli924/chaoxing，GPL-3.0）
├── server/
│   ├── web.py                 # Flask 主应用：登录、控制台、任务 API + SSE
│   ├── auth.py                # 鉴权：授权码（主）/用户（可选）/额度/审计
│   ├── admin.py               # 管理后台：用户/授权码/任务/用量/审计/设置
│   ├── usage.py               # AI token 用量与费用统计（计价、聚合）
│   ├── tasks.py               # 任务管理：子进程并行、输出捕获、日志与生命周期
│   ├── templates/             # index.html（控制台）/ login.html / admin.html
│   ├── runner.py              # 刷课编排（登录、章节队列、答题、签到、统计）
│   ├── job.py                 # 单账号一次性运行（子进程入口）
│   ├── scheduler.py           # 定时调度（APScheduler + 超时 + 重试）
│   ├── config.py              # YAML 解析 / 校验 / INI 渲染
│   ├── store.py               # SQLite 运行记录
│   ├── notify.py              # 消息推送
│   └── paths.py               # 运行时目录布局
├── deploy/
│   ├── install.sh             # 一行命令一键部署（要求 DeepSeek Key）
│   ├── write_config.py        # 部署时生成 / 更新 config.yaml
│   ├── chaoxing-web.service   # 控制台 systemd 单元
│   ├── chaoxing-serve.service # 定时调度 systemd 单元
│   ├── chaoxing-run@.service  # 手动触发单个账号
│   └── Dockerfile + docker-compose.yml
├── tests/                     # 离线测试（含假超星服务）
├── docs/admin.md              # 鉴权、后台、授权码与费用说明
├── docs/troubleshooting.md    # 常见问题排查
├── config.example.yaml
└── requirements.txt
```

---

## 测试

测试完全离线：`tests/fake_cx.py` 在 `requests` 传输层替换了一个假超星服务，返回与真实页面同构的 HTML/JSON；网页任务用本地 Python 脚本替代真实刷课进程；DeepSeek 用假客户端替代。

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -v
```

覆盖内容：配置解析 / 校验 / INI 渲染、部署配置生成、SQLite 状态、课程过滤、完整刷课链路（登录 → 章节 → 视频上报 → 章节检测 → 提交）、答题三级匹配与提交内容、签到、调度器超时与重试、任务管理器（并行 / 停止 / 删除 / 持久化 / 并发上限）、Web API 与鉴权、CLI 各命令。

---

## ⚠️ 使用须知

1. 刷课通常违反超星平台服务条款，部分学校按学术不端处理；**请自行评估风险，后果自负**。
2. `config.yaml` 与 `/var/lib/chaoxing/**` 内含账号密码与登录 Cookie，不要提交到公开仓库、不要分享给他人。
3. **授权码就是你的使用凭证**，不要外传；管理后台入口建议改路径并只对自己的 IP 开放。
4. 人脸识别、手势签到无法在服务器端完成，需要你在手机或浏览器上手动处理一次。
5. 答题正确率取决于题库与模型，不可能 100% 正确；对成绩敏感的章节检测建议先 `--dry-run` 或用 `submit: false` 观察。
6. 并发别开太大：默认 `jobs: 4`、倍速 `1.0`、同时 8 个任务属于较保守设置；同一账号不建议重复提交任务。

---

## 来源与授权

* 服务端刷课思路：[lispringing/SuperStar](https://github.com/lispringing/SuperStar)、
  [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing)
* AI 答题（DeepSeek、三级答案匹配）：[Khihl-lucky/ChaoXing-AutoAnswer](https://github.com/Khihl-lucky/ChaoXing-AutoAnswer)
* `chaoxing_core/` 继承自 GPL-3.0 项目，故本项目整体以 **GPL-3.0** 授权，见 [`LICENSE`](LICENSE)；与上游的具体差异记录在 [`chaoxing_core/README.md`](chaoxing_core/README.md)。

本项目仅用于个人学习与技术研究，请遵守所在学校规定与平台服务条款。
