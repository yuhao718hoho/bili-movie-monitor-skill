#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时任务入口：先查新投稿，再推进一小段历史回扫，只输出摘要行以节省 token。"""

import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
BACKFILL_BATCHES = "8"


def run(args):
    p = subprocess.run([PY, os.path.join(BASE, "monitor.py")] + args,
                       cwd=BASE, capture_output=True, text=True)
    return (p.stdout or "").strip(), p.returncode


def brief(text):
    """只保留首行摘要与失败行，其余明细已写入 logs/。"""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return "无输出"
    keep = [lines[0]]
    for l in lines[1:]:
        if "｜" in l or "失败" in l or "中止" in l or "中断" in l or "未登录" in l:
            keep.append(l)
    return "\n".join(keep)


def main():
    out1, rc1 = run([])
    print(brief(out1))

    # 新投稿处理被限流时不再追加回扫，避免白跑
    if rc1 != 0:
        return rc1

    out2, rc2 = run(["--backfill", BACKFILL_BATCHES])
    if out2:
        b = brief(out2)
        if "没有新的投稿" not in b:
            print(b)
    return 0


if __name__ == "__main__":
    sys.exit(main())
