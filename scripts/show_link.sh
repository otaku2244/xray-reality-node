#!/usr/bin/env bash
# 从现有配置反推客户端链接（轮换密钥、换客户端时用）
#
# 用法：./show_link.sh [服务器IP或域名]
# 不传 IP 时自动取本机公网出口地址。
set -euo pipefail

CFG="${XRAY_CFG:-/usr/local/etc/xray/config.json}"
NODE_NAME="${NODE_NAME:-reality-node}"

[[ -f "$CFG" ]] || { echo "配置不存在: $CFG"; exit 1; }

read -r UUID DEST PRIV SID PORT < <(python3 - "$CFG" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
ib = d["inbounds"][0]
rs = ib["streamSettings"]["realitySettings"]
u = ib["settings"]["clients"][0]
print(u["id"], rs["serverNames"][0], rs["privateKey"], rs["shortIds"][0], ib["port"])
PY
)

# 公钥由私钥推导，不额外保存
PUB=$(xray x25519 -i "$PRIV" | awk -F': *' 'tolower($1) ~ /public|password/ {print $2; exit}')
[[ -n "$PUB" ]] || { echo "无法推导公钥"; exit 1; }

IP="${1:-}"
if [[ -z "$IP" ]]; then
  IP=$(curl -s -m 10 https://api.ipify.org || true)
  [[ -n "$IP" ]] || { echo "取不到公网 IP，请手动传入"; exit 1; }
fi

echo "vless://${UUID}@${IP}:${PORT}?encryption=none&security=reality&sni=${DEST}&fp=chrome&pbk=${PUB}&sid=${SID}&type=tcp&flow=xtls-rprx-vision#${NODE_NAME}"
echo
printf 'UUID     %s\n公钥     %s\nshortId  %s\nSNI      %s\n端口     %s\n' \
  "$UUID" "$PUB" "$SID" "$DEST" "$PORT"
