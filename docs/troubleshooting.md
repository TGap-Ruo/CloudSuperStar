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

## 部署命令在国内服务器上跑不动

**`curl ... | sudo bash` 卡住、超时或提示 "Failed to connect"**

`raw.githubusercontent.com` 在国内经常不可达，改用 Gitee 整包下载（推荐，已验证可用）：

```bash
cd /tmp && curl -fsSL -o cx.zip https://gitee.com/tgap/cloud-super-star/repository/archive/main.zip \
  && python3 -m zipfile -e cx.zip cx && sudo bash cx/cloud-super-star-main/deploy/install.sh
```

**Gitee 的 raw 链接返回 451**

`https://gitee.com/tgap/cloud-super-star/raw/main/deploy/install.sh` 会返回
`451 The content may contain violation information` —— 这是 Gitee 的内容审核拦截了含"刷课"字样的文件，
不是脚本坏了。用上面的**整包下载**方式即可（zip 通道不受影响）。

如果连 gitee.com 也访问不了（内网/无外网出口），就在能联网的机器上 `git clone`
后把整个目录上传到服务器，然后执行 `sudo bash deploy/install.sh`。

**海外服务器提示"源码下载失败"**

海外机器上 Gitee 经常限速或拒绝，直接用 GitHub 整包：

```bash
cd /tmp && curl -fsSL -o cx.zip https://github.com/TGap-Ruo/CloudSuperStar/archive/refs/heads/main.zip \
  && python3 -m zipfile -e cx.zip cx && sudo bash cx/CloudSuperStar-main/deploy/install.sh
```

或强制让脚本优先用 GitHub（避免先在 Gitee 上等一轮）：

```bash
curl -fsSL https://raw.githubusercontent.com/TGap-Ruo/CloudSuperStar/main/deploy/install.sh | sudo bash -s -- --github
```

注意两个托管站的仓库名不同：Gitee 是 `tgap/cloud-super-star`，GitHub 是 `TGap-Ruo/CloudSuperStar`
（大小写无关，但路径不同）。脚本会按站点各自的名字拼接下载地址，用 `--repo OWNER/NAME`
可以把两个站点同时覆盖成你自己的仓库。

**卡在"安装系统依赖"很久**

脚本会尝试把 apt 源切成国内镜像（`--apt-mirror auto`，仅当当前是官方源且国内镜像可达时才切，
切换后 `apt-get update` 失败会自动还原）。也可以手动指定：

```bash
cd /tmp && curl -fsSL -o cx.zip https://gitee.com/tgap/cloud-super-star/repository/archive/main.zip \
  && python3 -m zipfile -e cx.zip cx && sudo bash cx/cloud-super-star-main/deploy/install.sh --apt-mirror aliyun
```

**pip 装依赖很慢或失败**

默认已使用清华镜像。可换阿里云源：

```bash
... | sudo bash -s -- --pip-index https://mirrors.aliyun.com/pypi/simple/
```

**提示"无法访问 https://api.deepseek.com，已跳过校验"**

说明服务器连不上 DeepSeek API（或被安全策略拦截）。若确实无法直连，可在部署时加
`--skip-key-test` 跳过校验，并在 `config.yaml` 的 `answer.providers[0]` 里把
`base_url` 换成你能访问的中转地址；答题是否可用以控制台任务日志为准。

**DeepSeek Key 输入错了想改**

重新跑一次部署命令按提示输入新 Key（会保留其它配置），或者直接编辑配置文件：

```bash
sudo sed -i 's#^\( *key: \)sk-.*#\1sk-你的新Key#' /etc/chaoxing/config.yaml
sudo systemctl restart chaoxing-web chaoxing-serve
```

---

## Web 控制台

**登录相关**

* 第一次部署怎么进后台？初始管理员账号密码在部署输出里打印过，也在
  `/var/lib/chaoxing/initial_admin_password.txt`（用完建议删掉）；如果没设 `admin_password`，
  它会被随机生成。也可以用 `grep -A2 '管理员' /var/log/...` 或 `journalctl -u chaoxing-web | grep 初始密码` 找。
* 忘记管理员密码：停掉 web 服务，删掉 `state.db` 里的 admin 记录不可取；更简单的办法是
  用 `sqlite3 /var/lib/chaoxing/state.db` 不行——请用下面这段脚本重置：
  ```bash
  cd /opt/chaoxing && .venv/bin/python -c "
  from server.auth import AuthManager
  a = AuthManager('/var/lib/chaoxing/state.db')
  a.set_password('admin', '新密码至少6位')
  print('已重置')"
  sudo systemctl restart chaoxing-web
  ```
* 登录失败次数过多会临时封禁该 IP（5 分钟 8 次），等几分钟即可。

**额度/卡密相关**

* 提示「刷课次数已用完」：到后台给该用户加额度，或生成卡密让用户自己兑换。
* 提示「今日任务数已达上限」：用户设置了 `daily_task_limit`，后台可改。
* 任务失败会自动退还额度；如果没退，检查 `server.refund_on_failure` 是否为 true，
  以及后台「审计日志」里有没有记录。
* 卡密提示「已用尽」但你没用过：可能这张卡是"有限次数"类型且 `max_uses` 设小了，
  后台点「重置」可清零已用次数。

**费用/token 相关**

* 「用量与费用」是空的：只有**真正调用过大模型**才会有记录。纯看视频不产生 token；
  命中缓存（`cache.json`）的题目也不会重复调用模型。
