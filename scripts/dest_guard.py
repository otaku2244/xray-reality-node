#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REALITY dest 巡检：确认伪装目标没有被套进第三方 CDN。

要防的场景：dest 这个域名本来直连，某天它的 DNS 被换成 Cloudflare / Fastly 这类
透明 CDN 的前置节点。此后所有未通过 REALITY 认证的流量（扫描器、爬虫、误连的
浏览器）都会被你的服务器原样转发出去，等于你在给 CDN 做免费反代，付流量费的是你。

本脚本只读：解析 DNS、查 ASN、比对 CDN 名单、记录状态，不碰配置也不重启服务。
周期执行交给 dest-guard.timer（每小时）。

用法：
  ./dest_guard.py                # 读当前配置里的 serverNames[0]
  ./dest_guard.py --dest www.amazon.com
  ./dest_guard.py --json

退出码：0 正常，1 命中风险 CDN 或解析失败，2 无法判定（缺少依赖/配置）。
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.request

DEFAULT_CFG = os.environ.get("XRAY_CFG", "/usr/local/etc/xray/config.json")
STATE_DIR = os.environ.get("DEST_GUARD_STATE", "/var/lib/dest-guard")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

CDN_HIGH = {
    "AS13335": "Cloudflare",
    "AS209242": "Cloudflare (London)",
}
CDN_MID = {
    "AS54113": "Fastly",
    "AS16625": "Akamai",
    "AS20940": "Akamai",
}


def http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "dest-guard/1.0"})
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
        return org.split()[0] if org.startswith("AS") else "", org
    except Exception:
        pass
    try:
        d = http_json("https://api.ip.sb/geoip/%s" % ip)
        return "AS%s" % d.get("asn", ""), d.get("asn_organization", "")
    except Exception:
        return "", ""


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print("WARN 状态写入失败：%s" % exc)


def notify(text):
    token = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        return
    try:
        url = "https://api.telegram.org/bot%s/sendMessage" % token
        body = json.dumps({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:
        print("WARN Telegram 推送失败：%s" % exc)


def dest_from_cfg(path):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    names = cfg["inbounds"][0]["streamSettings"]["realitySettings"]["serverNames"]
    return names[0] if names else None


def main():
    ap = argparse.ArgumentParser(description="REALITY dest 巡检")
    ap.add_argument("--dest", help="伪装域名，留空则读配置")
    ap.add_argument("--cfg", default=DEFAULT_CFG)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    dest = args.dest
    if not dest:
        try:
            dest = dest_from_cfg(args.cfg)
        except Exception as exc:
            print("ERROR 读取配置失败：%s" % exc)
            return 2
    if not dest:
        print("ERROR 无法确定 dest")
        return 2

    ips, err = dns_a(dest)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if not ips:
        msg = "%s ERROR dest=%s 解析失败：%s" % (stamp, dest, err or "空结果")
        print(msg)
        notify("[REALITY] dest 解析失败 %s：%s" % (dest, err or "空结果"))
        return 1

    findings, high_hits, mid_hits = [], [], []
    for ip in ips:
        asn, org = asn_of(ip)
        time.sleep(0.2)
        findings.append({"ip": ip, "asn": asn, "org": org})
        if asn in CDN_HIGH:
            high_hits.append((ip, asn, CDN_HIGH[asn]))
        elif asn in CDN_MID:
            mid_hits.append((ip, asn, CDN_MID[asn]))

    state = load_state()
    prev = set(state.get(dest, {}).get("ips") or [])
    cur = set(ips)
    drifted = bool(prev) and prev != cur

    state[dest] = {"ips": ips, "checked_at": stamp,
                   "asn": [f["asn"] for f in findings]}
    save_state(state)

    result = {
        "dest": dest,
        "ips": ips,
        "findings": findings,
        "checked_at": stamp,
        "drifted": drifted,
        "prev_ips": sorted(prev),
        "high": [{"ip": i, "asn": a, "cdn": n} for i, a, n in high_hits],
        "mid": [{"ip": i, "asn": a, "cdn": n} for i, a, n in mid_hits],
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("%s dest=%s ips=%s" % (stamp, dest, ",".join(ips)))
        for f in findings:
            print("  %-15s %-9s %s" % (f["ip"], f["asn"] or "?", f["org"][:44]))
        if drifted:
            print("  NOTE IP 发生变化：%s -> %s（CDN 边缘调度属正常，但需确认不是被接管）"
                  % (",".join(sorted(prev)), ",".join(ips)))
        for i, a, n in high_hits:
            print("  ALERT %s 落在 %s(%s) 上，dest 已被透明 CDN 接管" % (i, n, a))
        for i, a, n in mid_hits:
            print("  NOTE  %s 落在 %s(%s) 上，未认证流量会经你服务器转发" % (i, n, a))

    if high_hits:
        notify("[REALITY] %s 的 IP 已落在 %s 上，建议更换 dest" %
               (dest, ", ".join(n for _, _, n in high_hits)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
