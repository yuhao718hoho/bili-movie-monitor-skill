# 运维与踩坑记录

## 依赖与目录

```
scripts/
├── monitor.py            主程序（配置、抓取、查分、收藏、队列）
├── run_daily.py          定时入口，先查新投稿再回扫 N 批
├── build_index.py        由豆瓣 CSV 生成本地索引 data/douban_index.json
├── retry_pending_batch.py 批量补查待补评影片（带探针，可独立运行）
├── config.json           配置（私密，不入库）
├── state.json            运行状态（seen / pending / backfill_offset），程序自动维护
├── data/douban_index.json 本地片名→评分索引，命中可跳过联网查询
├── logs/YYYY-MM.log      明细日志
└── bili_cookies.txt      由程序从 config 生成，供子进程复用
```

## 获取 B 站 cookie（cookie 失效时）

1. 用 Edge/Chrome 登录 bilibili.com。
2. 只读方式读 cookie：复制浏览器配置到临时目录，另起一个带调试端口的实例，**不打断用户现有窗口**。

```bash
mkdir -p /tmp/edgecdp/Default
cp ~/Library/Application\ Support/Microsoft\ Edge/Local\ State /tmp/edgecdp/
cp -r ~/Library/Application\ Support/Microsoft\ Edge/Default/Network/Cookies /tmp/edgecdp/Default/
open -n -a "Microsoft Edge" --args --remote-debugging-port=9223 --user-data-dir=/tmp/edgecdp \
  --no-first-run --no-default-browser-check https://www.bilibili.com/
```

3. 用 CDP 的 `Network.getAllCookies` 拿 cookie（需 websocket 客户端，系统 python 常无此库，可临时 venv 装 `websocket-client`）。必须用 **page 级** websocket，`/devtools/browser/...` 调该接口无效。
4. 取到后写入 config.json 的 `cookie`，只留 `SESSDATA` 与 `bili_jct`，然后验证：

```python
monitor.http("https://api.bilibili.com/x/web-interface/nav",
             headers={"Referer": "https://www.bilibili.com/"})
# code==0 且 data.isLogin==True 即正常
```

5. 验证通过后 kill 掉带 `--user-data-dir=/tmp/edgecdp` 的进程，端口关闭。

## 豆瓣限流治理

- 豆瓣 rexxar 接口静默限流时返回空结果，与"未收录"表现相同。**不要**用"连续 N 部查无"判定限流。
- 正确做法：`probe_douban()` 查《星际穿越》《泰坦尼克号》，能返回有效评分说明接口活着，此时查无就是真未收录。
- 带登录 cookie（`douban_cookie`）+ 请求间隔 6~10 秒能显著降低触发概率，但只能缓解。
- 批量补查用 `scripts/retry_pending_batch.py`：内置探针，每 8 部复核一次接口，探针失败才停。

## 待补评队列

- 参数：`PENDING_DAYS=30`（超期淘汰）、`PENDING_MAX=200`、`PENDING_RETRY_GAP=12*3600`（同一片最小重查间隔）。
- `retry_pending(state, limit, fid, csrf, max_n=3)`：每轮最多重查 `max_n` 部，`not_found` 连续 2 次则 break。队列积压时可临时调大 `max_n`。
- 入队 `ts` 必须是 `int(time.time())`。写成稿件发布时间会导致历史片一入队即被 30 天规则淘汰。

## 查分命中率

`lookup_douban(name)` 依次尝试多变体：完整名 → core（去掉 `：:·` 等）→ short（去掉尾部标点/数字）→ rexxar 搜空自动退 `douban_legacy` 兜底。
匹配只保留"投稿名 ∈ 条目名"的正向包含（75 分），**不要**加反向包含，否则《碟中谍7：致命清算》会被《碟中谍》顶替。

## 版本去重

标题尾部解析 `ver`（"未标注版本"/"国语版"/"粤语版"等）。同一影片多个版本时只保留 rank 最高一版，其余直接跳过并记日志。

## 日志读取

日志为中文 UTF-8，`sed`/`grep` 可能报 `illegal byte sequence`，用 python 读：

```bash
python3 -c "from pathlib import Path; print(Path('logs/2026-11.log').read_text(encoding='utf-8')[-3000:])"
```

日志按 ` / ` 分隔多条目，按日期 `rfind('2026-11-12')` 可定位某轮。

## 已知死路（不要重试）

- urllib 直连 B 站动态接口必 412，需带 UA 与 Referer 走 `monitor.http()`。
- `monitor.http()` 签名是 `http(url, headers=None, data=None, timeout=25)`，传 `referer=` kwarg 会 TypeError。
- `Cookie` 头不能直接传 dict，需转成 `k=v; k=v` 字符串。
