#!/usr/bin/env bash
# =============================================================================
#  ⚠️ 你现在看到的是脚本源码（说明只执行了 curl，没有真正安装）。
#     要真正部署，请把下面这条命令整行复制到服务器执行：
#
#       cd /tmp && curl -fsSL -o cx.zip https://gitee.com/tgap/cloud-super-star/repository/archive/main.zip \
#         && python3 -m zipfile -e cx.zip cx && sudo bash cx/cloud-super-star-main/deploy/install.sh
#
#     执行过程中会提示你粘贴 DeepSeek API Key（输入不显示，直接粘贴回车即可）。
#
# =============================================================================
#  超星学习通 · 自动刷课/答题  Ubuntu 一键部署
#
#  服务器一行命令（国内推荐 Gitee 源；会交互式询问 DeepSeek API Key）：
#    cd /tmp && curl -fsSL -o cx.zip https://gitee.com/tgap/cloud-super-star/repository/archive/main.zip \
#      && python3 -m zipfile -e cx.zip cx && sudo bash cx/cloud-super-star-main/deploy/install.sh
#
#  说明：Gitee 的 raw 链接对本仓库内容会返回 451（平台内容审核），所以走整包下载，
#        顺带省掉一次源码下载；python3 是 Ubuntu 自带的，不需要额外装 unzip。
#
#  海外服务器或 Gitee 不通时，可换 GitHub raw：
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
REPO="${REPO:-}"                        # --repo 会同时覆盖两个托管站
REPO_GITEE="${REPO_GITEE:-${REPO:-tgap/cloud-super-star}}"
REPO_GITHUB="${REPO_GITHUB:-${REPO:-TGap-Ruo/CloudSuperStar}}"
BRANCH="${BRANCH:-main}"
GIT_HOST="${GIT_HOST:-auto}"
APT_MIRROR="${APT_MIRROR:-auto}"
PIP_CANDIDATES=()
PIP_INDEX_CHOSEN=""
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

  cd /tmp && curl -fsSL -o cx.zip https://gitee.com/tgap/cloud-super-star/repository/archive/main.zip \
    && python3 -m zipfile -e cx.zip cx && sudo bash cx/cloud-super-star-main/deploy/install.sh

  执行后会提示粘贴 DeepSeek API Key（输入不显示）。

  （海外服务器也可用 GitHub 源：
    curl -fsSL https://raw.githubusercontent.com/TGap-Ruo/CloudSuperStar/main/deploy/install.sh | sudo bash）

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
  --repo OWNER/NAME    同时覆盖两个托管站的仓库名（默认 Gitee: tgap/cloud-super-star，GitHub: TGap-Ruo/CloudSuperStar）
  --gitee / --github   指定优先代码来源，默认 auto（自动探测，两个源都会尝试）
  --branch NAME        分支，默认 main
  --skip-key-test      跳过 Key 联网校验
  --no-firewall        不修改 ufw
  --uninstall          卸载服务（保留数据）
USAGE
}

# ─────────────────────────── pip 源探测（解决 Errno 101 / 国外源不可达）──
# 很多国内 VPS 只有 IPv4 路由，但镜像域名带 AAAA 记录，客户端优先走 IPv6 时
# 会立刻返回 "Errno 101 Network is unreachable"。这里逐源探测并优先使用 IPv4。
PREFER_IPV4_DONE="0"

prefer_ipv4() {
  [[ "${PREFER_IPV4_DONE}" == "1" ]] && return 0
  PREFER_IPV4_DONE="1"
  [[ -f /etc/gai.conf ]] || return 0
  grep -qs "precedence ::ffff:0:0/96" /etc/gai.conf && return 0
  cp -a /etc/gai.conf "/etc/gai.conf.bak-$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
  printf '\n# chaoxing 部署脚本追加：IPv6 不可达时优先使用 IPv4\nprecedence ::ffff:0:0/96  100\n' >> /etc/gai.conf
  info "检测到 IPv6 不通而 IPv4 正常，已让系统优先使用 IPv4（/etc/gai.conf）"
}

http_code_of() {
  # $1=url $2=可选 curl 附加参数；返回 HTTP 码，000 表示不可达
  local code
  # shellcheck disable=SC2086
  code="$(curl -sS -m 8 -o /dev/null -w '%{http_code}' ${2:-} "$1" 2>/dev/null || true)"
  echo "${code:-000}"
}

url_ok() {
  # 只认 2xx/3xx：403/451/5xx 这类「能连上但用不了」必须排除
  local code
  code="$(http_code_of "$1" "${2:-}")"
  case "${code}" in
    2??|3??) return 0 ;;
    *) return 1 ;;
  esac
}

