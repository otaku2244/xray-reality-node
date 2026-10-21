# xray-reality-node

一台自建代理节点：**VLESS + XTLS-Vision + REALITY**，单端口 443，不需要自己的域名，也不需要签发证书。

配置、部署脚本、服务单元、自检工具四件套，从一台裸 Linux 到可用的节点约五分钟。

```
客户端 ──TLS1.3(SNI=www.amazon.com)──> 本机:443 ──REALITY 认证──> 出站 freedom
                │
                └─ 未通过认证的流量：原样转发给真实的 www.amazon.com，
                   审查者看到的就是一次正常的亚马逊访问
```

---

## 为什么用 REALITY

传统 TLS 代理有两个硬伤：

| 方案 | 问题 |
|---|---|
| VMess/VLESS + 自签证书 | 证书是自签的，主动探测一查就露，指纹也不对 |
| TLS + 真域名 + CDN 套壳 | 要买域名、要维护证书、CDN 还会看到你的全部流量 |

REALITY 换了个思路：**它不自己造 TLS，而是借用目标站点的真实 TLS 握手**。

- 客户端连过来时，握手走的是真实 `www.amazon.com` 的证书链，指纹、ALPN、TLS 版本全对
- 认证信息藏在 ClientHello 的密钥交换里（用服务端公钥加密的临时密钥）
- 认证通过 → 代理开始转发；认证失败 → 流量直接落到真实的亚马逊服务器
- 服务端**不持有任何证书**，也就没有证书可被吊销、可被比对

结果是：这个端口在扫描器眼里就是一个普通的 HTTPS 站点，而且是被广泛访问的大站。

---

## 部署

### 前提

- 一台能连外网的 Linux（Debian 12 / Ubuntu 22.04+ 验证过）
- root 权限
- 443 端口空闲

### 一键

```bash
git clone git@github.com:<you>/xray-reality-node.git
cd xray-reality-node

# 伪装站点默认 www.amazon.com，可换
REALITY_DEST=www.amazon.com ./scripts/deploy.sh
```

脚本会依次做：

1. 装 Xray（调用官方 `install-release.sh`，已装则跳过）
2. 检查 443 是否被别的进程占用
3. 生成 UUID、x25519 密钥对、16 字节 shortId
4. 用 `config/config.json.example` 渲染出 `/usr/local/etc/xray/config.json`（旧配置自动备份）
5. `xray run -test` 校验，不通过就停
6. 装 systemd 单元、开机自启、立刻启动
7. 打印可直接导入客户端的 `vless://` 链接

### 手工

```bash
cp config/config.json.example /usr/local/etc/xray/config.json
# 替换四处占位符：UUID / dest / privateKey / shortIds
xray x25519          # 生成密钥对
xray uuid            # 生成 UUID
openssl rand -hex 8  # 生成 shortId
xray run -test -config /usr/local/etc/xray/config.json
install -m 0644 deploy/xray.service /etc/systemd/system/xray.service
systemctl daemon-reload && systemctl enable --now xray
```

### 参数

`deploy.sh` 接受的环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `REALITY_DEST` | `www.amazon.com` | 伪装目标，必须支持 TLS1.3 + h2，且不在国内被墙 |
| `REALITY_PORT` | `443` | 监听端口。REALITY 建议用 443，换端口会降低伪装可信度 |
| `NODE_NAME` | `reality-node` | 客户端里显示的节点名 |

---

## 客户端

`deploy.sh` 末尾打印的链接直接粘贴即可：

```
vless://<uuid>@<ip>:443?encryption=none&security=reality&sni=www.amazon.com&fp=chrome&pbk=<pubkey>&sid=<shortid>&type=tcp&flow=xtls-rprx-vision#reality-node
```

| 客户端 | 用法 |
|---|---|
| v2rayN / v2rayNG | 从剪贴板导入 |
| sing-box | `outbounds` 里 `type: vless`，`tls.reality` 段填 `public_key` / `short_id` |
| mihomo / Clash.Meta | `proxies` 里 `type: vless`，加 `reality-opts` |
| 命令行 | `scripts/show_link.sh` 从现有配置反推链接 |

**关键参数说明**：

