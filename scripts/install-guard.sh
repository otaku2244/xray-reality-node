#!/usr/bin/env bash
# 安装 / 卸载 dest 巡检（每小时一次，只读，不碰 xray 配置）
#
# 安装：./scripts/install-guard.sh
# 卸载：./scripts/install-guard.sh --remove
#
# 想改巡检频率，直接编辑 /etc/systemd/system/dest-guard.timer 里的 OnCalendar，
# 然后 systemctl daemon-reload && systemctl restart dest-guard.timer
set -euo pipefail

LIB=/usr/local/lib/xray-guard
UNIT=/etc/systemd/system
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_SRC="$(cd "$HERE/.." && pwd)"

c() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "请用 root 运行"; exit 1; }

if [[ "${1:-}" == "--remove" ]]; then
  c "卸载巡检"
  systemctl disable --now dest-guard.timer 2>/dev/null || true
  rm -f "$UNIT/dest-guard.service" "$UNIT/dest-guard.timer"
  systemctl daemon-reload
  rm -rf "$LIB"
  warn "已移除。状态文件保留在 /var/lib/dest-guard，需要的话手动删"
  exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "缺少 python3"; exit 1; }
command -v openssl >/dev/null 2>&1 || warn "缺少 openssl，体检里的 TLS 项会失败（巡检不受影响）"

c "安装脚本到 $LIB"
mkdir -p "$LIB"
install -m 0755 "$ROOT_SRC/scripts/dest_guard.py" "$LIB/dest_guard.py"
install -m 0755 "$ROOT_SRC/scripts/dest_check.py" "$LIB/dest_check.py"
echo "$(ls -1 "$LIB")"

c "安装 systemd 单元"
install -m 0644 "$ROOT_SRC/deploy/dest-guard.service" "$UNIT/dest-guard.service"
install -m 0644 "$ROOT_SRC/deploy/dest-guard.timer" "$UNIT/dest-guard.timer"
systemctl daemon-reload

c "启用定时器"
systemctl enable --now dest-guard.timer
systemctl list-timers dest-guard.timer --no-pager | head -3

c "立刻跑一次"
"$LIB/dest_guard.py" || warn "巡检返回非 0，见上面的 ALERT / NOTE"
