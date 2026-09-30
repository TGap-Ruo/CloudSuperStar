#!/usr/bin/env bash
# =============================================================================
#  ⚠️ 你现在看到的是脚本源码（说明只执行了 curl，没有真正安装）。
#     要真正部署，请把下面这条命令整行复制到服务器执行：
#
#       curl -fsSL https://gitee.com/tgap/cloud-super-star/raw/main/deploy/install.sh | sudo bash
#
#     执行过程中会提示你粘贴 DeepSeek API Key（输入不显示，直接粘贴回车即可）。
#
# =============================================================================
#  超星学习通 · 自动刷课/答题  Ubuntu 一键部署
#
#  服务器一行命令（国内推荐 Gitee 源，会交互式询问 DeepSeek API Key）：
#    curl -fsSL https://gitee.com/tgap/cloud-super-star/raw/main/deploy/install.sh | sudo bash
#
#  海外服务器或 Gitee 不通时，可换 GitHub 源：
#    curl -fsSL https://raw.githubusercontent.com/TGap-Ruo/CloudSuperStar/main/deploy/install.sh | sudo bash
#
#  本地代码部署（在项目根目录）：
#    sudo bash deploy/install.sh
#
#  常用参数：
#    --deepseek-key KEY    直接提供 DeepSeek API Key（默认交互式输入，适合自动化）
#    --model NAME          DeepSeek 模型，默认 deepseek-chat
#    --base-url URL        API 地址，默认 https://api.deepseek.com/v1
#    --port N              Web 控制台端口，默认 8765
#    --timezone TZ         定时任务时区，默认取服务器 /etc/timezone（国内建议 Asia/Shanghai）
#    --token TOKEN         访问令牌；默认自动随机生成
#    --no-token            不启用访问令牌（任何人可访问，风险自负）
#    --max-parallel N      同时运行的任务数上限，默认 8
#    --apt-mirror M        apt 源: auto(默认)/aliyun/tsinghua/none（国内服务器加速）
#    --pip-index URL       pip 源，默认清华镜像
#    --repo OWNER/NAME     代码仓库，默认 tgap/cloud-super-star
#    --gitee / --github    代码来源，默认 gitee
#    --branch NAME         分支，默认 main
#    --skip-key-test       跳过 DeepSeek Key 联网校验
#    --no-firewall         不修改 ufw 规则
#    --uninstall           卸载服务（保留配置与数据）
# =============================================================================
set -euo pipefail

APP_USER="chaoxing"
APP_DIR="/opt/chaoxing"
ETC_DIR="/etc/chaoxing"
DATA_DIR="/var/lib/chaoxing"
WEB_PORT="8765"
WEB_TOKEN=""
WEB_HOST="0.0.0.0"
TIMEZONE=""
MAX_PARALLEL="8"
MODEL="deepseek-chat"
BASE_URL="https://api.deepseek.com/v1"
DEEPSEEK_KEY="${DEEPSEEK_KEY:-}"
REPO="${REPO:-tgap/cloud-super-star}"
BRANCH="${BRANCH:-main}"
GIT_HOST="${GIT_HOST:-gitee}"
APT_MIRROR="${APT_MIRROR:-auto}"
SKIP_KEY_TEST="0"
OPEN_FIREWALL="1"
UNINSTALL="0"
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; BOLD=$'\033[1m'; NC=$'\033[0m'
info()  { printf '%s[信息]%s %s\n' "$GREEN" "$NC" "$*"; }
warn()  { printf '%s[警告]%s %s\n' "$YELLOW" "$NC" "$*"; }
die()   { printf '%s[错误]%s %s\n' "$RED" "$NC" "$*" >&2; exit 1; }
title() { printf '\n%s%s%s\n' "$BOLD" "$*" "$NC"; }
usage() {
  cat <<'USAGE'
超星学习通 · Ubuntu 一键部署

  curl -fsSL https://gitee.com/tgap/cloud-super-star/raw/main/deploy/install.sh | sudo bash

  执行后会提示粘贴 DeepSeek API Key（输入不显示）。

参数：
  --deepseek-key KEY   直接提供 DeepSeek API Key（默认交互式输入）
  --model NAME         模型，默认 deepseek-chat
  --base-url URL       API 地址，默认 https://api.deepseek.com/v1
  --port N             控制台端口，默认 8765
  --timezone TZ        定时任务时区（国内建议 Asia/Shanghai）
  --token TOKEN        访问令牌，默认随机生成
  --no-token           关闭访问令牌（风险自负）
  --max-parallel N     同时运行任务数上限，默认 8
  --apt-mirror M       apt 源: auto/aliyun/tsinghua/none（默认 auto）
  --pip-index URL      pip 源，默认清华镜像
  --repo OWNER/NAME    代码仓库，默认 tgap/cloud-super-star
  --gitee / --github   代码来源，默认 gitee
  --branch NAME        分支，默认 main
  --skip-key-test      跳过 Key 联网校验
  --no-firewall        不修改 ufw
  --uninstall          卸载服务（保留数据）
USAGE
}

