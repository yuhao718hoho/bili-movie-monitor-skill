---
name: bilibili-movie-fav-monitor
description: 监控 B 站指定 UP 主的电影投稿并按豆瓣评分自动收藏到 B 站收藏夹。用于：①每日定时检查某 UP 主（如「哔哩哔哩电影」mid=15773384）的新投稿；②分批回溯该 UP 主历史投稿；③按豆瓣评分阈值（默认 7.8）筛选后自动加入 B 站收藏夹；④维护待补评队列并逐轮重查；⑤排查 B 站 cookie 失效、豆瓣限流、重复收藏等问题。
---

# B 站电影投稿监控与自动收藏

## 快速开始

1. 准备配置：把 `assets/config.example.json` 复制为 `scripts/config.json`，填入 B 站 cookie 与豆瓣 cookie（见下）。
2. 跑一次：`cd scripts && python3 run_daily.py`。正常 8~15 分钟，前台超过 15 秒会自动转后台，用 TaskOutput 收结果。
3. 看结果：终端只输出 1~3 行摘要（新增投稿/待补评/回扫），明细写入 `scripts/logs/YYYY-MM.log`。

## 配置要点（scripts/config.json）

| 字段 | 说明 |
|---|---|
| `mid` | 被监控 UP 主 uid，如 15773384 |
| `up_name` | UP 主昵称，仅用于日志 |
| `fav_folder` | 目标 B 站收藏夹名称，需账号内已存在 |
| `min_score` | 豆瓣评分阈值，默认 7.8 |
| `pages` | 每轮新投稿翻页数，默认 1 |
| `cookie` | 对象，仅需 `SESSDATA` 与 `bili_jct` 两个字段 |
| `douban_cookie` | 字符串，豆瓣登录 cookie，用于降低限流概率 |
| `local_accept` / `local_reject` | 本地索引命中时的收/拒门槛 |

`scripts/config.json`、`state.json`、`bili_cookies.txt` 属于私密凭证与运行状态，**不要提交到公开仓库**。

## 核心机制（改动代码前必读）

- **防重复收藏**：收藏前必查 `is_favorited(aid)`，且 `state.json` 的 `seen` 记录已处理视频；接口返回 code 11201（已在夹内）同样按成功处理。
- **版本去重**：同一影片有多个投稿版本（"未标注版本"/"国语版"/"粤语版"）时，只保留 rank 最高的一版，其余跳过。
- **待补评队列**：低分/未收录/暂无评分的片进 `pending`，由 `retry_pending()` 每轮重查有限配额；`enqueue_pending` 的 `ts` 必须是入队时间 `int(time.time())`，若写成稿件发布时间会被 30 天淘汰规则秒删。
- **豆瓣限流判定**："连续 N 部查无结果"不等于限流——B 站这类片源实测约七成是真的豆瓣未收录。必须用 `probe_douban()` 探针（查《星际穿越》等知名影片）确认接口是否真的不可用，否则会每天误判收尾、白白少扫大片。
- **游标不回退**：被限流的片已入 `pending` 并写 `seen`，回扫游标只前进不后退，避免反复扫同一区域。

## 排障

| 现象 | 判定 | 处置 |
|---|---|---|
| 输出「B 站未登录 / cookie 过期」 | `nav` 接口 code=-101、isLogin=false | 重新获取 SESSDATA 与 bili_jct 后写回 config，**不要反复重试** |
| 输出「动态接口返回空列表」 | 先跑 `nav` 探针；nav 正常则接口问题，nav 失败则 cookie 失效 | 按上一条处置 |
| 回扫出现大量「豆瓣限流，留待下次」 | 用 `probe_douban()` 复核 | 探针正常说明是真未收录，可继续扫；探针失败才是真限流 |
| 收藏夹里出现重复影片 | 检查版本去重与 `seen` 是否被绕过 | 见 `references/ops-notes.md` 的去重规则 |
| 待补评队列只增不减 | 检查 `enqueue_pending` 的 `ts` 取值 | 必须为入队时间 |

## 更多细节

运维细节、cookie 获取流程、参数调优、历史踩坑记录见 `references/ops-notes.md`；批量补查待补评影片可用 `scripts/retry_pending_batch.py`。
