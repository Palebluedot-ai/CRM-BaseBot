#!/bin/bash
# 把「每日增量导入」装成 launchd 任务（每天 10:45 / 16:00 各跑一次）。
#
#   ./scripts/install-daily-import-launchd.sh
#
# 回滚：launchctl bootout "gui/$(id -u)/com.chao.crm-basebot.daily-import"
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.chao.crm-basebot.daily-import"
DOMAIN="gui/$(id -u)"
DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="${ROOT}/scripts/${LABEL}.plist.template"
RUNNER="${ROOT}/scripts/run-daily-import.sh"
LOGDIR="${ROOT}/logs"

PATH_VALUE="/opt/homebrew/bin:${HOME}/.local/bin:${HOME}/.nvm/versions/node/v22.22.1/bin:${HOME}/.pyenv/shims:/usr/bin:/bin"

if [ ! -f "${TEMPLATE}" ]; then
  echo "模板不存在：${TEMPLATE}" >&2
  exit 1
fi

if [ ! -f "${ROOT}/.env" ]; then
  echo "警告：${ROOT}/.env 不存在。任务能装上，但跑起来会因为缺凭证失败。" >&2
fi

chmod +x "${RUNNER}"
mkdir -p "${LOGDIR}" "${HOME}/Library/LaunchAgents"

sed \
  -e "s|__SCRIPT__|${RUNNER}|g" \
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
echo "每天 10:45 和 16:00（机器本地时间）各跑一次；脚本幂等，没有新数据就不写 Base。"
echo "日志：${LOGDIR}/daily-import-stdout.log 与 daily-import-stderr.log"
echo "先手工验一次：${RUNNER} --dry-run"
echo "回滚：launchctl bootout ${DOMAIN}/${LABEL}"
