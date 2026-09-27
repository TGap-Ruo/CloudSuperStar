# 常见问题排查

排查任何问题的第一步都是看日志：

```bash
# systemd 部署
sudo journalctl -u chaoxing-serve -n 200 --no-pager
tail -f /var/lib/chaoxing/accounts/<账号>/chaoxing.log     # TRACE 级详细日志

# 手动部署
.venv/bin/python -m server.cli status
tail -f data/accounts/<账号>/chaoxing.log
```

提高日志详细程度：把 `config.yaml` 的 `server.log_level` 改成 `DEBUG`（或 `TRACE`）后重启服务。

---

## Web 控制台

**打开页面提示"需要访问令牌" / 接口返回 401**

* 部署脚本会打印形如 `http://IP:8765/?token=xxxx` 的地址，直接用它打开即可（token 会被浏览器记住）。
* 也可以手动输入令牌：令牌就是 `config.yaml` 里的 `server.web_token`；
  用 `grep web_token /etc/chaoxing/config.yaml` 查看，或在部署时用 `--token 自定义` 指定。
* 想彻底关闭令牌校验：把 `web_token` 设为空并重启 `chaoxing-web`（**开放到公网时非常危险，务必配合安全组限制来源 IP**）。

**页面能打开，但点「开始刷课」没反应 / 一直转圈**

* 按 F12 看 Network 里 `/api/tasks` 的返回：401 = 令牌问题（见上），400 = 参数问题（例如账号为空、并发已满）。
* 错误也会以红色提示条弹在右上角。

**批量启动时只有一部分成功了**

* 单批次受 `server.web_max_parallel_tasks`（部署参数 `--max-parallel`）限制，超出会返回
  `同时运行的任务已达上限`。等前面的任务跑完再提交，或调大该值（不建议超过 8~10）。

**任务一直停在"运行中"**

* 长视频可能跑几小时，看右侧日志是否在持续输出。
* 若日志不动了：点「停止」；或在服务器上 `ps aux | grep server.job` 查看是否还有进程。
  web 服务重启会把残留的"运行中"任务标记为「已停止」。
* 单账号的运行时长上限由 `server.run_timeout_minutes` 控制，超时会自动终止并记录为超时。

**网页任务的数据在哪里**

* 任务私有配置与输出日志：`/var/lib/chaoxing/web_tasks/<任务ID>/`
* 该账号的 Cookie / 缓存 / 详细日志：`/var/lib/chaoxing/accounts/web-<任务ID>/`
* 运行记录（与定时任务共用一张表）：`/var/lib/chaoxing/state.db`，
  命令行查看：`sudo -u chaoxing /opt/chaoxing/.venv/bin/chaoxing --config /etc/chaoxing/config.yaml tasks`

**顶部显示"未配置 DeepSeek Key"**

* 说明 `config.yaml` 的 `answer.providers[0].key` 还是占位符或为空，此时无法自动做题。
* 重新执行部署命令并带上 `--deepseek-key sk-xxx` 即可（会保留已有账号等配置），
  或手动编辑 `/etc/chaoxing/config.yaml` 后 `systemctl restart chaoxing-web chaoxing-serve`。

**想让控制台只允许自己访问**

* 最稳妥：不要开放端口到公网，用 SSH 隧道
  ```bash
  ssh -L 8765:127.0.0.1:8765 user@你的服务器
  # 本地浏览器打开 http://127.0.0.1:8765/?token=xxxx
  ```
  同时把 `config.yaml` 的 `server.web_host` 改为 `127.0.0.1` 并重启服务。
* 或者用云厂商安全组只放行你的家庭/公司出口 IP。

---

## 登录相关

**登录失败：账号或密码错误**

* 先用手机号 + 密码在浏览器确认能正常登录（有些学校要求走统一身份认证，需要先在
  i.chaoxing.com 绑定手机号）。
* 账号开了二次验证时，密码登录会失败，请改用 Cookie：浏览器登录后复制 Cookie，
  执行 `.venv/bin/python -m server.cli login -a main --cookie "k=v; k2=v2"`，
  然后在配置里设置 `use_cookies: true`。

**Cookie 经常失效**

* 学习通会在多地登录时互踢。如果同一账号在手机上频繁登录，服务器端 Cookie 可能被挤掉。
  本项目在 Cookie 失效且配置了密码时，会自动回退到密码登录，所以推荐**同时填好账号密码**。

**登录被要求验证码 / 滑块**

* 触发风控的信号：`403`、返回内容里包含 `验证码`/`validate`。
* 可选安装 `ddddocr` 让程序自动过图形验证码：
  ```bash
  .venv/bin/pip install -r requirements-optional.txt
  ```
  同时把倍速降到 1.0、`jobs` 降到 2~3、账号错开时间运行。