# 国内服务器把 apt 源换成国内镜像；失败会自动还原，绝不把机器搞成装不了包的状态。
setup_apt_mirror() {
  local want="${1:-auto}"
  [[ "${want}" == "none" ]] && return 0

  local codename=""
  # shellcheck disable=SC1091
  codename="$(. /etc/os-release 2>/dev/null; echo "${VERSION_CODENAME:-}")"
  [[ -z "${codename}" ]] && codename="$(lsb_release -cs 2>/dev/null || true)"

  local aliyun="https://mirrors.aliyun.com/ubuntu"
  local tuna="https://mirrors.tuna.tsinghua.edu.cn/ubuntu"
  local base=""
  case "${want}" in
    aliyun)   base="${aliyun}" ;;
    tsinghua) base="${tuna}" ;;
    auto)
      grep -rqs -e "archive.ubuntu.com" -e "security.ubuntu.com" \
        /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null || return 0
      if [[ -n "${codename}" ]] && curl -fsS -m 6 -o /dev/null \
          "${aliyun}/dists/${codename}/Release" 2>/dev/null; then
        base="${aliyun}"
      elif [[ -n "${codename}" ]] && curl -fsS -m 6 -o /dev/null \
          "${tuna}/dists/${codename}/Release" 2>/dev/null; then
        base="${tuna}"
      else
        info "未检测到可用的国内镜像，保持原有 apt 源"
        return 0
      fi
      ;;
    *) warn "未知的 --apt-mirror 值: ${want}，保持原有 apt 源"; return 0 ;;
  esac

  local stamp files=()
  stamp="$(date +%Y%m%d%H%M%S)"
  local candidates=(/etc/apt/sources.list)
  local f
  for f in /etc/apt/sources.list.d/*.sources /etc/apt/sources.list.d/*.list; do
    [[ -f "${f}" ]] && candidates+=("${f}")
  done
  for f in "${candidates[@]}"; do
    [[ -f "${f}" ]] && grep -qs -e "ubuntu.com" "${f}" && files+=("${f}")
  done
  [[ ${#files[@]} -eq 0 ]] && return 0

  for f in "${files[@]}"; do
    cp -a "${f}" "${f}.bak-${stamp}"
    sed -i \
      -e "s|http://archive.ubuntu.com/ubuntu|${base}|g" \
      -e "s|https://archive.ubuntu.com/ubuntu|${base}|g" \
      -e "s|http://security.ubuntu.com/ubuntu|${base}|g" \
      -e "s|https://security.ubuntu.com/ubuntu|${base}|g" \
      -e "s|http://ports.ubuntu.com/ubuntu-ports|${base%ubuntu}ubuntu-ports|g" \
      -e "s|https://ports.ubuntu.com/ubuntu-ports|${base%ubuntu}ubuntu-ports|g" \
      "${f}"
  done
  info "apt 源已切换为 ${base}（原文件备份为 *.bak-${stamp}）"

  if ! apt-get update -qq; then
    warn "切换镜像后 apt-get update 失败，正在还原原始 apt 源…"
    for f in "${files[@]}"; do
      [[ -f "${f}.bak-${stamp}" ]] && mv -f "${f}.bak-${stamp}" "${f}"
    done
    apt-get update -qq || warn "还原后 apt-get update 仍失败，请检查服务器网络或安全组"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deepseek-key)   DEEPSEEK_KEY="${2:-}"; shift 2 ;;
    --deepseek-key=*) DEEPSEEK_KEY="${1#*=}"; shift ;;
    --model)          MODEL="${2:-}"; shift 2 ;;
    --base-url)       BASE_URL="${2:-}"; shift 2 ;;
    --port)           WEB_PORT="${2:-}"; shift 2 ;;
    --timezone)       TIMEZONE="${2:-}"; shift 2 ;;
    --host)           WEB_HOST="${2:-}"; shift 2 ;;
    --token)          WEB_TOKEN="${2:-}"; shift 2 ;;
    --no-token)       WEB_TOKEN="__NO_TOKEN__"; shift ;;
    --max-parallel)   MAX_PARALLEL="${2:-}"; shift 2 ;;
    --repo)           REPO="${2:-}"; shift 2 ;;
    --branch)         BRANCH="${2:-}"; shift 2 ;;
    --gitee)          GIT_HOST="gitee"; shift ;;
    --github)         GIT_HOST="github"; shift ;;
    --skip-key-test)  SKIP_KEY_TEST="1"; shift ;;
    --no-firewall)    OPEN_FIREWALL="0"; shift ;;
    --uninstall)      UNINSTALL="1"; shift ;;
    --apt-mirror)     APT_MIRROR="${2:-auto}"; shift 2 ;;
    --pip-index)      PIP_INDEX="${2:-}"; shift 2 ;;
    -h|--help)        usage; exit 0 ;;
    *) die "未知参数: $1（用 --help 查看用法）" ;;
  esac
done

[[ "${EUID}" -eq 0 ]] || die "请用 root 运行：curl -fsSL <install.sh> | sudo bash"

if [[ "${UNINSTALL}" == "1" ]]; then
  info "停止并移除服务"
  systemctl disable --now chaoxing-web.service 2>/dev/null || true
  systemctl disable --now chaoxing-serve.service 2>/dev/null || true
  rm -f /etc/systemd/system/chaoxing-web.service /etc/systemd/system/chaoxing-serve.service
  systemctl daemon-reload
  warn "已移除服务；${APP_DIR} / ${ETC_DIR} / ${DATA_DIR} 仍然保留，如需彻底删除请手动处理。"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
TMP_ROOT="$(mktemp -d /tmp/chaoxing-deploy.XXXXXX)"
cleanup() { rm -rf "${TMP_ROOT}"; }
trap cleanup EXIT

# ---------------------------------------------------------------- 1. 系统依赖
title "1/7 安装系统依赖"
setup_apt_mirror "${APT_MIRROR}"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl wget unzip rsync ca-certificates tzdata >/dev/null
info "Python: $(python3 -V)"
python3 - <<'PY' || die "需要 Python 3.10 及以上版本"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

# ------------------------------------------------------------------ 2. 源代码
title "2/7 准备源代码"
SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" 2>/dev/null && pwd || true)"
SRC_ROOT=""
if [[ -n "${SCRIPT_DIR}" && -f "${SCRIPT_DIR}/../server/cli.py" ]]; then
  SRC_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
  info "使用本地源码: ${SRC_ROOT}"
fi

if [[ -z "${SRC_ROOT}" ]]; then
  case "${GIT_HOST}" in
    github)
      PRIMARY_URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.zip"
      FALLBACK_URL="https://gitee.com/${REPO}/repository/archive/${BRANCH}.zip"
      ;;
    gitee)
      PRIMARY_URL="https://gitee.com/${REPO}/repository/archive/${BRANCH}.zip"
      FALLBACK_URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.zip"
      ;;
    *) die "不支持的代码源: ${GIT_HOST}（可选 github / gitee）" ;;
  esac

  DOWNLOADED="0"
  for url in "${PRIMARY_URL}" "${FALLBACK_URL}"; do
    info "下载源码: ${url}"
    if wget -q --timeout=60 --tries=2 -O "${TMP_ROOT}/repo.zip" "${url}"; then
      DOWNLOADED="1"
      break
    fi
    warn "该地址下载失败，尝试备用地址…"
  done

  if [[ "${DOWNLOADED}" != "1" ]]; then
    warn "源码下载失败（${REPO} / ${BRANCH}）。可选办法："
    warn "  1) 先把仓库镜像到 Gitee，再加 --gitee 重跑本命令"
    warn "  2) 在能联网的机器上 git clone 后把整个目录上传到服务器，然后执行："
    warn "     cd 项目目录 && sudo bash deploy/install.sh（会提示输入 DeepSeek Key）"
    die "无法获取源码，已退出"
  fi

  unzip -q -o "${TMP_ROOT}/repo.zip" -d "${TMP_ROOT}/src" || die "解压失败"
  CLI_FILE="$(find "${TMP_ROOT}/src" -maxdepth 3 -type f -path '*/server/cli.py' -print -quit)"
  [[ -n "${CLI_FILE}" ]] || die "源码结构不符（未找到 server/cli.py）"
  SRC_ROOT="$(dirname "$(dirname "${CLI_FILE}")")"
  info "源码解压到: ${SRC_ROOT}"
