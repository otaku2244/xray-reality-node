#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REALITY 伪装目标（dest）质量体检。

对一个或多个候选域名做六项检查，给出 PASS / WARN / FAIL：

  1. DNS 解析与 ASN 归属
  2. TLS 1.3 是否可用
  3. 密钥交换是否为 X25519
  4. ALPN 是否协商出 h2
  5. 证书是否匹配该 SNI、是否临近过期
  6. TCP 握手延迟

用法：
  ./dest_check.py                      # 不带参数：读当前配置里的 serverNames
  ./dest_check.py www.amazon.com
  ./dest_check.py a.com b.com --json

只读脚本，不修改任何配置。退出码：0 全部 PASS，1 存在 FAIL，2 存在 WARN。
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

DEFAULT_CFG = os.environ.get("XRAY_CFG", "/usr/local/etc/xray/config.json")

# ASN -> (名称, 风险级别)。high 表示透明 CDN，dest 落在上面等于给别人当反代
CDN = {
    "AS13335": ("Cloudflare", "high"),
    "AS209242": ("Cloudflare", "high"),
    "AS54113": ("Fastly", "mid"),
    "AS16625": ("Akamai", "mid"),
    "AS20940": ("Akamai", "mid"),
    "AS16509": ("Amazon CloudFront", "low"),
    "AS15169": ("Google", "low"),
}

HIGH_RTT_MS = 150.0
CERT_FAIL_DAYS = 7
CERT_WARN_DAYS = 30


def http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "dest-check/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def dns_a(host):
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return [], str(exc)
    return sorted({i[4][0] for i in infos}), ""


def asn_of(ip):
    try:
        d = http_json("https://ipinfo.io/%s/json" % ip)
        org = d.get("org") or ""
        return {
            "asn": org.split()[0] if org.startswith("AS") else "",
            "org": org,
            "country": d.get("country", ""),
            "city": d.get("city", ""),
        }
    except Exception:
        pass
    try:
        d = http_json("https://api.ip.sb/geoip/%s" % ip)
        return {
            "asn": "AS%s" % d.get("asn", ""),
            "org": d.get("asn_organization", ""),
            "country": d.get("country", ""),
            "city": d.get("city", ""),
        }
    except Exception as exc:
        return {"asn": "", "org": "", "country": "", "city": "", "error": str(exc)}


def cert_fields(pem):
    out = {}
    try:
        p = subprocess.run(
            ["openssl", "x509", "-noout", "-issuer", "-subject", "-enddate", "-text"],
            input=pem.encode(), capture_output=True, timeout=15,
        )
    except Exception as exc:
        return {"error": str(exc)}
    txt = p.stdout.decode("utf-8", "replace")
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("issuer="):
            out["issuer"] = line[7:]
        elif line.startswith("subject="):
            out["subject"] = line[8:]
        elif line.startswith("notAfter="):
            out["not_after"] = line[9:]
    out["san"] = sorted(set(re.findall(r"DNS:([A-Za-z0-9._*-]+)", txt)))
    for key in ("issuer", "subject"):
        cn = re.search(r"CN\s*=\s*([^,]+)", out.get(key, ""))
        out[key + "_cn"] = cn.group(1).strip() if cn else ""
    return out


def days_left(not_after):
    try:
        dt = datetime.strptime(" ".join(not_after.split()), "%b %d %H:%M:%S %Y %Z")
        dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400.0


def san_match(san, sni):
    for entry in san:
        if entry == sni:
            return True
        if entry.startswith("*.") and sni.split(".", 1)[-1] == entry[2:]:
            return True
    return False


def tls_probe(ip, sni, timeout=20):
    cmd = ["openssl", "s_client", "-connect", "%s:443" % ip, "-servername", sni,
           "-alpn", "h2", "-tls1_3"]
    try:
        p = subprocess.run(cmd, input=b"", capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "握手超时"}
    txt = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    res = {
        "ok": True,
        "alpn_h2": "ALPN protocol: h2" in txt,
        "verify_ok": "Verify return code: 0" in txt,
    }
    # OpenSSL 3.x 的 s_client 不再单独打印 Protocol / Cipher 行，
    # 协议与套件只出现在 "New, TLSv1.3, Cipher is ..." 这一行里
    m = re.search(r"New,\s*(TLSv[0-9.]+),\s*Cipher is\s+(\S+)", txt)
    if m:
        res["protocol"], res["cipher"] = m.group(1), m.group(2)
    else:
        m = re.search(r"Protocol version:\s*(TLSv[0-9.]+)", txt)
        res["protocol"] = m.group(1) if m else ""
        m = re.search(r"Ciphersuite:\s*(\S+)", txt)
        res["cipher"] = m.group(1) if m else ""
    res["tls13"] = res["protocol"] == "TLSv1.3" or "Protocol version: TLSv1.3" in txt
    m = re.search(r"Server Temp Key:\s*([A-Za-z0-9-]+)", txt)
    res["kex"] = m.group(1) if m else ""
    cert = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", txt, re.S)
    res["cert"] = cert_fields(cert.group(0)) if cert else {}
    return res


def tcp_rtt(ip, times=3):
    vals = []
    for _ in range(times):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        start = time.time()
        try:
            sock.connect((ip, 443))
            vals.append((time.time() - start) * 1000.0)
        except Exception:
            pass
        finally:
            sock.close()
    return min(vals) if vals else None


