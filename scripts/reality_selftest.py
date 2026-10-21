#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REALITY 端到端自检。

在 VPS 本机起一个临时 xray 客户端（socks5 -> REALITY -> 127.0.0.1:443），
用 curl 穿过它访问外网，验证服务端认证与转发链路是否真正可用。
测试完成后自动清理临时进程与文件，不改动线上配置。
"""
import json
import os
import subprocess
import sys
import time

SRV_CFG = os.environ.get("XRAY_CFG", "/usr/local/etc/xray/config.json")
TEST_CFG = os.environ.get("SELFTEST_CFG", "/tmp/.reality_selftest_client.json")
SOCKS_PORT = int(os.environ.get("SELFTEST_SOCKS_PORT", "10809"))
XRAY = os.environ.get("XRAY_BIN", "/usr/local/bin/xray")


def main():
    with open(SRV_CFG, "r", encoding="utf-8") as fh:
        srv = json.load(fh)

    ib = srv["inbounds"][0]
    rs = ib["streamSettings"]["realitySettings"]
    user = ib["settings"]["clients"][0]

    uuid = user["id"]
    flow = user.get("flow", "xtls-rprx-vision")
    sni = rs["serverNames"][0]
    sid = rs["shortIds"][0]
    port = ib["port"]

    # 由服务端私钥推导公钥
    out = subprocess.run([XRAY, "x25519", "-i", rs["privateKey"]],
                         capture_output=True, text=True, timeout=30)
    pub = None
    for line in out.stdout.splitlines():
        if "PublicKey" in line or "Password" in line:
            pub = line.split(":", 1)[1].strip()
    if not pub:
        print("!! 无法推导公钥")
        print(out.stdout, out.stderr)
        return 1

    print("[配置] 端口=%s SNI=%s sid=%s flow=%s" % (port, sni, sid, flow))
    print("[配置] 公钥=%s" % pub)

    client_cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "listen": "127.0.0.1",
            "port": SOCKS_PORT,
            "protocol": "socks",
            "settings": {"udp": False},
        }],
        "outbounds": [{
            "protocol": "vless",
            "settings": {"vnext": [{
                "address": "127.0.0.1",
                "port": port,
                "users": [{"id": uuid, "flow": flow, "encryption": "none"}],
            }]},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "serverName": sni,
                    "fingerprint": "chrome",
                    "publicKey": pub,
                    "shortId": sid,
                    "spiderX": "/",
                },
            },
        }],
    }

    with open(TEST_CFG, "w", encoding="utf-8") as fh:
        json.dump(client_cfg, fh, indent=2)

    proc = subprocess.Popen(
        [XRAY, "run", "-config", TEST_CFG],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("[启动] 临时客户端 pid=%d，等待就绪..." % proc.pid)
    time.sleep(3)

    if proc.poll() is not None:
        print("!! 临时客户端启动失败，退出码 %s" % proc.returncode)
        return 1

    rc = 0
    try:
        # 1. 穿过隧道取出口 IP
        r = subprocess.run(
            ["curl", "-s", "--max-time", "25", "--socks5-hostname",
             "127.0.0.1:%d" % SOCKS_PORT, "https://www.cloudflare.com/cdn-cgi/trace"],
            capture_output=True, text=True, timeout=40,
        )
        body = r.stdout or ""
        print("\n[测试1] 隧道内访问 cloudflare/cdn-cgi/trace  退出码=%d" % r.returncode)
        if body:
            for line in body.strip().splitlines():
                if line.startswith(("ip=", "loc=", "colo=")):
                    print("        " + line)
        else:
            print("        无返回")
            print("        stderr: " + (r.stderr or "").strip()[:300])
            rc = 1

        # 2. 对比直连出口 IP
        d = subprocess.run(
            ["curl", "-s", "--max-time", "20", "https://www.cloudflare.com/cdn-cgi/trace"],
            capture_output=True, text=True, timeout=35,
        )
        for line in (d.stdout or "").strip().splitlines():
            if line.startswith("ip="):
                print("[测试2] 本机直连出口 %s" % line)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        for path in (TEST_CFG,):
            try:
                os.remove(path)
            except OSError:
                pass
        print("[清理] 临时客户端与配置已移除")

    return rc


if __name__ == "__main__":
    sys.exit(main())