fi

# -------------------------------------------------------------- 3. 部署与依赖
title "3/7 部署代码与 Python 环境"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  info "创建系统用户 ${APP_USER}"
  useradd --system --create-home --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi
mkdir -p "${APP_DIR}" "${ETC_DIR}" "${DATA_DIR}"

rsync -a --delete \
  --exclude '.venv' --exclude 'venv' --exclude 'data' --exclude '__pycache__' \
  --exclude '.git' --exclude '_ref' --exclude '*.egg-info' --exclude 'config.yaml' \
  "${SRC_ROOT}/" "${APP_DIR}/"

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  python3 -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/python" -m pip install -q --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install -q -i "${PIP_INDEX}" -e "${APP_DIR}" \
  || die "Python 依赖安装失败（可尝试 PIP_INDEX=https://pypi.org/simple 重新执行）"
info "依赖安装完成"

# -------------------------------------------------------------- 4. DeepSeek Key
title "4/7 配置 DeepSeek API Key（一键做题必需）"

existing_key() {
  [[ -f "${ETC_DIR}/config.yaml" ]] || return 0
  grep -m1 -E '^[[:space:]]+key:[[:space:]]*sk-' "${ETC_DIR}/config.yaml" 2>/dev/null \
    | sed -E 's/^[[:space:]]*key:[[:space:]]*//' | tr -d "\"'" || true
}

