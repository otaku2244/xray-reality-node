#!/usr/bin/env bash
# VLESS + REALITY 节点一键部署（Debian / Ubuntu，root 执行）
#
# 用法：
#   REALITY_DEST=www.amazon.com ./deploy.sh
#
# 环境变量：
#   REALITY_DEST  伪装目标站点，必须支持 TLS1.3 + h2，默认 www.amazon.com
#   REALITY_PORT  监听端口，默认 443
#   NODE_NAME     客户端显示名，默认 reality-node
set -euo pipefail

DEST="${REALITY_DEST:-www.amazon.com}"
PORT="${REALITY_PORT:-443}"
NODE_NAME="${NODE_NAME:-reality-node}"

CFG_DIR=/usr/local/etc/xray
CFG="$CFG_DIR/config.json"
SVC=/etc/systemd/system/xray.service
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_SRC="$(cd "$HERE/.." && pwd)"

c() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "请用 root 运行"; exit 1; }

# ---------- 1. 安装 xray ----------
c "检查 Xray"
if command -v xray >/dev/null 2>&1; then
  echo "已安装：$(xray version | head -1)"
else
  echo "未安装，执行官方安装脚本..."
  bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
fi

# ---------- 2. 端口占用检查 ----------
c "检查端口 $PORT"
if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
  HOLDER=$(ss -tlnp 2>/dev/null | grep ":$PORT " | grep -oP 'users:\(\("\K[^"]+' | head -1)
  if [[ "$HOLDER" != "xray" ]]; then
    warn "端口 $PORT 已被 $HOLDER 占用"
    warn "REALITY 需要独占 $PORT。请先停掉它，或用 REALITY_PORT 换一个端口"
    exit 1
  fi
  echo "已被 xray 自身占用，将覆盖配置后重启"
fi

# ---------- 3. 生成密钥与标识 ----------
c "生成随机参数"
UUID=$(xray uuid)
KEYOUT=$(xray x25519)
# Xray 不同版本输出关键字不同：PrivateKey/PublicKey 或 Private key/Password
PRIV=$(printf '%s\n' "$KEYOUT" | awk -F': *' 'tolower($1) ~ /private/ {print $2; exit}')
PUB=$(printf '%s\n' "$KEYOUT" | awk -F': *' 'tolower($1) ~ /public|password/ {print $2; exit}')
SID=$(openssl rand -hex 8)

if [[ -z "$PRIV" || -z "$PUB" ]]; then
  warn "无法解析 x25519 输出："
  printf '%s\n' "$KEYOUT"
  exit 1
fi
echo "UUID    = $UUID"
echo "shortId = $SID"
echo "公钥    = $PUB"

# ---------- 4. 渲染配置 ----------
c "渲染配置"
mkdir -p "$CFG_DIR"
if [[ -f "$CFG" ]]; then
  BAK="$CFG.bak-$(date +%Y%m%d%H%M%S)"
  cp -f "$CFG" "$BAK"
  echo "旧配置已备份：$BAK"
fi

UUID="$UUID" PRIV="$PRIV" SID="$SID" DEST="$DEST" PORT="$PORT" \
  python3 "$ROOT_SRC/scripts/render_config.py" \
  "$ROOT_SRC/config/config.json.example" "$CFG"

c "校验配置"
xray run -test -config "$CFG" || { warn "配置校验失败"; exit 1; }

# ---------- 5. 安装服务 ----------
c "安装 systemd 服务"
install -m 0644 "$ROOT_SRC/deploy/xray.service" "$SVC"
systemctl daemon-reload
systemctl enable xray >/dev/null 2>&1 || true
systemctl restart xray
sleep 2
systemctl is-active xray

c "运行状态"
systemctl show xray -p ExecMainStartTimestamp -p NRestarts | tr '\n' ' '
echo
ss -tlnp 2>/dev/null | grep ":$PORT " || warn "端口 $PORT 未监听，检查 journalctl -u xray -n 50"

# ---------- 6. 输出客户端链接 ----------
IP=$(curl -s -m 10 https://api.ipify.org || echo "<你的服务器IP>")
LINK="vless://${UUID}@${IP}:${PORT}?encryption=none&security=reality&sni=${DEST}&fp=chrome&pbk=${PUB}&sid=${SID}&type=tcp&flow=xtls-rprx-vision#${NODE_NAME}"

c "完成"
cat <<EOF
客户端链接（可直接导入 v2rayN / v2rayNG / sing-box / mihomo）：

$LINK

参数备忘：
  UUID       $UUID
  公钥 pbk   $PUB
  shortId    $SID
  SNI        $DEST
  flow       xtls-rprx-vision
  端口       $PORT

下一步：验证
  1) 体检伪装目标（换过 REALITY_DEST 时一定要跑，不合格的 dest 会让伪装形同虚设）
       python3 $ROOT_SRC/scripts/dest_check.py $DEST
  2) 端到端自检（不依赖你的手机）
       python3 $ROOT_SRC/scripts/reality_selftest.py
  3) 需要长期盯住 dest 有没有被套 CDN，装每小时巡检
       $ROOT_SRC/scripts/install-guard.sh

注意：私钥只存在于 $CFG，不要提交到任何仓库，不要发给任何人。
EOF