build_pip_candidates() {
  local raw=()
  # 优先尊重用户/系统已有配置（/etc/pip.conf 里的 index-url 通常已经被验证可用）
  local configured=""
  configured="$(grep -hs -m1 -E '^\s*index-url\s*=' /etc/pip.conf ~/.pip/pip.conf ~/.config/pip/pip.conf 2>/dev/null \
    | sed -E 's/^\s*index-url\s*=\s*//' | tr -d ' ' || true)"
  [[ -n "${configured}" ]] && raw+=("${configured}")
  raw+=("${PIP_INDEX}" "https://pypi.tuna.tsinghua.edu.cn/simple" "https://mirrors.aliyun.com/pypi/simple/" \
        "https://mirrors.cloud.tencent.com/pypi/simple/" "https://pypi.org/simple/")

  local seen=" " idx
  PIP_CANDIDATES=()
  for idx in "${raw[@]}"; do
    [[ -z "${idx}" ]] && continue
    case "${seen}" in *" ${idx} "*) continue ;; esac
    seen="${seen}${idx} "
    PIP_CANDIDATES+=("${idx}")
  done
}

select_pip_index() {
  local idx probe code
  for idx in "${PIP_CANDIDATES[@]}"; do
    probe="${idx%/}/setuptools/"
    if url_ok "${probe}"; then
      PIP_INDEX_CHOSEN="${idx}"
      return 0
    fi
    if url_ok "${probe}" "-4"; then
      # 仅 IPv4 可达 → 典型的 IPv6 无路由，改 gai.conf 后 pip 也能通
      prefer_ipv4
      PIP_INDEX_CHOSEN="${idx}"
      return 0
    fi
    code="$(http_code_of "${probe}")"
    warn "pip 源不可用: ${idx}（HTTP ${code}）"
  done
  return 1
}

diagnose_pip_failure() {
  warn "所有 pip 源都不可达，下面是一些诊断信息："
  warn "  默认路由："
  ip -4 route show default 2>/dev/null | sed 's/^/    /' >&2 || true
  ip -6 route show default 2>/dev/null | sed 's/^/    /' >&2 || true
  warn "  DNS 解析 pypi.tuna.tsinghua.edu.cn："
  getent ahosts pypi.tuna.tsinghua.edu.cn 2>/dev/null | head -4 | sed 's/^/    /' >&2 || true
  warn "  连通性："
  warn "    IPv4 curl: $(http_code_of https://pypi.tuna.tsinghua.edu.cn/simple/setuptools/ -4)"
  warn "    IPv6 curl: $(http_code_of https://pypi.tuna.tsinghua.edu.cn/simple/setuptools/ -6)"
  local idx
  for idx in "${PIP_CANDIDATES[@]}"; do
    warn "    ${idx} -> HTTP $(http_code_of "${idx%/}/setuptools/")"
  done
  warn "  可尝试："
  warn "    1) 指定可达的源：  ... | sudo bash -s -- --pip-index https://你的源/simple/"
  warn "    2) 若 IPv6 不通：  echo 'precedence ::ffff:0:0/96 100' >> /etc/gai.conf"
  warn "    3) 无外网出口时，用能联网的机器下载 wheels 后离线安装"
}

install_python_deps() {
  local python_bin="${APP_DIR}/.venv/bin/python"
  local args=(-q --disable-pip-version-check --timeout 20 --retries 2)
  local chosen="${PIP_INDEX_CHOSEN}"
  local idx ok="0"

  info "使用 pip 源: ${chosen}"
  "${python_bin}" -m pip install "${args[@]}" --upgrade pip -i "${chosen}" \
    || warn "pip 自身升级失败，继续使用 venv 自带版本"

  for idx in "${chosen}" "${PIP_CANDIDATES[@]}"; do
    [[ -z "${idx}" ]] && continue
    if "${python_bin}" -m pip install "${args[@]}" -i "${idx}" -e "${APP_DIR}"; then
      ok="1"
      [[ "${idx}" != "${chosen}" ]] && PIP_INDEX_CHOSEN="${idx}"
      break
    fi
    warn "用 ${idx} 安装依赖失败，尝试下一个源…"
  done

  if [[ "${ok}" != "1" ]]; then
    diagnose_pip_failure
    die "Python 依赖安装失败"
  fi
}

# 国内服务器把 apt 源换成国内镜像；失败会自动还原，绝不把机器搞成装不了包的状态。
# ─────────────────────────── 源码下载（Gitee / GitHub 双源）───────────────
repo_for_host() {
  case "$1" in
    gitee)  echo "${REPO_GITEE}" ;;
    github) echo "${REPO_GITHUB}" ;;
    *) return 1 ;;
  esac
}