# 交互式读取 DeepSeek API Key。
# 注意：curl | bash 时 stdin 是脚本本身，必须从 /dev/tty 读取，否则会把脚本内容吃掉。
prompt_deepseek_key() {
  local attempt=1 value=""
  while [[ ${attempt} -le 3 ]]; do
    {
      printf '\n'
      printf '%s请粘贴 DeepSeek API Key%s\n' "${BOLD}" "${NC}"
      printf '  · 申请地址：https://platform.deepseek.com （形如 sk-xxxxxxxx）\n'
      printf '  · 输入内容不会显示在屏幕上，粘贴后直接按回车\n'
      printf 'DeepSeek API Key: '
    } > /dev/tty

    value=""
    IFS= read -r -s value < /dev/tty || true
    printf '\n' > /dev/tty
    # 去掉粘贴时可能带上的空格/换行/引号
    value="$(printf '%s' "${value}" | tr -d "[:space:]'\"")"

    if [[ -z "${value}" ]]; then
      warn "没有读到内容，请重试（${attempt}/3）"
    elif [[ "${value}" != sk-* || ${#value} -lt 20 ]]; then
      warn "这看起来不像 DeepSeek Key（应以 sk- 开头且长度较长）：${value:0:8}…"
      if [[ ${attempt} -eq 3 ]]; then
        warn "仍按你输入的内容继续（可用 --skip-key-test 跳过校验）"
        DEEPSEEK_KEY="${value}"
        return 0
      fi
    else
      DEEPSEEK_KEY="${value}"
      info "已读取 API Key：${value:0:6}…${value: -4}（共 ${#value} 位）"
      return 0
    fi
    attempt=$((attempt + 1))
  done
  return 1
}

if [[ -z "${DEEPSEEK_KEY}" ]]; then
  DEEPSEEK_KEY="$(existing_key)"
  [[ -n "${DEEPSEEK_KEY}" ]] && info "复用已有配置中的 DeepSeek Key"
fi

if [[ -z "${DEEPSEEK_KEY}" ]]; then
  if [[ -c /dev/tty ]]; then
    prompt_deepseek_key || true
  fi
fi

if [[ -z "${DEEPSEEK_KEY}" ]]; then
  cat >&2 <<EOF
${RED}[错误]${NC} 未提供 DeepSeek API Key，无法启用自动做题。

当前环境没有可交互的终端（例如通过 CI/脚本调用），请改用下面任意一种方式：
  1) 重新执行并跟随提示输入 Key：
     curl -fsSL https://gitee.com/${REPO}/raw/${BRANCH}/deploy/install.sh | sudo bash

  2) 用参数直接传入（适合自动化）：
     curl -fsSL https://gitee.com/${REPO}/raw/${BRANCH}/deploy/install.sh | sudo bash -s -- --deepseek-key sk-你的密钥

  3) 或先导出环境变量：
     export DEEPSEEK_API_KEY=sk-你的密钥
     curl -fsSL https://gitee.com/${REPO}/raw/${BRANCH}/deploy/install.sh | sudo -E bash

