#!/bin/bash
# 每月结算的跑批入口。launchd 调它；人也可以直接手工跑一次看结果。
#
#   ./scripts/run-monthly-reconcile.sh            # 真写 + 通知（launchd 跑的就是这条）
#   ./scripts/run-monthly-reconcile.sh --dry-run  # 只算不写不发
#
# 默认就带 --apply：这个脚本存在的意义就是每月自动结算，跑起来什么都不做没有意义。
# 要预演传 --dry-run，它会把 --apply 摘掉。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}" || exit 1

PY="${ROOT}/.venv/bin/python"

APPLY="--apply"
PASS=()
for arg in "$@"; do
  if [ "${arg}" = "--dry-run" ]; then
    APPLY=""
    continue
  fi
  PASS+=("${arg}")
done

run_it() {
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

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') 月度结算开始 ==="
run_it scripts/monthly_reconcile.py ${APPLY} ${PASS[@]+"${PASS[@]}"}
STATUS=$?
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') 结束（退出码 ${STATUS}）==="
exit "${STATUS}"