zip_url_for_host() {
  local host="$1" repo
  repo="$(repo_for_host "${host}")" || return 1
  case "${host}" in
    gitee)  echo "https://gitee.com/${repo}/repository/archive/${BRANCH}.zip" ;;
    github) echo "https://github.com/${repo}/archive/refs/heads/${BRANCH}.zip" ;;
  esac
}

order_hosts() {
  # 显式指定则按指定顺序；auto 时用短探测决定顺序（探测不准也仍会依次尝试）
  if [[ "${GIT_HOST}" == "gitee" || "${GIT_HOST}" == "github" ]]; then
    echo "${GIT_HOST}"
    if [[ "${GIT_HOST}" == "gitee" ]]; then echo "github"; else echo "gitee"; fi
    return 0
  fi
  local g h
  g="$(http_code_of "https://gitee.com/")"
  h="$(http_code_of "https://github.com/")"
  if [[ "${g}" == "000" && "${h}" != "000" ]]; then
    echo "github"
    echo "gitee"
  else
    # 都可达或都探测失败：国内优先 Gitee，其后仍会尝试 GitHub
    echo "gitee"
    echo "github"
  fi
}

try_download() {
  # wget / curl 各试一次：两者对代理、重定向、TLS 的处理不同，多一层保险
  local url="$1" out="$2"
  if wget -q --timeout=30 --tries=1 -O "${out}" "${url}" 2>/dev/null; then
    return 0
  fi
  curl -fsSL --connect-timeout 10 -m 150 -o "${out}" "${url}" 2>/dev/null
}

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
      local cand probe release
      for cand in "${aliyun}" "${tuna}"; do
        [[ -z "${codename}" ]] && break
        release="${cand}/dists/${codename}/Release"
        if url_ok "${release}"; then
          base="${cand}"
          break
        fi
        if url_ok "${release}" "-4"; then
          prefer_ipv4
          base="${cand}"
          break
        fi
      done
      if [[ -z "${base}" ]]; then
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
  SOURCE_HOSTS=()
  while IFS= read -r _host; do
    [[ -n "${_host}" ]] && SOURCE_HOSTS+=("${_host}")
  done < <(order_hosts)
  info "代码来源顺序: ${SOURCE_HOSTS[*]}（Gitee: ${REPO_GITEE} / GitHub: ${REPO_GITHUB}）"
  DOWNLOADED="0"
  for host in "${SOURCE_HOSTS[@]}"; do
    url="$(zip_url_for_host "${host}")" || continue
    info "下载源码: ${url}"
    if try_download "${url}" "${TMP_ROOT}/repo.zip"; then
      DOWNLOADED="1"
      break
    fi
    warn "该地址下载失败（${host}），尝试下一个来源…"
  done

  if [[ "${DOWNLOADED}" != "1" ]]; then
    warn "源码下载失败（分支 ${BRANCH}）。可选办法："
    warn "  1) 整包下载 · 国内（Gitee）："
    warn "     cd /tmp && curl -fsSL -o cx.zip https://gitee.com/${REPO_GITEE}/repository/archive/${BRANCH}.zip && python3 -m zipfile -e cx.zip cx && sudo bash cx/*/deploy/install.sh"
    warn "  2) 整包下载 · 海外（GitHub）："
    warn "     cd /tmp && curl -fsSL -o cx.zip https://github.com/${REPO_GITHUB}/archive/refs/heads/${BRANCH}.zip && python3 -m zipfile -e cx.zip cx && sudo bash cx/*/deploy/install.sh"
    warn "  3) 在能联网的机器上 git clone 后把整个目录上传到服务器，然后执行："
    warn "     cd 项目目录 && sudo bash deploy/install.sh（会提示输入 DeepSeek Key）"
    die "无法获取源码，已退出"
  fi

  mkdir -p "${TMP_ROOT}/src"
  unzip -q -o "${TMP_ROOT}/repo.zip" -d "${TMP_ROOT}/src" 2>/dev/null \
    || python3 -m zipfile -e "${TMP_ROOT}/repo.zip" "${TMP_ROOT}/src" \
    || die "源码解压失败"
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
build_pip_candidates
if ! select_pip_index; then
  diagnose_pip_failure
  die "找不到可用的 Python 软件源，已停止"
fi
install_python_deps
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
  1) 重新执行并跟随提示输入 Key（国内推荐）：
     cd /tmp && curl -fsSL -o cx.zip https://gitee.com/${REPO}/repository/archive/${BRANCH}.zip \\
       && python3 -m zipfile -e cx.zip cx && sudo bash cx/*/deploy/install.sh

  2) 用参数直接传入（适合自动化）：
     sudo bash cx/*/deploy/install.sh --deepseek-key sk-你的密钥

  3) 或先导出环境变量：
     export DEEPSEEK_API_KEY=sk-你的密钥
     sudo -E bash cx/*/deploy/install.sh

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