Key 只在服务器本地使用，不会外传。部署完成后也可以手动编辑
${ETC_DIR}/config.yaml 的 answer.providers[0].key 再 systemctl restart chaoxing-web chaoxing-serve。
EOF
  exit 1
fi

if [[ "${DEEPSEEK_KEY}" != sk-* ]]; then
  warn "Key 通常以 sk- 开头，当前值看起来不太对：${DEEPSEEK_KEY:0:6}…"
fi

if [[ "${SKIP_KEY_TEST}" != "1" ]]; then
  info "校验 DeepSeek Key 是否可用…"
  http_code="$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' \
      -H "Authorization: Bearer ${DEEPSEEK_KEY}" "${BASE_URL%/}/models" || echo 000)"
  case "${http_code}" in
    200) info "DeepSeek Key 校验通过 ✅" ;;
    401|403) die "DeepSeek Key 无效（HTTP ${http_code}）。请检查 Key 是否正确，或用 --skip-key-test 跳过校验" ;;
    000) warn "无法访问 ${BASE_URL}（网络受限？）。已跳过校验，可稍后用控制台上的任务日志确认" ;;
    *) warn "DeepSeek 返回 HTTP ${http_code}，已跳过校验；若答题失败请检查 Key 与网络" ;;
  esac
fi

# ------------------------------------------------------------------ 5. 写配置
title "5/7 生成配置"

# 服务器时区：云镜像默认多为 Etc/UTC，而 cron 表达式按该时区解释，
# 国内用户若不注意会把"每天 8 点"配成北京时间 16 点。
if [[ -z "${TIMEZONE}" ]]; then
  TIMEZONE="$(cat /etc/timezone 2>/dev/null || echo "")"
  [[ -z "${TIMEZONE}" ]] && TIMEZONE="$(timedatectl show -p Timezone --value 2>/dev/null || echo Asia/Shanghai)"
fi
case "${TIMEZONE}" in
  ""|UTC|Etc/UTC|GMT|Etc/GMT)
    warn "检测到服务器时区为 ${TIMEZONE:-UTC}：定时任务的 cron 会按该时区执行。"
    warn "国内用户建议：本命令加 --timezone Asia/Shanghai，或先执行 timedatectl set-timezone Asia/Shanghai"
    ;;
  *) info "定时任务时区: ${TIMEZONE}" ;;
esac

if [[ -z "${WEB_TOKEN}" ]]; then
  WEB_TOKEN="$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  info "已自动生成访问令牌"
elif [[ "${WEB_TOKEN}" == "__NO_TOKEN__" ]]; then
  WEB_TOKEN=""
  warn "你选择了 --no-token：控制台无需令牌即可访问，请务必用防火墙/安全组限制来源 IP"
fi

"${APP_DIR}/.venv/bin/python" "${APP_DIR}/deploy/write_config.py" \
  --config "${ETC_DIR}/config.yaml" \
  --template "${APP_DIR}/config.example.yaml" \
  --deepseek-key "${DEEPSEEK_KEY}" \
  --model "${MODEL}" \
  --base-url "${BASE_URL}" \
  --data-dir "${DATA_DIR}" \
  --web-host "${WEB_HOST}" \
  --web-port "${WEB_PORT}" \
  --web-token "${WEB_TOKEN}" \
  --web-max-parallel "${MAX_PARALLEL}" \
  --timezone "${TIMEZONE}" \
  || die "生成配置文件失败"

cat > "${ETC_DIR}/chaoxing.env" <<EOF
# 由部署脚本生成；账号密码等敏感信息可以放这里，配合 config.yaml 的 password_env 使用
PYTHONIOENCODING=utf-8
PYTHONUNBUFFERED=1
TZ=${TIMEZONE}
EOF

chmod 600 "${ETC_DIR}/config.yaml" "${ETC_DIR}/chaoxing.env"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}" "${DATA_DIR}" "${ETC_DIR}"
chmod 750 "${ETC_DIR}"

# ------------------------------------------------------------------ 6. 服务
title "6/7 安装 systemd 服务"
render_unit() {
  local template="$1" target="$2"
  sed -e "s|__APP_DIR__|${APP_DIR}|g" \
      -e "s|__ETC_DIR__|${ETC_DIR}|g" \
      -e "s|__DATA_DIR__|${DATA_DIR}|g" \
      -e "s|__USER__|${APP_USER}|g" \
      -e "s|__PORT__|${WEB_PORT}|g" \
      "${template}" > "${target}"
  chmod 644 "${target}"
}

