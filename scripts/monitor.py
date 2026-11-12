#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
哔哩哔哩电影（UP 主 mid=15773384）新投稿监控
流程：拉取 UP 主动态 -> 提取新投稿 -> 匹配豆瓣评分 -> 分数达标自动加入指定 B 站收藏夹

用法：
  python3 monitor.py --dry-run        # 只看结果，不写收藏夹
  python3 monitor.py --check          # 检查登录态与收藏夹
  python3 monitor.py                  # 正式执行
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
MID = 15773384
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
COOKIE_PATH = os.path.join(BASE, "bili_cookies.txt")
LOG_DIR = os.path.join(BASE, "logs")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULT_CONFIG = {
    "mid": 15773384,
    "up_name": "哔哩哔哩电影",
    "fav_folder": "于果暑假片单2026",
    "min_score": 7.8,
    "pages": 1,
    "min_duration_min": 30,
    "local_accept": 8.0,
    "local_reject": 7.3,
    "cookie": {
        "SESSDATA": "",
        "bili_jct": ""
    }
}


# ---------------------------------------------------------------- 基础

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return json.loads(json.dumps(default))


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def log(msg):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, datetime.now().strftime("%Y-%m") + ".log")
    with open(path, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def http(url, headers=None, data=None, timeout=25):
    """用 curl 发请求：TLS/HTTP2 指纹与浏览器一致，能避开 B 站风控。"""
    cmd = ["curl", "-sS", "--compressed", "-m", str(timeout), "-A", UA,
           "-b", COOKIE_PATH,
           "-H", "Accept: application/json, text/plain, */*",
           "-H", "Accept-Language: zh-CN,zh;q=0.9"]
    for k, v in (headers or {}).items():
        cmd += ["-H", "%s: %s" % (k, v)]
    if data is not None:
        cmd += ["--data", urllib.parse.urlencode(data)]
    cmd.append(url)
    last = None
    for attempt in range(3):
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        last = p
        time.sleep(3 * (attempt + 1))
    raise RuntimeError("curl 请求失败(%s) %s" % (last.returncode, last.stderr.strip()[:120]))


def ensure_cookie_file(cfg):
    """每次运行都用全新设备指纹预热，再把用户登录态注入 Cookie 文件。
    B 站对长期复用的 buvid 会软封禁（返回空列表），轮换指纹可稳定拿到数据。"""
    jar_path = COOKIE_PATH + ".jar"
    if os.path.exists(jar_path):
        os.remove(jar_path)
    subprocess.run(["curl", "-sS", "-m", "25", "-c", jar_path, "-A", UA,
                    "-H", "Accept-Language: zh-CN,zh;q=0.9",
                    "-o", "/dev/null", "https://www.bilibili.com/"],
                   capture_output=True, text=True)

    txt = ""
    if os.path.exists(jar_path):
        with open(jar_path, "r", encoding="utf-8") as f:
            txt = f.read()
    lines = [l for l in txt.splitlines() if l.strip()]

    c = cfg.get("cookie") or {}
    exp = int(time.time()) + 180 * 86400
    if c.get("SESSDATA"):
        lines.append(".bilibili.com\tTRUE\t/\tTRUE\t%d\tSESSDATA\t%s" % (exp, c["SESSDATA"]))
    if c.get("bili_jct"):
        lines.append(".bilibili.com\tTRUE\t/\tTRUE\t%d\tbili_jct\t%s" % (exp, c["bili_jct"]))
    with open(COOKIE_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.remove(jar_path)
    except OSError:
        pass
    # 预热与业务请求之间要留出间隔，否则易被判为脚本行为
    time.sleep(2.5)


# ---------------------------------------------------------------- B 站

def fetch_dynamics(cfg, pages):
    mid = cfg["mid"]
    items, offset = [], ""
    for _ in range(pages):
        q = urllib.parse.urlencode({
            "host_mid": mid,
            "offset": offset,
            "timezone_offset": -480,
        })
        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?" + q
        j = json.loads(http(url, headers={
            "Referer": "https://space.bilibili.com/%d/dynamic" % mid,
        }))
        if j.get("code") != 0:
            raise RuntimeError("动态接口返回 %s %s" % (j.get("code"), j.get("message")))
        d = j.get("data") or {}
        batch = d.get("items") or []
        items.extend(batch)
        offset = d.get("offset") or ""
        if not d.get("has_more") or not offset or not batch:
            break
        time.sleep(2)
    return items


def parse_videos(items):
    out = []
    for it in items:
        if it.get("type") != "DYNAMIC_TYPE_AV":
            continue
        modules = it.get("modules") or {}
        author = modules.get("module_author") or {}
        major = (modules.get("module_dynamic") or {}).get("major") or {}
        arc = major.get("archive") or {}
        if not arc.get("bvid"):
            continue
        try:
            aid = int(arc.get("aid") or 0)
        except Exception:
            aid = 0
        out.append({
            "bvid": arc.get("bvid"),
            "aid": aid,
            "title": (arc.get("title") or "").strip(),
            "desc": (arc.get("desc") or "").strip(),
            "ts": int(author.get("pub_ts") or 0),
        })
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


def nav_user():
    j = json.loads(http("https://api.bilibili.com/x/web-interface/nav",
                        headers={"Referer": "https://www.bilibili.com/"}))
    if j.get("code") != 0:
        return None
    d = j.get("data") or {}
    if not d.get("isLogin"):
        return None
    return {"mid": d.get("mid"), "uname": d.get("uname")}


def list_fav_folders(mid):
    url = "https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid=%s" % mid
    j = json.loads(http(url, headers={"Referer": "https://space.bilibili.com/%s/favlist" % mid}))
    if j.get("code") != 0:
        raise RuntimeError("收藏夹列表返回 %s %s" % (j.get("code"), j.get("message")))
    return ((j.get("data") or {}).get("list") or [])


def add_to_fav(aid, fid, csrf):
    data = {"rid": aid, "type": 2, "add_media_ids": fid, "del_media_ids": "", "csrf": csrf}
    raw = http("https://api.bilibili.com/x/v3/fav/resource/deal", data=data, headers={
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    })
    return json.loads(raw)


WBI_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
           33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
           26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
           20, 34, 44, 52]


def wbi_mixin():
    j = json.loads(http("https://api.bilibili.com/x/web-interface/nav",
                        headers={"Referer": "https://www.bilibili.com/"}))
    if j.get("code") != 0:
        raise RuntimeError("wbi 密钥获取失败 %s" % j.get("code"))
    img = j["data"]["wbi_img"]["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub = j["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    return "".join((img + sub)[i] for i in WBI_TAB)[:32]


def fetch_archive_page(mixin, pn, ps=30):
    """按发布时间倒序取投稿列表（含时长、aid），用于历史回扫。"""
    p = {"mid": MID, "ps": ps, "pn": pn, "order": "pubdate",
         "platform": "web", "web_location": 1550101, "wts": int(time.time())}
    q = "&".join("%s=%s" % (k, urllib.parse.quote(str(v), safe=""))
                 for k, v in sorted(p.items()))
    p["w_rid"] = hashlib.md5((q + mixin).encode()).hexdigest()
    url = "https://api.bilibili.com/x/space/wbi/arc/search?" + urllib.parse.urlencode(p)
    j = json.loads(http(url, headers={"Referer": "https://space.bilibili.com/%d/upload/video" % MID}))
    if j.get("code") != 0:
        raise RuntimeError("投稿列表返回 %s %s" % (j.get("code"), j.get("message")))
    d = j.get("data") or {}
    out = []
    for v in ((d.get("list") or {}).get("vlist") or []):
        out.append({
            "bvid": v.get("bvid"),
            "aid": int(v.get("aid") or 0),
            "title": (v.get("title") or "").strip(),
            "desc": (v.get("description") or "").strip(),
            "ts": int(v.get("created") or 0),
            "length": v.get("length") or "",
        })
    total = ((d.get("page") or {}).get("count")) or 0
    return out, total


def duration_seconds(text):
    parts = [p for p in (text or "").split(":") if p != ""]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return 0


def is_favorited(aid):
    """返回 True/False，接口异常时返回 None（按未收藏处理）。"""
    try:
        j = json.loads(http("https://api.bilibili.com/x/web-interface/archive/relation?aid=%s" % aid,
                            headers={"Referer": "https://www.bilibili.com/"}))
        if j.get("code") != 0:
            return None
        return bool((j.get("data") or {}).get("favorite"))
    except Exception:
        return None


# ---------------------------------------------------------------- 豆瓣

def norm(s):
    s = s or ""
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"[\s·・:：\-—_/\\#!！?？,，.。'\"“”‘’\[\]【】]+", "", s)
    return s.strip().lower()


# 本地豆瓣评分索引：命中且分数离阈值足够远时直接用，避免每次都打豆瓣
DB_INDEX_PATH = os.path.join(BASE, "data", "douban_index.json")
LOCAL_ACCEPT = 8.0   # 本地分 >= 此值，直接判定达标
LOCAL_REJECT = 7.3   # 本地分 <= 此值，直接判定不达标
_DB = None
_DOUBAN_COOKIE = ""  # 豆瓣登录 cookie，提升查询配额，减少限流


def local_db():
    global _DB
    if _DB is None:
        try:
            with open(DB_INDEX_PATH, "r", encoding="utf-8") as f:
                _DB = json.load(f)
        except Exception:
            _DB = {}
    return _DB


def local_lookup(name):
    hit = local_db().get(norm(name))
    if not hit:
        return None
    score = float(hit[0] or 0)
    if score >= LOCAL_ACCEPT or score <= LOCAL_REJECT:
        return {"status": "ok" if score > 0 else "no_rating", "score": score,
                "title": hit[2] or name, "url": "", "douban_id": None, "src": "local"}
    return None


def douban_search(q, count=10, tries=2):
    """豆瓣移动端搜索：一次请求同时拿到条目 id、片名和评分。"""
    url = "https://m.douban.com/rexxar/api/v2/search?" + urllib.parse.urlencode(
        {"q": q, "type": "movie", "start": 0, "count": count})
    _h = {"Referer": "https://m.douban.com/"}
    if _DOUBAN_COOKIE:
        _h["Cookie"] = _DOUBAN_COOKIE
    for i in range(tries):
        try:
            j = json.loads(http(url, headers=_h))
            out = []
            for it in ((j.get("subjects") or {}).get("items") or []):
                t = it.get("target") or {}
                if "/movie/" not in (t.get("uri") or ""):
                    continue
                out.append({
                    "id": str(t.get("id") or ""),
                    "title": t.get("title") or "",
                    "score": float((t.get("rating") or {}).get("value") or 0),
                })
            return out
        except Exception:
            pass
        if i < tries - 1:
            time.sleep(3 * (i + 1))
    return []


def pick_from_items(name, items, strict=False):
    n = norm(name)
    best, best_score = None, 0
    for c in items:
        t = norm(c.get("title"))
        if not t:
            continue
        s = 0
        if t == n:
            s = 100
        elif not strict and n and n in t:
            s = 75
        if s > best_score:
            best, best_score = c, s
    return best if best_score >= 75 else None


def douban_legacy(name, match=None):
    """搜索接口漏收时，退回旧的 suggest + 详情接口。match 用于校验结果标题。"""
    n = norm(match or name)
    _h = {"Referer": "https://movie.douban.com/"}
    if _DOUBAN_COOKIE:
        _h["Cookie"] = _DOUBAN_COOKIE
    _hm = {"Referer": "https://m.douban.com/"}
    if _DOUBAN_COOKIE:
        _hm["Cookie"] = _DOUBAN_COOKIE
    for q in (name, name + " 电影"):
        try:
            arr = json.loads(http("https://movie.douban.com/j/subject_suggest?q=" + urllib.parse.quote(q),
                                  headers=_h))
        except Exception:
            arr = []
        if arr:
            best, best_score = None, 0
            for c in arr:
                if c.get("type") != "movie":
                    continue
                t = norm(c.get("title"))
                s = 100 if t == n else (75 if n and n in t else 0)
                if s > best_score:
                    best, best_score = c, s
            if best and best_score >= 75:
                try:
                    d = json.loads(http("https://m.douban.com/rexxar/api/v2/movie/%s" % best["id"],
                                        headers=_hm))
                except Exception:
                    return None
                v = float((d.get("rating") or {}).get("value") or 0)
                return {"status": "ok" if v > 0 else "no_rating", "score": v,
                        "title": d.get("title") or best.get("title") or name,
                        "url": "https://movie.douban.com/subject/%s/" % best["id"],
                        "douban_id": str(best["id"])}
        time.sleep(1.5)
    return None


def lookup_douban(name):
    hit = local_lookup(name)
    if hit:
        return hit

    queries = [name]
    core = re.split(r"[：:·]", name)[0].strip()
    if core and core != name:
        queries.append(core)
    short = re.sub(r"[！!·\s\d]+$", "", core)      # 去掉尾部序号/标点：碟中谍7→碟中谍
    if short and short != core:
        queries.append(short)
    short2 = re.sub(r"[！!·\s\d]+$", "", name)
    if short2 and short2 != name and short2 not in queries:
        queries.append(short2)
    subj = None
    for idx, q in enumerate(queries):
        subj = pick_from_items(name, douban_search(q), strict=False)
        if subj:
            break
        time.sleep(1.5)
    if not subj:
        # rexxar 搜索漏收时退回 legacy suggest 接口兜底
        for q in queries + [name + " 电影"]:
            subj = douban_legacy(q, match=name)
            if subj:
                break
            time.sleep(1.5)
    if not subj:
        return {"status": "not_found"}
    score = subj.get("score") or 0
    _id = subj.get("id") or subj.get("douban_id")
    return {
        "status": "ok" if score > 0 else "no_rating",
        "score": score,
        "title": subj.get("title") or name,
        "url": "https://movie.douban.com/subject/%s/" % _id if _id else "",
        "douban_id": _id,
    }


def movie_name(title):
    m = re.search(r"【([^】]+)】", title or "")
    if m:
        name = m.group(1)
    else:
        name = re.sub(r"^[【\[(（][^】\])）]*[】\])）]", "", title or "")
        name = re.split(r"[|｜]", name)[0]
    name = re.sub(r"^(电影|影片|完整版|高清|4K|免费)[:：]?", "", name.strip())
    name = re.sub(r"[（(](国语|粤语|高清|完整版|中字)[）)]", "", name)
    return name.strip()


def audio_info(text):
    """判定音轨版本，数值越大越接近原声：原声 > 外语 > 粤语 > 未标注 > 国语配音。"""
    t = text or ""
    has_orig = bool(re.search(r"(原声|原音|原版)", t))
    has_dub = bool(re.search(r"(国语|国配|普通话|中文配音|译制)", t))
    has_canto = bool(re.search(r"(粤语|广东话)", t))
    has_lang = bool(re.search(r"(英语|英文|日语|日文|韩语|法语|德语|意大利语|西班牙语|泰语)", t))
    if has_orig and has_dub:
        return 45, "原声版"
    if has_orig:
        return 50, "原声版"
    if has_lang and not has_dub:
        return 40, "原声版"
    if has_canto and has_dub:
        return 30, "粤语版"
    if has_canto:
        return 35, "粤语版"
    if has_dub:
        return 10, "国语版"
    return 20, "未标注版本"


def group_versions(videos):
    """同一部片多版本时只留最接近原声的一版；返回 (保留, [(保留, 被弃), ...])"""
    groups = {}
    for v in videos:
        gkey = v["info"].get("douban_id") or ("name:" + norm(v["movie_name"]))
        groups.setdefault(gkey, []).append(v)
    picked, dropped = [], []
    for vs in groups.values():
        vs.sort(key=lambda x: (-x["rank"], x["ts"]))
        picked.append(vs[0])
        dropped.extend((vs[0], o) for o in vs[1:])
    picked.sort(key=lambda x: x["ts"])
    return picked, dropped


# ---------------------------------------------------------------- 历史回扫

def fetch_dynamic_page(cfg, offset):
    mid = cfg["mid"]
    q = urllib.parse.urlencode({"host_mid": mid, "offset": offset, "timezone_offset": -480})
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?" + q
    j = json.loads(http(url, headers={"Referer": "https://space.bilibili.com/%d/dynamic" % mid}))
    if j.get("code") != 0:
        raise RuntimeError("%s %s" % (j.get("code"), j.get("message")))
    d = j.get("data") or {}
    return d.get("items") or [], (d.get("offset") or ""), bool(d.get("has_more"))


def probe_douban():
    """用知名影片探测豆瓣接口是否真被限流。
    B 站这类片源里「豆瓣未收录」是常态（实测占七成），连续查无多是片源冷门，
    不能直接当成限流收尾，否则每天都会白白少扫一大片。"""
    for name in ("星际穿越", "泰坦尼克号"):
        try:
            info = lookup_douban(name)
        except Exception:
            time.sleep(4)
            continue
        if info.get("status") == "ok" and (info.get("score") or 0) > 0:
            return True
        time.sleep(4)
    return False


def run_backfill(cfg, args, state, fid, csrf):
    """沿动态流向前翻页回扫历史投稿（archive 列表接口更易被风控，这里复用动态源）。"""
    batches = args.backfill
    seen = state.setdefault("seen", {})
    limit = cfg.get("min_score", 7.8)

    offset = state.get("backfill_offset", "") or ""
    prev_offset = offset
    pool, got = [], 0
    for i in range(batches):
        try:
            items, offset, has_more = fetch_dynamic_page(cfg, offset)
        except Exception as e:
            if got == 0:
                print("回扫失败：动态翻页异常 %s" % e)
                return 1
            print("回扫中断：第 %d 批异常 %s" % (i + 1, e))
            break
        got += 1
        for v in parse_videos(items):
            if v["bvid"] not in seen:
                pool.append(v)
        if not has_more or not offset:
            break
        time.sleep(4)

    uniq = {}
    for v in pool:
        uniq[v["bvid"]] = v
    pool = list(uniq.values())

    state["backfill_offset"] = offset
    state["backfill_batch"] = int(state.get("backfill_batch", 0)) + got

    if not pool:
        if not args.dry_run:
            save_json(STATE_PATH, state)
        print("回扫 %d 批：没有新的投稿（可能已到末尾）" % got)
        return 0


    # 先剔除已收藏的，别把豆瓣配额浪费在不需要的片上
    candidates, dupfav = [], 0
    for v in pool:
        v["movie_name"] = movie_name(v["title"])
        v["rank"], v["ver"] = audio_info(v["title"] + " " + v["desc"])
        fav = is_favorited(v["aid"])
        time.sleep(1.0)
        if fav:
            dupfav += 1
            seen[v["bvid"]] = {"ts": v["ts"], "movie": v["movie_name"], "status": "dup_fav",
                               "aid": v["aid"], "ver": v["ver"], "src": None, "action": "dup_fav"}
            continue
        candidates.append(v)

    # 同名多版本先按片名归并，只留最接近原声的一版
    groups = {}
    for v in candidates:
        groups.setdefault(norm(v["movie_name"]), []).append(v)
    picked, dropped = [], []
    for vs in groups.values():
        vs.sort(key=lambda x: (-x["rank"], x["ts"]))
        picked.append(vs[0])
        dropped.extend((vs[0], o) for o in vs[1:])

    db_cache = state.setdefault("db_cache", {})
    now_ts = int(time.time())
    miss_streak = 0
    throttled = False
    for v in picked:
        k = norm(v["movie_name"])
        hit = db_cache.get(k)
        if (hit and now_ts - hit.get("at", 0) < 30 * 86400
                and (hit.get("info") or {}).get("status") != "not_found"):
            v["info"] = hit["info"]
            continue
        v["info"] = lookup_douban(v["movie_name"])
        if v["info"]["status"] != "not_found":
            db_cache[k] = {"at": now_ts, "info": v["info"]}
        if v["info"]["status"] == "not_found":
            miss_streak += 1
            if miss_streak >= 5:
                # 连续查无很可能是这批片源太冷门，先用知名影片探针确认接口是否真被限流
                if probe_douban():
                    miss_streak = 0     # 接口正常：判为真未收录，继续往下扫
                else:
                    print("豆瓣疑似限流（连续 %d 部查无结果），本次回扫提前收尾" % miss_streak)
                    throttled = True
                    v["info"] = {"status": "skipped"}
                    idx = picked.index(v)
                    for rest in picked[idx + 1:]:
                        rest["info"] = {"status": "skipped"}
                    break
        else:
            miss_streak = 0
        time.sleep(10)

    added, skipped, failed = [], [], []
    qualified = 0
    for v in picked:
        info, name = v["info"], v["movie_name"]
        if info["status"] == "skipped":
            skipped.append((name, "豆瓣限流，留待下次"))
            # 关键：限流片也入待补评队列并写 seen，offset 推进后不会丢片，
            # 后续由 retry_pending 按 12h 间隔每轮最多 3 部择机重查
            enqueue_pending(state, v, info)
            seen[v["bvid"]] = {"ts": v["ts"], "movie": name, "status": "throttled",
                               "aid": v["aid"], "ver": v["ver"], "src": None,
                               "action": "throttled"}
            continue
        if info["status"] != "ok":
            skipped.append((name, {"not_found": "豆瓣未收录", "error": "豆瓣查询失败",
                                   "no_rating": "豆瓣暂无评分"}.get(info["status"], "未知")))
            if info["status"] in ("no_rating", "not_found"):
                enqueue_pending(state, v, info)
            seen[v["bvid"]] = {"ts": v["ts"], "movie": name, "score": info.get("score"),
                               "status": info["status"]}
            continue
        if info["score"] < limit:
            skipped.append((name, "%.1f 分不达标" % info["score"]))
            seen[v["bvid"]] = {"ts": v["ts"], "movie": name, "score": info["score"],
                               "status": "low_score"}
            continue
        qualified += 1
        if args.dry_run:
            added.append({"movie": info.get("title") or name, "score": info["score"],
                          "ver": v["ver"], "src": info.get("src"), "action": "dry-run"})
            seen[v["bvid"]] = {"ts": v["ts"], "movie": name, "score": info["score"],
                               "status": "ok", "aid": v["aid"], "ver": v["ver"],
                               "rank": v["rank"], "douban_id": info.get("douban_id"),
                               "action": "dry-run"}
            continue

        # 同片已收藏过则跳过（按 douban_id 查重，本地索引命中时按片名兜底）
        _same = [s for s in seen.values()
                 if s.get("action") in ("added", "already") and (
                     (info.get("douban_id") and s.get("douban_id") == info.get("douban_id"))
                     or (not info.get("douban_id") and s.get("movie") == name))]
        if _same:
            skipped.append((name, "同片已收藏（%s）" % (_same[0].get("ver") or "另一版本")))
            seen[v["bvid"]] = {"ts": v["ts"], "movie": name, "score": info["score"],
                               "status": "ok", "aid": v["aid"], "ver": v["ver"],
                               "rank": v["rank"], "douban_id": info.get("douban_id"),
                               "action": "dup_fav"}
            time.sleep(1.0)
            continue
        try:
            r = add_to_fav(v["aid"], fid, csrf)
            if r.get("code") == 0:
                added.append({"movie": info.get("title") or name, "score": info["score"],
                              "ver": v["ver"], "src": info.get("src"), "action": "added"})
            elif r.get("code") == 11201:
                added.append({"movie": info.get("title") or name, "score": info["score"],
                              "ver": v["ver"], "src": info.get("src"), "action": "already"})
            else:
                failed.append((name, "%s %s" % (r.get("code"), r.get("message"))))
        except Exception as e:
            failed.append((name, str(e)[:50]))
        seen[v["bvid"]] = {"ts": v["ts"], "movie": name, "score": info["score"],
                           "status": "ok", "aid": v["aid"], "ver": v["ver"],
                           "rank": v["rank"], "douban_id": info.get("douban_id"),
                           "action": "added"}
        time.sleep(1.8)

    for keep, drop in dropped:
        skipped.append((drop["movie_name"], "%s（同片保留%s）" % (
            drop["ver"], "另一版本" if keep["ver"] == "未标注版本" else keep["ver"])))

    if len(seen) > 8000:
        keep = sorted(seen.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:8000]
        state["seen"] = dict(keep)
    if len(db_cache) > 3000:
        keepc = sorted(db_cache.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)[:3000]
        state["db_cache"] = dict(keepc)
    # 限流片已在上面入待补评队列(pending)并写 seen，游标不回退，
    # 让回扫持续推进，限流片由 retry_pending 按 12h 间隔逐部重查
    if not args.dry_run:
        save_json(STATE_PATH, state)

    head = "回扫 %d 批｜投稿 %d｜达标 %d｜新增 %d｜已收藏跳过 %d｜跳过 %d" % (
        got, len(pool), qualified,
        sum(1 for a in added if a.get("action") in ("added", "already")), dupfav, len(skipped))
    if failed:
        head += "｜失败 %d" % len(failed)
    lines = [head]
    for a in added:
        tag = {"added": "已收藏", "already": "已在夹内", "dry-run": "预演"}.get(a.get("action"), "处理")
        ver = "" if a.get("ver") in (None, "", "未标注版本") else "（%s）" % a["ver"]
        src = "〔库〕" if a.get("src") == "local" else ""
        lines.append("- %s《%s》%.1f 分%s%s" % (tag, a["movie"], a["score"], ver, src))
    for n, why in skipped:
        lines.append("- 跳过《%s》%s" % (n, why))
    for n, why in failed:
        lines.append("- 失败《%s》%s" % (n, why))
    out = "\n".join(lines)
    print(out)
    log(out.replace("\n", " / "))
    return 0


# ---------------------------------------------------------------- 待补评队列

PENDING_DAYS = 30
PENDING_MAX = 200
PENDING_RETRY_GAP = 12 * 3600


def enqueue_pending(state, v, info):
    """豆瓣暂无评分的片子挂起，之后每轮最多重查 3 部。"""
    pending = state.setdefault("pending", {})
    if len(pending) >= PENDING_MAX:
        oldest = min(pending.items(), key=lambda kv: kv[1].get("ts", 0))
        pending.pop(oldest[0], None)
    pending[v["bvid"]] = {
        # 入队时间（不是稿件发布时间）：稿件多为历史投稿，用发布时间会被 30 天清理秒删
        "ts": int(time.time()),
        "movie": v["movie_name"],
        "aid": v["aid"],
        "title": v["title"],
        "ver": v["ver"],
        "rank": v["rank"],
        "tries": 0,
        "last": int(time.time()),   # 至少隔 12 小时再查，避免同一轮反复打豆瓣
    }
    state["pending"] = pending


def retry_pending(state, limit, fid, csrf, dry_run=False, max_n=3):
    """重查待补评队列，返回一行摘要；没有可查的返回 None。"""
    pending = state.get("pending") or {}
    now = int(time.time())

    for bv in list(pending):
        if now - pending[bv].get("ts", now) > PENDING_DAYS * 86400:
            pending.pop(bv, None)

    due = [bv for bv, p in pending.items() if now - p.get("last", 0) >= PENDING_RETRY_GAP]
    due.sort(key=lambda bv: pending[bv].get("last", 0))
    due = due[:max_n]
    state["pending"] = pending
    if not due:
        return None

    added, tried, misses = [], 0, 0
    for bv in due:
        p = pending[bv]
        info = lookup_douban(p.get("movie") or "")
        tried += 1
        p["tries"] = int(p.get("tries", 0)) + 1
        p["last"] = now

        if info["status"] == "ok":
            misses = 0
            if info["score"] < limit:
                pending.pop(bv, None)          # 查到分了但不达标，出队
            elif dry_run:
                added.append((info.get("title") or p.get("movie"), info["score"]))
            else:
                if not is_favorited(p.get("aid") or 0):
                    try:
                        r = add_to_fav(p.get("aid"), fid, csrf)
                        if r.get("code") in (0, 11201):
                            added.append((info.get("title") or p.get("movie"), info["score"]))
                        pending.pop(bv, None)
                    except Exception:
                        pass
                    time.sleep(1.8)
                else:
                    pending.pop(bv, None)
        elif info["status"] == "not_found":
            misses += 1
            if misses >= 2:
                break
        time.sleep(4)

    state["pending"] = pending
    if not tried:
        return None
    head = "待补评 %d 部｜补入 %d 部" % (tried, len(added))
    if added:
        head += "（%s）" % "、".join("《%s》%.1f" % a for a in added)
    return head


# ---------------------------------------------------------------- 主流程

def main():
    global LOCAL_ACCEPT, LOCAL_REJECT
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只比对评分，不写入收藏夹")
    ap.add_argument("--check", action="store_true", help="检查登录状态与收藏夹")
    ap.add_argument("--since-hours", type=float, default=None)
    ap.add_argument("--pages", type=int, default=None)
    ap.add_argument("--backfill", type=int, default=0,
                    help="回扫历史投稿的页数（每页 30 条，按发布时间倒序）")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, cfg)
    global _DOUBAN_COOKIE
    _DOUBAN_COOKIE = cfg.get("douban_cookie") or ""
    LOCAL_ACCEPT = float(cfg.get("local_accept", LOCAL_ACCEPT))
    LOCAL_REJECT = float(cfg.get("local_reject", LOCAL_REJECT))
    ensure_cookie_file(cfg)

    if args.check:
        who = nav_user()
        if not who:
            print("登录检查：失败（SESSDATA 缺失或已过期）")
            return 2
        print("登录检查：成功，UID=%s 昵称=%s" % (who["mid"], who["uname"]))
        try:
            folders = list_fav_folders(who["mid"])
        except Exception as e:
            print("收藏夹读取失败：%s" % e)
            return 2
        print("收藏夹共 %d 个：" % len(folders))
        for f in folders:
            mark = "  <= 目标" if f.get("title") == cfg["fav_folder"] else ""
            print("  %-10s %-26s %d 个视频%s" % (
                f.get("id"), f.get("title"), f.get("media_count", 0), mark))
        target = [f for f in folders if f.get("title") == cfg["fav_folder"]]
        if target:
            print("目标收藏夹：%s（ID %s，现有 %d 个视频）" % (
                target[0]["title"], target[0]["id"], target[0].get("media_count", 0)))
            return 0
        print("未找到名为「%s」的收藏夹" % cfg["fav_folder"])
        return 3

    state = load_json(STATE_PATH, {"last_ts": 0, "seen": {}})

    if args.backfill:
        who = nav_user()
        if not who:
            print("回扫中断：B 站未登录（SESSDATA 缺失或过期）")
            return 2
        try:
            folders = list_fav_folders(who["mid"])
        except Exception as e:
            print("回扫中断：收藏夹读取失败 %s" % e)
            return 2
        target = [f for f in folders if f.get("title") == cfg["fav_folder"]]
        if not target:
            print("回扫中断：没有找到收藏夹「%s」" % cfg["fav_folder"])
            return 3
        return run_backfill(cfg, args, state, target[0]["id"],
                            (cfg.get("cookie") or {}).get("bili_jct", ""))

    pages = args.pages or cfg.get("pages", 2)

    try:
        items = fetch_dynamics(cfg, pages)
    except Exception as e:
        time.sleep(60)
        try:
            items = fetch_dynamics(cfg, pages)
        except Exception as e2:
            print("监控失败：动态拉取异常 %s" % e2)
            return 1

    videos = parse_videos(items)
    if not videos:
        # code 0 但列表为空，通常是风控软封禁；换指纹再试一次，仍空则明确报错
        time.sleep(45)
        ensure_cookie_file(cfg)
        try:
            items = fetch_dynamics(cfg, pages)
        except Exception:
            items = []
        videos = parse_videos(items)
    if not videos:
        print("监控失败：动态接口返回空列表（疑似 B 站风控），本次未处理")
        return 1
    seen = state.setdefault("seen", {})

    if args.since_hours is not None:
        cutoff = int(time.time() - args.since_hours * 3600)
    elif not state.get("last_ts"):
        cutoff = int(time.time() - 48 * 3600)
    else:
        cutoff = int(state["last_ts"]) - 60

    fresh = [v for v in videos if v["ts"] >= cutoff and v["bvid"] not in seen]

    fid, folder_title = None, cfg["fav_folder"]
    limit = cfg.get("min_score", 7.8)
    csrf = (cfg.get("cookie") or {}).get("bili_jct", "")
    if not args.dry_run:
        who = nav_user()
        if not who:
            print("监控中断：B 站未登录（SESSDATA 缺失或过期）")
            return 2
        try:
            folders = list_fav_folders(who["mid"])
        except Exception as e:
            print("监控中断：收藏夹读取失败 %s" % e)
            return 2
        target = [f for f in folders if f.get("title") == folder_title]
        if not target:
            names = "、".join(f.get("title", "") for f in folders[:12])
            print("监控中断：没有找到收藏夹「%s」。现有：%s" % (folder_title, names))
            return 3
        fid = target[0]["id"]

    if not fresh:
        state["last_ts"] = max([v["ts"] for v in videos] + [state.get("last_ts", 0)])
        note = retry_pending(state, limit, fid, csrf, args.dry_run)
        save_json(STATE_PATH, state)
        latest = datetime.fromtimestamp(videos[0]["ts"]).strftime("%m-%d %H:%M") if videos else "未知"
        print("OK 无新投稿（UP 主最近一条发布于 %s）" % latest)
        if note:
            print(note)
        return 0

    added, skipped, failed = [], [], []

    # 1) 逐条识别片名、豆瓣评分与音轨版本
    cache = {}
    miss_streak = 0
    throttled = False
    for v in fresh:
        name = movie_name(v["title"])
        k = norm(name)
        if k not in cache:
            cache[k] = lookup_douban(name)
            time.sleep(3)
        v["movie_name"] = name
        v["info"] = cache[k]
        v["rank"], v["ver"] = audio_info(v["title"] + " " + v["desc"])
        if v["info"]["status"] in ("not_found", "error"):
            miss_streak += 1
        else:
            miss_streak = 0
        if miss_streak >= 3:
            throttled = True
            print("豆瓣疑似限流，本次不推进水位，稍后重试")
            break

    if throttled:
        # 已入库的照常记账，未处理的留到下次
        save_json(STATE_PATH, state)
        print("OK 本次因豆瓣限流中止（已处理 %d 条）" % len(seen))
        return 1

    # 2) 同一部片出现多个版本时，只保留最接近原声的一版
    picked, dropped_versions = group_versions(fresh)
    for keep, drop in dropped_versions:
        seen[drop["bvid"]] = {"ts": drop["ts"], "movie": drop["movie_name"],
                              "score": drop["info"].get("score"),
                              "status": "dup_version"}

    # 3) 按评分决定是否入库
    for v in picked:
        info, name = v["info"], v["movie_name"]
        if info["status"] != "ok":
            reason = {"not_found": "豆瓣未收录", "error": "豆瓣查询失败",
                      "no_rating": "豆瓣暂无评分"}.get(info["status"], "未知原因")
            skipped.append((name, reason))
            if info["status"] in ("no_rating", "not_found"):
                enqueue_pending(state, v, info)
            seen[v["bvid"]] = {"ts": v["ts"], "movie": name,
                               "score": info.get("score"), "status": info["status"]}
            continue
        if info["score"] < limit:
            skipped.append((name, "%.1f 分不达标" % info["score"]))
            seen[v["bvid"]] = {"ts": v["ts"], "movie": name,
                               "score": info["score"], "status": "low_score"}
            continue

        rec = {
            "title": v["title"],
            "movie": info.get("title") or name,
            "score": info["score"],
            "ver": v["ver"],
            "src": info.get("src"),
            "douban": info["url"],
            "bilibili": "https://www.bilibili.com/video/%s" % v["bvid"],
            "ts": v["ts"],
        }
        # 此前若收过同片的低版本（如国语版），提示补入原声版
        prior = [s for s in seen.values()
                 if s.get("douban_id") and s.get("douban_id") == info.get("douban_id")
                 and s.get("action") in ("added", "already")
                 and s.get("rank", 0) < v["rank"]]
        note = None
        if prior:
            note = "此前收藏的是%s，已补入%s，旧条目未自动移出" % (
                prior[0].get("ver") or "旧版本", v["ver"])
        if args.dry_run:
            rec["action"] = "dry-run"
            added.append(rec)
            seen[v["bvid"]] = {"ts": v["ts"], "movie": name, "score": info["score"],
                               "status": "ok", "aid": v["aid"], "ver": v["ver"],
                               "rank": v["rank"], "douban_id": info.get("douban_id"),
                               "action": "dry-run"}
            continue
        if is_favorited(v["aid"]):
            skipped.append((name, "已经收藏过了"))
            seen[v["bvid"]] = {"ts": v["ts"], "movie": name, "score": info["score"],
                               "status": "ok", "aid": v["aid"], "ver": v["ver"],
                               "rank": v["rank"], "douban_id": info.get("douban_id"),
                               "action": "dup_fav"}
            time.sleep(1.2)
            continue
        time.sleep(1.2)
        try:
            r = add_to_fav(v["aid"], fid, csrf)
            code = r.get("code")
            if code == 0:
                rec["action"] = "added"
                added.append(rec)
            elif code == 11201:
                rec["action"] = "already"
                added.append(rec)
            else:
                failed.append((name, "%s %s" % (code, r.get("message"))))
        except Exception as e:
            failed.append((name, str(e)[:60]))
        seen[v["bvid"]] = {"ts": v["ts"], "movie": name, "score": info["score"],
                           "status": "ok", "aid": v["aid"], "ver": v["ver"],
                           "rank": v["rank"], "douban_id": info.get("douban_id"),
                           "action": rec.get("action") or "failed"}
        if note:
            rec["note"] = note
        time.sleep(2)

    # 被同片原声版挤掉的版本，在报告里点明
    for keep, drop in dropped_versions:
        keep_label = "另一版本" if keep["ver"] == "未标注版本" else keep["ver"]
        skipped.append((drop["movie_name"], "%s（同片保留%s）" % (drop["ver"], keep_label)))

    state["last_ts"] = max([v["ts"] for v in videos] + [state.get("last_ts", 0)])
    if len(seen) > 400:
        keep = sorted(seen.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:400]
        state["seen"] = dict(keep)
    pending_note = retry_pending(state, limit, fid, csrf, args.dry_run)
    save_json(STATE_PATH, state)

    head = "新增投稿 %d｜达标 %d｜入库 %d｜跳过 %d" % (
        len(fresh), len(added),
        sum(1 for a in added if a.get("action") in ("added", "already")), len(skipped))
    if failed:
        head += "｜失败 %d" % len(failed)
    lines = [head]
    for a in added:
        tag = {"added": "已收藏", "already": "已在夹内", "dry-run": "预演"}.get(a.get("action"), "处理")
        ver = "" if a.get("ver") in (None, "", "未标注版本") else "（%s）" % a["ver"]
        src = "〔库〕" if a.get("src") == "local" else ""
        lines.append("- %s《%s》%.1f 分%s%s → %s" % (
            tag, a.get("movie") or a["title"], a["score"], ver, src, folder_title))
        if a.get("note"):
            lines.append("- 提示《%s》%s" % (a.get("movie") or a["title"], a["note"]))
    for n, why in skipped:
        lines.append("- 跳过《%s》%s" % (n, why))
    for n, why in failed:
        lines.append("- 失败《%s》%s" % (n, why))
    if pending_note:
        lines.append(pending_note)
    out = "\n".join(lines)
    print(out)
    log(out.replace("\n", " / "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
