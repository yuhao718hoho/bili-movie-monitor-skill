#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把豆瓣电影信息 CSV 压缩成本地匹配索引（片名 -> 评分）。"""

import csv
import json
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE, "data", "douban_movie_info.csv")
OUT_PATH = os.path.join(BASE, "data", "douban_index.json")


def norm(s):
    s = s or ""
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"[\s·・:：\-—_/\\#!！?？,，.。'\"“”‘’\[\]【】]+", "", s)
    return s.strip().lower()


def main():
    idx = {}
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = norm(row.get("movie_name"))
            if not key:
                continue
            try:
                score = float(row.get("score") or 0)
            except ValueError:
                continue
            if score <= 0:
                continue
            try:
                votes = int(float(row.get("score_amt") or 0))
            except ValueError:
                votes = 0
            old = idx.get(key)
            if old and old[1] >= votes:
                continue
            idx[key] = [round(score, 1), votes, (row.get("movie_name") or "").strip()]

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, separators=(",", ":"))
    print("索引条目 %d，文件 %.1f MB" % (len(idx), os.path.getsize(OUT_PATH) / 1024 / 1024))


if __name__ == "__main__":
    main()