render_unit "${APP_DIR}/deploy/chaoxing-web.service" /etc/systemd/system/chaoxing-web.service
render_unit "${APP_DIR}/deploy/chaoxing-serve.service" /etc/systemd/system/chaoxing-serve.service
render_unit "${APP_DIR}/deploy/chaoxing-run@.service" /etc/systemd/system/chaoxing-run@.service

systemctl daemon-reload
systemctl enable chaoxing-web.service >/dev/null 2>&1
systemctl enable chaoxing-serve.service >/dev/null 2>&1
systemctl restart chaoxing-web.service
systemctl restart chaoxing-serve.service

if [[ "${OPEN_FIREWALL}" == "1" ]] && command -v ufw >/dev/null 2>&1; then
  if ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "${WEB_PORT}/tcp" >/dev/null 2>&1 && info "ufw 已放行 ${WEB_PORT}/tcp"
  fi
fi

# ------------------------------------------------------------ 7. 健康检查
title "7/7 健康检查"
sleep 3
if systemctl is-active --quiet chaoxing-web.service; then
  info "chaoxing-web  运行中"
else
  warn "chaoxing-web  未运行，请查看：journalctl -u chaoxing-web -n 50"
fi
if systemctl is-active --quiet chaoxing-serve.service; then
  info "chaoxing-serve 运行中"
else
  warn "chaoxing-serve 未运行，请查看：journalctl -u chaoxing-serve -n 50"
fi

if curl -fsS --max-time 5 "http://127.0.0.1:${WEB_PORT}/healthz" >/dev/null 2>&1; then
  info "Web 控制台自检通过"
else
  warn "Web 控制台自检失败，请查看：journalctl -u chaoxing-web -n 50"
fi

# 公网 IP：优先国外服务，失败则用国内服务，最后退回本机网卡地址
PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
[[ -z "${PUBLIC_IP}" ]] && PUBLIC_IP="$(curl -fsS --max-time 5 https://ip.3322.net 2>/dev/null | tr -d '[:space:]' || true)"
[[ -z "${PUBLIC_IP}" ]] && PUBLIC_IP="$(curl -fsS --max-time 5 https://ifconfig.me/ip 2>/dev/null | tr -d '[:space:]' || true)"
[[ -z "${PUBLIC_IP}" ]] && PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[[ -z "${PUBLIC_IP}" ]] && PUBLIC_IP="<服务器IP>"

TOKEN_SUFFIX=""
[[ -n "${WEB_TOKEN}" ]] && TOKEN_SUFFIX="?token=${WEB_TOKEN}"

cat <<EOF

${GREEN}${BOLD}部署完成 ✅${NC}

  ${BOLD}控制台地址${NC}：http://${PUBLIC_IP}:${WEB_PORT}/${TOKEN_SUFFIX}
  ${BOLD}访问令牌${NC}：${WEB_TOKEN:-（未启用）}
  ${BOLD}DeepSeek${NC}：${MODEL} @ ${BASE_URL}
  ${BOLD}配置文件${NC}：${ETC_DIR}/config.yaml
  ${BOLD}数据目录${NC}：${DATA_DIR}
  ${BOLD}代码来源${NC}：${GIT_HOST} ${REPO}@${BRANCH}

  ${BOLD}浏览器打不开控制台？${NC}
  国内直连国外服务器的非标准端口（如 ${WEB_PORT}）常被运营商拦截，此时用 SSH 隧道最稳：
    ssh -L ${WEB_PORT}:127.0.0.1:${WEB_PORT} root@${PUBLIC_IP}
    然后在本地浏览器打开：http://127.0.0.1:${WEB_PORT}/${TOKEN_SUFFIX}

  打开控制台后，直接填写学习通「账号 + 密码」即可开始刷课：
    · 支持「批量并行」：每行一个账号（账号,密码）
    · 右侧实时显示日志，可停止 / 删除任务 / 下载日志
    · 答题使用你刚才填写的 DeepSeek Key，无需再配置

  常用运维命令：
    systemctl status chaoxing-web        # 控制台服务
    systemctl status chaoxing-serve      # 定时调度服务
    journalctl -u chaoxing-web -f        # 实时日志
    sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/chaoxing --config ${ETC_DIR}/config.yaml status

  如需定时自动刷课，编辑 ${ETC_DIR}/config.yaml 的 accounts 段
  （cron 示例：schedule: "0 8 * * *"），再 systemctl restart chaoxing-serve。

  ${YELLOW}提醒：刷课可能违反平台条款与校规，请自行评估风险；请勿把访问令牌分享给他人。${NC}

EOF