- `flow=xtls-rprx-vision`：必须和客户端一致，否则连上但不通。它省掉一层 TLS 记录封装，降低特征
- `fp=chrome`：客户端指纹，建议 chrome 或 safari，与服务端伪装站点匹配
- `sid`：shortId 是 0–16 位十六进制字符串，留空也可以，但填上能减少被动特征

---

## 自检

不用手机，在服务器本机上就能验证链路是否真的通：

```bash
python3 scripts/reality_selftest.py
```

原理：读服务端配置 → 由私钥反推公钥 → 在 127.0.0.1 起一个临时 xray 客户端（socks5 → REALITY → 本机 443）→ 用 curl 穿过它访问 `cloudflare.com/cdn-cgi/trace` → 比对出口 IP → 清理临时进程。

通过的样子：

```
[配置] 端口=443 SNI=www.amazon.com sid=xxxxxxxx flow=xtls-rprx-vision
[配置] 公钥=xxxxxxxx
[启动] 临时客户端 pid=12345，等待就绪...
[测试1] 隧道内访问 cloudflare/cdn-cgi/trace  退出码=0
        ip=1.2.3.4
        loc=HK
        colo=HKG
[测试2] 本机直连出口 ip=1.2.3.4
[清理] 临时客户端与配置已移除
```

`ip=` 与直连出口一致，说明流量确实绕了一圈又出来，转发正常。

可配置项：`XRAY_CFG`、`SELFTEST_CFG`、`SELFTEST_SOCKS_PORT`、`XRAY_BIN`。

---

## 密钥轮换

怀疑泄露时，重新生成全套参数（客户端需要重新导入）：

```bash
./scripts/deploy.sh          # 会重新生成 UUID/密钥/shortId，旧配置自动备份
```

只换 UUID（保留密钥、仅踢掉某个客户端）：

```bash
NEW=$(xray uuid)
python3 - "$NEW" <<'PY'
import json, sys
p = "/usr/local/etc/xray/config.json"
d = json.load(open(p))
d["inbounds"][0]["settings"]["clients"][0]["id"] = sys.argv[1]
json.dump(d, open(p, "w"), indent=2)
PY
systemctl restart xray
```

**私钥在 `/usr/local/etc/xray/config.json` 里，只有它能解密客户端握手。泄露 = 别人可以搭一个同 SNI 的假节点做中间人。不要提交，不要外发。**

---

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 服务起不来，日志 `address already in use` | 443 被 nginx/caddy 占用 | `ss -tlnp \| grep :443` 找出占用者，REALITY 需要独占 |
| 客户端连上但打不开网页 | `flow` 两端不一致 | 服务端 `xtls-rprx-vision`，客户端也必须填 |
| 客户端握手失败 / 一直重连 | `pbk` 或 `sid` 填错 | 用 `scripts/show_link.sh` 重新生成链接，别手抄 |
| 偶发超时，换个时段又正常 | 伪装站点不可达或响应慢 | 换 `REALITY_DEST`，优先选 CDN 大站 |
| `REALITY: processed invalid connection` | 扫描器或指纹不匹配的客户端 | 正常现象，说明拦截生效 |
| 自检 `退出码!=0` 且无返回 | 出站被机房限制，或 DNS 不通 | `curl -v https://www.cloudflare.com` 先确认本机能出网 |

---

## 安全边界

- 服务以 `root` 运行并监听 443，这是官方模板的默认做法。要降权需要 `CAP_NET_BIND_SERVICE`，会牺牲一部分兼容性
- 日志级别默认 `warning`，不记录访问内容。调 `debug` 只在排错时临时开，用完改回
- 节点本身不做分流，`outbounds` 只有 `freedom` 和 `blackhole`。需要分流规则请在客户端侧加
- **这个仓库不含任何真实密钥**。`config.json.example` 全是占位符，真实配置只存在于服务器本地

## 局限

- 只支持 TCP。REALITY 目前不支持 QUIC/Hysteria 那套 UDP 方案，弱网下抗丢包不如它们
- 单节点单用户模型。多用户要在 `clients` 数组里追加，各自独立 UUID
- 伪装站点是单点依赖：如果 `www.amazon.com` 在你的线路上不可达，客户端握手会直接失败
