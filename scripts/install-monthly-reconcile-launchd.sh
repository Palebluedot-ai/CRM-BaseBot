#!/bin/bash
# 把「每月结算」装成 launchd 任务（每月 3 号 10:00 跑上个月）。
#
#   ./scripts/install-monthly-reconcile-launchd.sh
#
# 回滚：launchctl bootout "gui/$(id -u)/com.chao.crm-basebot.monthly-reconcile"
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.chao.crm-basebot.monthly-reconcile"
DOMAIN="gui/$(id -u)"
DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="${ROOT}/scripts/${LABEL}.plist.template"
RUNNER="${ROOT}/scripts/run-monthly-reconcile.sh"
LOGDIR="${ROOT}/logs"

PATH_VALUE="/opt/homebrew/bin:${HOME}/.local/bin:${HOME}/.nvm/versions/node/v22.22.1/bin:${HOME}/.pyenv/shims:/usr/bin:/bin"

if [ ! -f "${TEMPLATE}" ]; then
  echo "模板不存在：${TEMPLATE}" >&2
  exit 1
fi

if [ ! -f "${ROOT}/.env" ]; then
  echo "${ROOT}/.env 不存在 —— 任务能装上，但跑起来会因为缺凭证失败。" >&2
  exit 1
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
echo "每月 3 号 10:00（机器本地时间）结算上个月，写进 Commission Summary 并通知管理员。"
echo "同一个月跑第二次不会重复写 —— 汇总表里已有该月数据时会跳过，照样发通知。"
echo "日志：${LOGDIR}/monthly-reconcile-stdout.log 与 monthly-reconcile-stderr.log"
echo ""
echo "先手工验一次（只算不写不发）："
echo "  ${RUNNER} --dry-run"
echo ""
echo "回滚：launchctl bootout ${DOMAIN}/${LABEL}"