* 费用和 DeepSeek 官方账单有出入：见 [admin.md](admin.md#五token-用量与费用是怎么算的)
  里列的 5 类正常差异；先把后台「系统设置 → 计费单价」按官网最新价格改对。
* 想省钱：① 保持 `submit: true` 让答对的题进缓存，重刷不再调用；② 用 `deepseek-flash`
  而不是 `deepseek-v4-pro`；③ 非高峰时段跑（UTC 01-04 / 06-10 之外，即北京时间
  9-12 点、14-18 点之外）**注意高峰区间是 UTC**，北京时间的高峰是 09:00-12:00 与 14:00-18:00，
  非高峰正好相反——把任务安排在晚上跑更便宜。

**打开页面提示"需要访问令牌" / 接口返回 401**

* 部署脚本会打印形如 `http://IP:8765/?token=xxxx` 的地址，直接用它打开即可（token 会被浏览器记住）。
* 也可以手动输入令牌：令牌就是 `config.yaml` 里的 `server.web_token`；
  用 `grep web_token /etc/chaoxing/config.yaml` 查看，或在部署时用 `--token 自定义` 指定。
* 想彻底关闭令牌校验：把 `web_token` 设为空并重启 `chaoxing-web`（**开放到公网时非常危险，务必配合安全组限制来源 IP**）。

**页面能打开，但点「开始刷课」没反应 / 一直转圈**

* 按 F12 看 Network 里 `/api/tasks` 的返回：401 = 令牌问题（见上），400 = 参数问题（例如账号为空、并发已满）。
* 错误也会以红色提示条弹在右上角。

**单个账号模式：点了「开始刷课」后先转一会儿才弹窗**

* 正常现象。这一步会**真正登录**学习通并读取课程列表（`/api/courses`），通常 3~10 秒；
  登录失败（密码错、账号被临时冻结、网络不通）会直接弹红色提示，不会开始刷课。
* 弹窗里没列出课程时，说明该账号下没有可取课程或课程都不可用，可直接点「开始刷课」按全部课程处理。
* 弹窗里的课程来自学习通"我学的课"列表，含课程名、教师与课程 ID；可以直接搜索课程名/教师/ID。
* 点「取消」= 丢弃这次选择，对应的临时任务与账号目录会被清理掉，不会残留垃圾数据。
* 勾选后开始刷课时，程序会**复用刚才登录保存的 Cookie**，不会用密码再登录一次；若 Cookie 失效会自动回退密码登录。

**批量模式下没有课程选择弹窗**

* 设计如此：批量是一键启动多个账号，逐个人工选课会失去意义。需要限定课程时，在「高级设置 → 只刷指定课程 ID」里填写，对所有批量账号生效。

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
* 重新执行部署命令，按提示粘贴 Key 即可（会保留已有账号等配置）：
  ```bash
  cd /tmp && curl -fsSL -o cx.zip https://gitee.com/tgap/cloud-super-star/repository/archive/main.zip \
    && python3 -m zipfile -e cx.zip cx && sudo bash cx/cloud-super-star-main/deploy/install.sh
  ```
  或手动编辑 `/etc/chaoxing/config.yaml` 的 `answer.providers[0].key` 后执行
  `systemctl restart chaoxing-web chaoxing-serve`。

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

**日志出现「章节检测有 N/N 题回答错误（成绩 100.0 分）」**

这是 2026-09-30 修复的一个判分 bug（已修复，请更新到最新代码）：

* 超星的「已批阅」页面**经常不显示正确答案**（老师设置了不公开），解析出来是空字符串；
* 旧逻辑用「我的答案 != 正确答案」判断对错 → `"ABC" != ""` → 所有题都被判为答错；
* 于是触发无意义重做，而重做时题目页已是「已批阅」取不到题目 → 章节被判失败并重试 5 次。

现在的判定顺序是：**页面上的对错标记（`marking_dui`/`marking_cuo`）→ 本次成绩 → 可见的正确答案文本比较**；
任何信息都不足以判断时会跳过重做，绝不把已完成的章节判成失败。答案比较也做了归一化
（`AC` 与 `A、C`、`错` 与 `错误` 视为相同）。

**日志出现「章节检测无法作答 / 已提交但不开放重做」**

说明这次章节检测**已经提交过**（页面变成了"已批阅"），而老师没有开放重做次数。
程序会保留现有成绩、不再重做，也不会把章节判为失败。想提高分数只能等老师开放重做，
或到手机/网页端手动重做。

**发现答题记录里有随机答案**

* 说明题库没搜到该题，程序按题型随机选了一个（日志中会标注 `随机选择`）。
* 想彻底避免随机作答：把 `submit` 设为 `false`，或临时把该章节留给自己手动完成。

**题目里出现怪字（如「下巚巓巘巕巗问巙詶是?」「坙坘壢坥坢坖」）**

这是超星的**字体加密题没解出来**——加密题需要靠 `font_map_table.json` 把怪字还原成正常汉字，
映射表没加载时解密会退化成"保留原文"，题目和选项就都是怪字，AI 自然也答不对。

该问题（2026-09-30 修复）根因是打包时把映射表放进了 `chaoxing_core/resources/`，
而代码按上游的 `resource/`（单数、项目根）去找，服务端又把工作目录切到了账号目录 → 表根本没加载。
现在 `resource_path()` 会按 PyInstaller 解包目录 → 包目录 → 项目根目录 → 当前工作目录 依次查找，
并兼容 `resource/` 与 `resources/` 两种目录名。

自检方法：

```bash
cd /opt/chaoxing && .venv/bin/python -c "
from chaoxing_core.cxsecret_font import fonthash_dao
print('字体表条目数:', len(fonthash_dao.hash_map))"
# 应输出三万多条；若为 0 且日志里有"初始化字体哈希数据失败"，说明资源没被正确打包
```

修复后日志里不应再出现 `初始化字体哈希数据失败`，题目文本应为可读中文。

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
