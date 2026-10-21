#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 config.json.example 渲染成实际配置。

占位符替换 + JSON 合法性校验，避免手改配置时漏掉某个字段。
参数通过环境变量传入，避免密钥出现在进程命令行里（ps 可见）。
"""
import json
import os
import sys

FRESH_KEYS = ("UUID", "PRIV", "SID", "DEST", "PORT")


def main():
    if len(sys.argv) != 3:
        print("usage: render_config.py <template> <output>", file=sys.stderr)
        return 2

    tpl_path, out_path = sys.argv[1], sys.argv[2]
    miss = [k for k in FRESH_KEYS if not os.environ.get(k)]
    if miss:
        print("缺少环境变量: " + ", ".join(miss), file=sys.stderr)
        return 2

    with open(tpl_path, "r", encoding="utf-8") as fh:
        text = fh.read()

    repl = {
        "__REPLACE_WITH_UUID__": os.environ["UUID"],
        "__REPLACE_WITH_PRIVATE_KEY__": os.environ["PRIV"],
        "__REPLACE_WITH_SHORT_ID__": os.environ["SID"],
        "__REPLACE_WITH_DEST__": os.environ["DEST"],
    }
    for k, v in repl.items():
        text = text.replace(k, v)

    # 端口可改（默认模板里写的是 443）
    cfg = json.loads(text)
    cfg["inbounds"][0]["port"] = int(os.environ["PORT"])

    if "__REPLACE_WITH_" in json.dumps(cfg):
        print("模板里还有未替换的占位符", file=sys.stderr)
        return 1

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    # 回读校验，防止写出半截文件
    with open(out_path, "r", encoding="utf-8") as fh:
        json.load(fh)

    print("[render] 已生成 %s（port=%s dest=%s）" % (out_path, os.environ["PORT"], os.environ["DEST"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