* 滑块/短信类验证码无法自动通过，请在手机端完成一次，然后立刻 `login` 保存 Cookie。

---

## 刷课相关

**章节显示"未开启 / 未开放"**

* 常见原因是**上一章的章节检测没有提交**，导致后续章节被锁。
* 处理办法：把 `answer.submit` 设为 `true`，并配置可用的题库（推荐 AI）；
  确认能答对后再跑一次。若该章节本身已过期关闭，可把 `study.notopen_action`
  设为 `continue` 直接跳过，避免整轮卡住。

**视频进度上不去 / 反复重试**

* 学习通对上报频率有限制，本项目已限速（每次上报间隔 ≥2 秒）。
* 倍速过高（>1.5）容易被识别成异常，建议 `speed: 1.0~1.5`。
* 日志里出现 `403` 时会自动刷新会话重试；连续失败会跳过该任务点并在报告里标记失败。

**弹出了人脸识别 / 拍照签到**

* 服务器端无法完成。请在手机或浏览器上手动通过一次，之后程序会继续执行。
* 若该视频反复要求人脸识别，建议把该课程从 `include_courses` 中排除，手动完成。

**任务点全都显示完成了，但学习通里进度没变**

* 学习通的进度统计有延迟（通常几分钟到半小时），可以稍后在 App 里刷新查看。
* 若长时间不变，检查日志里是否有 `提交答题失败`、`403`、`视频进度上报返回` 等错误。

---

## 答题相关

**答案错误 / 覆盖率低**

* 先在日志里搜 `章节检测题库覆盖率`，看看有多少题没搜到。
* 改用 AI 题库（DeepSeek）通常能显著提升覆盖率；也可以配多个题库做回退。
* 提高 `cover_rate`（例如 0.9）会让程序在覆盖率不足时只保存不提交，减少"全错提交"的风险，
  但代价是章节可能被锁住，需要你手动去补交。
* 开启 `work_max_retries`（默认 3）后，程序会在提交后检查得分，把错题反馈给 AI 重做。

**发现答题记录里有随机答案**

* 说明题库没搜到该题，程序按题型随机选了一个（日志中会标注 `随机选择`）。
* 想彻底避免随机作答：把 `submit` 设为 `false`，或临时把该章节留给自己手动完成。

**题目是图片 / 带特殊字体**

* 超星的字体加密题（`cxSecretStyle`）已内置解码，日志里出现
  `未找到字体文件，可能是未加密的题目` 属正常提示。
* 纯图片题需要模型支持视觉，当前 AI provider 走的是文本接口；这类题通常能通过
  题库（TikuLike 等支持视觉的 provider）或人工补交解决。

---

## 调度与服务

**到点了但没跑**

* `systemctl status chaoxing-serve` 看服务是否在运行；
* `schedule` 用的是服务器时区（`server.timezone`），不是学习通或你手机的时区；
* 审计排程：`.venv/bin/python -m server.cli status` 会显示每条 cron 的下次触发时间；
* 服务重启期间的错过的任务会在 1 小时内补跑（`misfire_grace_time=3600`）。

**服务启动后立刻退出**

* 配置校验失败会直接退出，先跑 `check`：
  ```bash
  .venv/bin/python -m server.cli --config /etc/chaoxing/config.yaml check
  ```
* systemd 下注意文件权限：`/etc/chaoxing/config.yaml` 与 `chaoxing.env` 应当可被
  `chaoxing` 用户读取（安装脚本已设为 600 + 属主 chaoxing）。

**运行记录一直显示 running**

* 说明上次运行被强杀（重启、断电、OOM）。调度服务启动时会自动把残留的 `running`
  记录标记为 `failed`，也可以直接重跑一次。

**想看某个账号到底卡在哪一章**

```bash
.venv/bin/python -m server.cli status -a main --limit 20
cat data/accounts/main/runs/<run_id>.json     # 单次运行的完整报告
```

---

## 性能与资源

* 单账号一轮通常占用 50~150 MB 内存，主要是 Python 与依赖本身；CPU 几乎空闲。
* 1 核 1G 的轻量云服务器跑 2~3 个账号没问题，但建议 `max_concurrent_accounts: 1`，
  让账号串行执行更稳。
* 视频任务本身是"按秒睡觉"的循环，很省资源；磁盘占用主要来自日志（自动轮转，保留 14 天）。

---

## 安全建议

* 服务器只开放 SSH，状态面板保持 `web_host: 127.0.0.1` 并用 SSH 隧道访问。
* `config.yaml`、`data/accounts/*/cookies.txt` 都包含账号凭据，不要进 Git、不要给他人。
* 定期 `chmod 600 /etc/chaoxing/config.yaml /etc/chaoxing/chaoxing.env`。
* 用完随时 `sudo systemctl stop chaoxing-serve` 停止，或在配置里把账号 `enabled: false`。
