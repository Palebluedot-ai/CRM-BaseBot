#!/bin/bash
# 每日增量导入的跑批入口。launchd 调它；人也可以直接手工跑一次看结果。
#
#   ./scripts/run-daily-import.sh              # 先试邮箱，失败退回本地目录
#   ./scripts/run-daily-import.sh --dry-run    # 参数原样透给 python
#
# 为什么会退回本地目录：抓邮件依赖 Graph（凭证、额度、网络、发件人是否发了），
# 而附件在本地往往已经有一份（另一条同步链路会落到同一个目录）。退回这一步让
# 「今天没数据」和「今天没抓到」不会混在一起 —— 而且退回时会在日志里明确写出来。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}" || exit 1

PY="${ROOT}/.venv/bin/python"

run_import() {
  if [ -x "${PY}" ]; then
    # 优先用仓库自带的 venv：launchd 的 PATH 很短，不该依赖 uv 在不在 PATH 里
    "${PY}" "$@"
    return $?
  fi
  if command -v uv >/dev/null 2>&1; then
    uv run python "$@"
    return $?
  fi
  echo "找不到可用的 python：${PY} 不存在，PATH 里也没有 uv" >&2
  return 127
}

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') 每日增量导入开始 ==="

if run_import scripts/import_daily_incremental.py --from-mail "$@"; then
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') 结束（走邮箱）==="
  exit 0
fi

echo "邮箱取件这一步失败，退回本地导出目录。" >&2

# 退回用哪个目录：命令行显式给的 --export-dir 优先，其次是 .env 的 DAILY_EXPORT_DIR，
# 最后是仓库里的 attachments/。人明确指了目录就听人的。
EXPORT_DIR="${DAILY_EXPORT_DIR:-attachments}"
prev=""
for arg in "$@"; do
  case "${arg}" in
    --export-dir=*) EXPORT_DIR="${arg#--export-dir=}" ;;
  esac
  if [ "${prev}" = "--export-dir" ]; then
    EXPORT_DIR="${arg}"
  fi
  prev="${arg}"
done
case "${EXPORT_DIR}" in
  /*) ;;
  *) EXPORT_DIR="${ROOT}/${EXPORT_DIR}" ;;
esac

COUNT=$(find "${EXPORT_DIR}" -maxdepth 1 -name 'OTC组销售明细_*.xlsx' 2>/dev/null | wc -l | tr -d ' ')
if [ "${COUNT}" = "0" ]; then
  echo "本地目录 ${EXPORT_DIR} 里也没有导出文件 —— 这次什么也没做。" >&2
  echo "  要么把 .env 的 MICROSOFT_GRAPH_* 配好，要么把 DAILY_EXPORT_DIR 指向放导出的目录。" >&2
  exit 1
fi

echo "退回本地目录 ${EXPORT_DIR}（${COUNT} 份导出）—— 这次用的是本地文件，不是邮件附件。" >&2
run_import scripts/import_daily_incremental.py --export-dir "${EXPORT_DIR}" "$@"
STATUS=$?
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') 结束（走本地文件，退出码 ${STATUS}）==="
exit "${STATUS}"
