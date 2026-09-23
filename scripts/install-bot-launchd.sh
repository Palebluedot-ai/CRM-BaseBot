#!/bin/bash
# 把机器人装成 launchd 常驻任务（开机自启、崩溃自动拉起）。
#
#   ./scripts/install-bot-launchd.sh
#
# 重装（改了 .env、拉了新代码）直接再跑一次：脚本会先停掉旧的再装新的。
# 回滚：launchctl bootout "gui/$(id -u)/com.chao.crm-basebot.bot"
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.chao.crm-basebot.bot"
DOMAIN="gui/$(id -u)"
DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="${ROOT}/scripts/${LABEL}.plist.template"
LOGDIR="${ROOT}/logs"
PYTHON="${ROOT}/.venv/bin/python"

PATH_VALUE="/opt/homebrew/bin:${HOME}/.local/bin:${HOME}/.nvm/versions/node/v22.22.1/bin:${HOME}/.pyenv/shims:/usr/bin:/bin"

if [ ! -f "${TEMPLATE}" ]; then
  echo "模板不存在：${TEMPLATE}" >&2
  exit 1
fi

# .env 对机器人是硬要求，不像每日导入那样只是警告一下：没有凭证它起来就崩，
# 而 KeepAlive 会把它一直拉起来，变成一个只会写日志的循环。宁可现在装不上。
if [ ! -f "${ROOT}/.env" ]; then
  echo "${ROOT}/.env 不存在 —— 机器人没有凭证起不来，先把 .env 配好再装。" >&2
  echo "  cp .env.target.example .env   然后填 LARK_APP_ID / LARK_APP_SECRET / LARK_BASE_APP_TOKEN" >&2
  exit 1
fi

if [ ! -x "${PYTHON}" ]; then
  echo "找不到 ${PYTHON}" >&2
  echo "  先在仓库根目录跑一次 uv sync，把 .venv 建出来再装。" >&2
  exit 1
fi

# ---- 同一个应用只能有一个长连接 ----
#
# 两个进程拿同一对 App ID/Secret 连上去，飞书按「集群」处理：每条事件只投给其中
# 一个，投给谁不确定。表现是机器人时灵时不灵 —— 销售发十条消息回七条，剩下三条
# 石沉大海，日志里什么错都没有。这是这套部署里最难查的一种故障，所以在这里拦掉。
#
# 已经被 launchd 管着的那个不算「野进程」：下面 bootout 会把它停掉。
OURS=""
if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  OURS="$(launchctl print "${DOMAIN}/${LABEL}" 2>/dev/null \
    | awk -F'= ' '/^[[:space:]]*pid = /{gsub(/ /,"",$2); print $2; exit}')"
fi

# 只认「可执行文件是 python、命令行里带 crm_basebot.app」的进程。
# 不用 pgrep -f：它按整条命令行做子串匹配，连带把「命令行里碰巧提到这个名字」的
# 外层 shell 也算进来（在 bash -c '…crm_basebot.app…' 里跑本脚本就会误报）。
STRAY=""
for pid in $(ps -axo pid=,args= 2>/dev/null | awk '
  {
    n = split($2, seg, "/")
    if (seg[n] !~ /^[Pp]ython/) next
    if (index($0, "crm_basebot.app") == 0) next
    print $1
  }'); do
  [ "${pid}" = "${OURS}" ] && continue
  STRAY="${STRAY}${STRAY:+ }${pid}"
done

if [ -n "${STRAY}" ]; then
  echo "检测到手工起的机器人进程还在跑：PID ${STRAY}" >&2
  echo "" >&2
  echo "同一个应用同时开两条长连接，飞书会把事件随机分给其中一个 —— 消息会无声无息地漏。" >&2
  echo "先把它停掉（终端里 Ctrl-C，或者执行下面这行），再重跑本脚本：" >&2
  echo "" >&2
  echo "  kill ${STRAY}" >&2
  exit 1
fi

mkdir -p "${LOGDIR}" "${HOME}/Library/LaunchAgents"

sed \
  -e "s|__PYTHON__|${PYTHON}|g" \
  -e "s|__WORKDIR__|${ROOT}|g" \
  -e "s|__LOGDIR__|${LOGDIR}|g" \
  -e "s|__PATH__|${PATH_VALUE}|g" \
  "${TEMPLATE}" > "${DEST}"

plutil -lint "${DEST}" >/dev/null

if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "${DOMAIN}/${LABEL}" || true
fi
launchctl bootstrap "${DOMAIN}" "${DEST}"
launchctl enable "${DOMAIN}/${LABEL}"

echo "已安装 ${DEST}"
echo "开机自启，崩溃自动拉起；长连接断了是 SDK 自己重连，不归 launchd 管。"
echo "日志：${LOGDIR}/bot.log（stdout 和 stderr 都在这一个文件里）"
echo ""
echo "验一下（等几秒再看，连接要一两秒才建立）："
echo "  launchctl print ${DOMAIN}/${LABEL} | grep -E 'state|pid'"
echo "  grep 'connected to wss' ${LOGDIR}/bot.log | tail -1"
echo ""
echo "回滚：launchctl bootout ${DOMAIN}/${LABEL}"