def check(host):
    fails, warns = [], []
    r = {"dest": host, "fails": fails, "warns": warns}

    ips, err = dns_a(host)
    r["ips"] = ips
    if not ips:
        fails.append("DNS 无 A 记录（%s）" % (err or "空结果"))
        r["verdict"] = "FAIL"
        return r

    asns = []
    r["addr"] = []
    for ip in ips:
        info = asn_of(ip)
        asns.append(info.get("asn", ""))
        cdn = CDN.get(info.get("asn", ""))
        info["cdn"] = "%s/%s" % cdn if cdn else ""
        r["addr"].append(dict(info, ip=ip))
        time.sleep(0.2)

    if len({a for a in asns if a}) > 1:
        warns.append("多个 IP 归属不同 ASN，边缘节点漂移，需留意")

    high = [c for c in r["addr"] if c.get("cdn", "").endswith("/high")]
    if high:
        fails.append("dest 落在 Cloudflare 等透明 CDN 上（%s），未认证流量会被当免费反代"
                     % ", ".join(c["ip"] for c in high))
    mid = [c for c in r["addr"] if c.get("cdn", "").endswith("/mid")]
    if mid:
        warns.append("dest 挂在第三方 CDN（%s），可接受但需注意流量被转发" % mid[0]["cdn"])

    primary = ips[0]
    tls = tls_probe(primary, host)
    r["tls"] = tls
    if not tls.get("ok"):
        fails.append("TLS 握手失败：%s" % tls.get("error", "未知"))
    else:
        if not tls["tls13"]:
            fails.append("未协商出 TLS 1.3（REALITY 硬性要求）")
        if not tls["alpn_h2"]:
            fails.append("ALPN 未协商出 h2")
        if not tls["verify_ok"]:
            warns.append("证书链验证未通过（自签或缺中间证书）")
        if tls["kex"] and tls["kex"].upper() != "X25519":
            warns.append("密钥交换为 %s，非 X25519" % tls["kex"])
        cert = tls.get("cert") or {}
        if cert.get("san"):
            if not san_match(cert["san"], host):
                fails.append("证书 SAN 不包含该 SNI，握手会露馅")
        left = days_left(cert["not_after"]) if cert.get("not_after") else None
        r["cert_days_left"] = left
        if left is not None:
            if left < CERT_FAIL_DAYS:
                fails.append("证书 %d 天后过期" % int(left))
            elif left < CERT_WARN_DAYS:
                warns.append("证书 %d 天后过期，注意 dest 可能换证" % int(left))

    rtt = tcp_rtt(primary)
    r["rtt_ms"] = rtt
    if rtt is None:
        fails.append("443 端口 TCP 握手失败")
    elif rtt > HIGH_RTT_MS:
        warns.append("TCP 延迟 %.0f ms 偏高，握手会慢" % rtt)

    r["verdict"] = "FAIL" if fails else ("WARN" if warns else "PASS")
    return r


def render(r):
    print("== %s ==" % r["dest"])
    if not r.get("ips"):
        print("  判定        FAIL   %s" % "；".join(r["fails"]))
        return
    for a in r.get("addr", []):
        print("  %-11s %-10s %s %s/%s" % (
            a["ip"], a.get("asn") or "?", (a.get("org") or "")[:38],
            a.get("country") or "?", a.get("city") or "?", ))
    tls = r.get("tls") or {}
    print("  TLS         %s  %s  密钥交换 %s" % (
        tls.get("protocol") or "-", tls.get("cipher") or "-", tls.get("kex") or "-"))
    cert = (tls.get("cert") or {})
    left = r.get("cert_days_left")
    print("  证书        %s  issuer=%s%s" % (
        cert.get("subject_cn") or cert.get("subject") or "-",
        cert.get("issuer_cn") or cert.get("issuer") or "-",
        "" if left is None else "  剩余 %d 天" % int(left)))
    print("  延迟        %s" % ("-" if r.get("rtt_ms") is None else "%.1f ms" % r["rtt_ms"]))
    for f in r["fails"]:
        print("  FAIL        %s" % f)
    for w in r["warns"]:
        print("  WARN        %s" % w)
    print("  判定        %s" % r["verdict"])


def dest_from_cfg(path):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    names = cfg["inbounds"][0]["streamSettings"]["realitySettings"]["serverNames"]
    return names[0] if names else None


def main():
    ap = argparse.ArgumentParser(description="REALITY dest 质量体检")
    ap.add_argument("domains", nargs="*", help="候选域名，留空则读配置里的 serverNames[0]")
    ap.add_argument("--cfg", default=DEFAULT_CFG, help="xray 配置路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    targets = list(args.domains)
    if not targets:
        try:
            one = dest_from_cfg(args.cfg)
        except Exception as exc:
            sys.exit("读取 %s 失败：%s" % (args.cfg, exc))
        if not one:
            sys.exit("配置里没有 serverNames，请显式传域名")
        targets = [one]

    results = [check(t) for t in targets]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            render(r)
            print()

    if any(r["verdict"] == "FAIL" for r in results):
        return 1
    if any(r["verdict"] == "WARN" for r in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
