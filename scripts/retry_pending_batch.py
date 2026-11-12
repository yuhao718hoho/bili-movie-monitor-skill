#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量补查待补评队列中的影片，达标则收藏。

用法：python3 retry_pending_batch.py [数量，默认45]
内置豆瓣探针：每 8 部用知名影片复核接口，探针失败才停止，避免把"真未收录"误判成限流。
"""

import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
N = 45
if len(sys.argv) > 1:
    try:
        N = max(1, min(int(sys.argv[1]), 200))
    except ValueError:
        N = 45
sys.path.insert(0, BASE)
sys.argv = ['monitor.py']
import monitor


def main():
    cfg = json.load(open(os.path.join(BASE, 'config.json'), encoding='utf-8'))
    monitor._DOUBAN_COOKIE = cfg.get('douban_cookie') or ''
    monitor.ensure_cookie_file(cfg)

    nav = json.loads(monitor.http('https://api.bilibili.com/x/web-interface/nav',
                                  headers={'Referer': 'https://www.bilibili.com/'}))
    if nav.get('code') != 0 or not (nav.get('data') or {}).get('isLogin'):
        print('B 站未登录（code=%s），请先更新 config.json 的 cookie' % nav.get('code'))
        return 2
    mid = nav['data']['mid']
    folders = monitor.list_fav_folders(mid)
    target = [f for f in folders if f.get('title') == cfg.get('fav_folder')]
    if not target:
        print('找不到收藏夹「%s」' % cfg.get('fav_folder'))
        return 3
    fid = target[0]['id']
    csrf = cfg['cookie']['bili_jct']
    limit = float(cfg.get('min_score', 7.8))

    state = json.load(open(os.path.join(BASE, 'state.json'), encoding='utf-8'))
    pending = state.setdefault('pending', {})
    seen = state.setdefault('seen', {})

    cand = [(bv, p.get('ts', 0)) for bv, p in pending.items()
            if (seen.get(bv) or {}).get('status') == 'throttled']
    cand.sort(key=lambda x: -x[1])
    cand = [bv for bv, _ in cand][:N]
    print('待补查 %d 部' % len(cand), flush=True)
    if not cand:
        return 0

    def probe():
        for name in ('星际穿越', '泰坦尼克号'):
            try:
                r = monitor.lookup_douban(name)
            except Exception:
                time.sleep(6)
                continue
            if r.get('status') == 'ok' and (r.get('score') or 0) > 0:
                return True
            time.sleep(5)
        return False

    if not probe():
        print('豆瓣接口不可用，停止本轮补查')
        return 1

    added, low, nf = [], 0, 0
    for n, bv in enumerate(cand, 1):
        p = pending.get(bv) or {}
        movie = p.get('movie') or ''
        try:
            info = monitor.lookup_douban(movie)
        except Exception as e:
            print('%d/%d 《%s》查询异常 %s' % (n, len(cand), movie, e), flush=True)
            time.sleep(8)
            continue
        st = info.get('status')
        if st == 'ok':
            score = info.get('score') or 0
            if score >= limit:
                aid = p.get('aid') or 0
                if not monitor.is_favorited(aid):
                    try:
                        r = monitor.add_to_fav(aid, fid, csrf)
                    except Exception as e:
                        print('%d/%d 《%s》收藏异常 %s' % (n, len(cand), movie, e), flush=True)
                        time.sleep(8)
                        continue
                    if r.get('code') in (0, 11201):
                        added.append((info.get('title') or movie, score))
                        print('%d/%d 《%s》%.1f 已收藏' % (n, len(cand), movie, score), flush=True)
                    else:
                        print('%d/%d 《%s》收藏失败 code=%s' % (n, len(cand), movie, r.get('code')), flush=True)
                else:
                    print('%d/%d 《%s》%.1f 已在收藏夹' % (n, len(cand), movie, score), flush=True)
                pending.pop(bv, None)
                seen[bv] = {'ts': (seen.get(bv) or {}).get('ts'), 'movie': movie,
                            'score': score, 'status': 'ok', 'action': 'fav'}
            else:
                low += 1
                print('%d/%d 《%s》%.1f 不达标' % (n, len(cand), movie, score), flush=True)
                pending.pop(bv, None)
                seen[bv] = {'ts': (seen.get(bv) or {}).get('ts'), 'movie': movie,
                            'score': score, 'status': 'low_score', 'action': 'skip'}
        elif st == 'not_found':
            nf += 1
            print('%d/%d 《%s》未收录' % (n, len(cand), movie), flush=True)
        else:
            print('%d/%d 《%s》%s' % (n, len(cand), movie, st), flush=True)
        json.dump(state, open(os.path.join(BASE, 'state.json'), 'w'), ensure_ascii=False, indent=1)
        time.sleep(6)
        if n % 8 == 0:
            if not probe():
                print('探针异常，疑似限流，提前停止（已处理 %d 部）' % n, flush=True)
                break
            time.sleep(10)

    json.dump(state, open(os.path.join(BASE, 'state.json'), 'w'), ensure_ascii=False, indent=1)
    print('DONE 收藏 %d｜不达标 %d｜未收录 %d｜pending 剩余 %d'
          % (len(added), low, nf, len(pending)), flush=True)
    if added:
        print('收藏明细: ' + '、'.join('《%s》%.1f' % a for a in added), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
