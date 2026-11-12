# bilibili-movie-fav-monitor

一个 Claude/Agent Skill，也一套能独立跑的 Python 脚本：监控 B 站指定 UP 主的电影投稿，按豆瓣评分自动收藏进 B 站收藏夹。

## 它做什么

1. **监控新投稿**：抓 UP 主（默认「哔哩哔哩电影」mid=15773384）最新发布，从标题提片名。
2. **分批回扫历史**：沿动态流向前翻页，每次处理若干批，逐部补齐历史投稿。
3. **查豆瓣评分**：先查本地索引，未命中再联网（多变体匹配 + legacy 兜底）。
4. **达标自动收藏**：评分 ≥ 7.8 加入指定收藏夹，低分/未收录/暂无评分进待补评队列，后续逐轮重查。

## 设计上几个关键点

- **不重复收藏**：收藏前查 `is_favorited`，并用 `state.json` 的 `seen` 记账；接口返回 11201（已在夹内）按成功处理。
- **版本去重**：同一部影片有多个投稿版本（未标注/国语/粤语）时只留评分最高的一版。
- **限流不误判**：「连续 N 部查无」不等于豆瓣限流——这类片源约七成是真未收录。用知名影片探针确认接口存活后才判定，避免每天误判提前收尾、白白少扫大片。
- **游标不回退**：限流片入待补评队列并写 `seen`，回扫游标只前进，不会反复扫同一段。

## 快速开始

```bash
# 1. 准备配置
cp assets/config.example.json scripts/config.json
# 填入 cookie.SESSDATA / cookie.bili_jct / douban_cookie

# 2. 跑一轮
cd scripts && python3 run_daily.py

# 3. 需要时批量补查待补评影片
python3 retry_pending_batch.py 45
```

## 目录

```
SKILL.md                     Agent Skill 定义（触发词、机制、排障）
scripts/monitor.py           主程序
scripts/run_daily.py         定时入口
scripts/build_index.py       由豆瓣 CSV 生成本地索引
scripts/retry_pending_batch.py  批量补查（内置探针）
references/ops-notes.md      运维细节与踩坑记录
assets/config.example.json   配置模板
```

## 注意

- `config.json`、`state.json`、`logs/`、`bili_cookies.txt` 含私密凭证和运行状态，已在 `.gitignore` 中排除，请勿提交。
- 脚本依赖 B 站与豆瓣的非官方网页接口，仅适合个人自用的小频率抓取；接口随时可能变动或被风控，请自行控制频率。
- 未做代理、验证码、风控绕过等处理。
