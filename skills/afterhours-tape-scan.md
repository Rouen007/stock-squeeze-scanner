---
name: afterhours-tape-scan
description: 用 IBKR + nasdaq100_afterhours_scraper.py 扫描 QQQ top-N 权重股盘前/盘后大单 cluster，输出结构化中文报告
triggers:
  - afterhours-tape-scan
  - 盘后大单
  - 盘前大单
  - 机构扫货
  - tape scan
  - 大单扫描
  - 盘后扫描
  - 盘前扫描
---

# Afterhours Tape Scan — IBKR 大单扫描器

扫描 QQQ / Nasdaq-100 权重股的盘前（04:00–09:30 ET）或盘后（16:00–20:00 ET）机构大单 cluster，数据源为 IBKR 全量 SIP consolidated tape。

---

## 环境要求

```
项目路径：<REPO_ROOT>/
脚本：    nasdaq100_afterhours_scraper.py
依赖：    pip install requests ib_insync
IBKR：    IB Gateway 或 TWS 必须在运行
```

---

## Step 1 — 检测 IBKR 端口

运行前先确认哪个端口在监听：

```python
# 快速检测（在项目目录内运行）
cd <REPO_ROOT>
python3 -c "
from nasdaq100_afterhours_scraper import connect_ibkr
for port in [4001, 4002, 7496, 7497]:
    ib, err = connect_ibkr('127.0.0.1', port, 99)
    if ib:
        print(f'✅ port {port} connected')
        ib.disconnect()
        break
    else:
        print(f'❌ port {port}: {err}')
"
```

| 端口 | 用途 |
|------|------|
| 4001 | Live IB Gateway（推荐） |
| 4002 | Paper IB Gateway |
| 7496 | Live TWS |
| 7497 | Paper TWS |

---

## 标的列表（唯一数据源）

标的清单不再写死在这里，统一读取 **`~/.claude/watchlist.txt`**——
这份清单与「每日 setup 扫描」(`setup-scan` 技能) 共用同一份。

增删标的用 `watchlist` 技能（"加 XYZ 到自选" / "从自选删 XYZ" / "看自选"），
或直接 `python3 ~/.claude/skills/watchlist/watchlist.py add XYZ`。改完两个扫描下次跑都会跟着变。

下面的运行命令用 `--tickers $(grep -vE '^\s*#|^\s*$' ~/.claude/watchlist.txt)`
把整份清单展开（命令替换会按空格/换行自动拆成多个参数，不受"shell 变量"陷阱影响），
不用 `--fallback-universe`。

---

## Step 2 — 运行扫描

### 盘后大单（16:00–20:00 ET）

```bash
cd <REPO_ROOT>

# 标的从共享清单 ~/.claude/watchlist.txt 读取（增删用 watchlist 技能）
python3 nasdaq100_afterhours_scraper.py \
  --source ibkr \
  --ibkr-port 4001 \
  --ibkr-client-id 15 \
  --tickers $(grep -vE '^\s*#|^\s*$' ~/.claude/watchlist.txt) \
  --date $(date +%Y-%m-%d) \
  --markettype post \
  --min-cluster-volume 50000 \
  --min-single-trade-size 100000 \
  --min-repeats 2
```

### 盘前大单（04:00–09:30 ET）

```bash
cd <REPO_ROOT>

python3 nasdaq100_afterhours_scraper.py \
  --source ibkr \
  --ibkr-port 4001 \
  --ibkr-client-id 15 \
  --tickers $(grep -vE '^\s*#|^\s*$' ~/.claude/watchlist.txt) \
  --date $(date +%Y-%m-%d) \
  --markettype pre \
  --min-cluster-volume 50000 \
  --min-single-trade-size 100000 \
  --min-repeats 2
```

### 常用参数速查

| 参数 | 默认 | 说明 |
|------|------|------|
| `--date` | 今天 | 交易日期 YYYY-MM-DD |
| `--markettype` | post | post=盘后 / pre=盘前 |
| `--limit` | 50 | 扫描前 N 只 QQQ 权重股 |
| `--fallback-universe` | off | 跳过实时抓取持仓，用内置 top-50 名单 |
| `--min-cluster-volume` | 100000 | cluster 最小总股数 |
| `--min-single-trade-size` | 100000 | 单笔最小股数（即使不成 cluster 也报出） |
| `--min-repeats` | 2 | cluster 最小成交笔数 |
| `--ibkr-client-id` | 1 | 建议用 10，避免冲突 |

---

## Step 3 — 读取结果

输出文件位于：
```
outputs/nasdaq_extended/nasdaq100_top33_<DATE>_<pre|post>_clusters.csv
outputs/nasdaq_extended/nasdaq100_top33_<DATE>_<pre|post>_trades.csv
```

验证成功：stdout 显示 `fetched=33/33`，source 列为 `ibkr`。

> ⚠️ **clusters CSV 只是冰山一角**——只收录"同价位短时间重复打 ≥2 笔"的信号。
> trades CSV 才是全貌，必须两层都分析，不能只看 cluster。

### 两种信号的区别

| 信号类型 | 触发条件 | 暗示含义 |
|---------|---------|---------|
| **Cluster** | 同价位 × 短时间 ≥ 2 笔 × 总量 ≥ 50K 股 | 大单拆碎分批执行，机构隐蔽建仓 |
| **单笔大单** | 单笔名义金额 ≥ $100万（建议阈值） | 机构直接一次性成交，更"暴力"直接 |

### 从 trades CSV 提取单笔大单 + 标的汇总

trades CSV 列结构：`ticker, time, price, size, source`（无 notional 列，需手算 price × size）

```python
import csv
from collections import defaultdict

rows = []
with open('outputs/nasdaq_extended/nasdaq100_top33_<DATE>_<pre|post>_trades.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        price = float(row['price'])
        size = int(float(row['size']))
        rows.append({'ticker': row['ticker'], 'time': row['time'],
                     'price': price, 'size': size, 'notional': price * size})

rows.sort(key=lambda r: r['notional'], reverse=True)

# ① 单笔大单 TOP 40
print(f'{"ticker":<6} {"time":<22} {"price":>8} {"size":>10} {"notional":>14}')
for r in rows[:40]:
    if r['notional'] < 1_000_000: break
    print(f'{r["ticker"]:<6} {r["time"]:<22} {r["price"]:>8.2f} {r["size"]:>10,} ${r["notional"]:>13,.0f}')

# ② 各标的总量汇总
by_ticker = defaultdict(lambda: {'notional':0,'size':0,'trades':0,'max_single':0})
for r in rows:
    t = r['ticker']
    by_ticker[t]['notional'] += r['notional']
    by_ticker[t]['size'] += r['size']
    by_ticker[t]['trades'] += 1
    if r['notional'] > by_ticker[t]['max_single']:
        by_ticker[t]['max_single'] = r['notional']

ranked = sorted(by_ticker.items(), key=lambda x: x[1]['notional'], reverse=True)
print(f'{"ticker":<6} {"总名义金额":>16} {"总股数":>10} {"笔数":>6} {"最大单笔":>14}')
for ticker, d in ranked[:20]:
    print(f'{ticker:<6} ${d["notional"]:>14,.0f} {d["size"]:>10,} {d["trades"]:>6,} ${d["max_single"]:>12,.0f}')
```

---

## Step 4 — 输出结构化中文报告

读取 clusters CSV **和** trades CSV 后，按以下结构整理报告：

### 信号优先级（输出时按此顺序解读，不要平等对待所有维度）

```
① 成交量集中度   ← 最重要：3秒内 $400M 本身就是信息，无论方向
② 价格聚集位置   ← 是否在关键支撑/压力位成交（结合当日收盘价判断）
③ 多标的联动     ← 同一时刻多只出现大单 = 疑似机构程序化扫货
④ 成交后价格漂移 ← 大单之后价格是否顺向走，是最终验证（需结合后续tape）
⑤ Up/Down 方向  ← 最弱维度，仅作参考，单独看几乎无意义
```

> **为什么盘前/盘后信号更纯粹**：盘中大单 = 真实意图 + 期权 delta 对冲噪音混合；
> 盘后期权市场关闭，无做市商 gamma 对冲驱动的机械买卖，每笔大单更接近"主动决策"。

### 报告模板

```
## 📊 <DATE> 盘后/盘前大单扫描 | IBKR SIP Tape

> 信号质量说明：成交量集中 > 多标的联动 > 价格位置 > up/down方向
> 盘前/盘后无期权对冲噪音，大单更接近机构真实意图。
> fetch=XX/33 ✅，source=ibkr

---

### 📊 各标的总量排名（来自 trades CSV，完整全貌）
| 排名 | 标的 | 总名义金额 | 笔数 | 最大单笔 |
（列出前 15，按总名义金额降序）

---

### 🔥 [仅盘后] 16:00 开盘集中清算（MOC 结算段，方向无意义）
| 时间 ET | 标的 | 成交笔 | 股数 | 名义金额 | 方向* |
* 此段所有方向字段忽略，均为 MOC 结算机制产生，非主动信号。

---

### 🎯 高信号大单（单笔 ≥ $200万，主动段）
盘后：16:05 之后；盘前：全段均为主动段，无 MOC 干扰
| 时间 ET | 标的 | 价格 | 股数 | 名义金额 | 解读 |
（包含 cluster 内的笔 + 未成 cluster 的单笔大单，两种都列）

---

### 🔁 Cluster 信号（同价位重复打单，机构隐蔽建仓）
来自 clusters CSV，格式：时间区间 / 价格 / 笔数 / 总量 / 名义金额
（若 cluster CSV 为空或仅1条，需说明"大单以单笔形式出现，未触发 cluster 门槛"）

---

### 🔗 多标的联动（疑似程序化 / 板块轮动）
同一时间窗口（±5分钟）内出现多只大单，列出并标注板块主题。

---

### 📋 关键结论
- 【量级】总量 TOP3 标的 + 最大单笔是哪只
- 【Cluster vs 单笔】本次以哪种信号为主，说明机构执行风格
- 【联动】是否出现跨标的同步，板块方向判断
- 【位置】大单价格是否在关键支撑压力位
- 【开盘关注】盘前大单建仓方向 → 开盘若顺向走则确认有效
- 【方向备注】up/down 仅在中段且量级显著时才值得一提
```

### 方向字段说明（Tick Rule，非K线颜色）

`direction` 基于**逐笔 Tick Rule**（Lee-Ready）：当前成交价高于前一笔 → `up`，低于 → `down`，与K线最终收绿/收红无关。

| direction 值 | 原始含义 | 实际参考价值 |
|-------------|---------|------------|
| `up` | 当前价高于前一笔，主动买单进攻 | 低，需结合量级 |
| `down` | 当前价低于前一笔，主动卖单进攻 | 低，需结合量级 |
| `up-follow` | 下一笔价格更高 | 低 |
| `down-follow` | 下一笔价格更低 | 低 |
| `flat` | 价格不变，被动/对冲成交 | 中性 |

⚠️ **16:00:00–16:00:05 的 cluster 统一标注为 MOC 结算，方向字段全部忽略。**
⚠️ **大单拆单、被动 limit 被吃、spread 跳动都会产生方向噪音，不要孤立解读 up/down。**

---

## 成功标准

- [ ] `fetched=33/33`（33只全部成功拉取）
- [ ] source 列显示 `ibkr`（非 nasdaq fallback）
- [ ] **trades CSV 已分析**（单笔大单 + 各标的总量汇总，不能只看 clusters CSV）
- [ ] clusters CSV 已读取（即使为空也要说明"大单以单笔形式出现，未触发 cluster 门槛"）
- [ ] 报告包含：总量排名 + 高信号单笔大单 + cluster 信号 + 多标的联动 + 关键结论

---

## 已知陷阱

1. **16:00 巨量 down 不是做空**：是 MOC 结算，所有主力权重股都会在收盘瞬间出现，正常现象
2. **cluster CSV 为空 ≠ 盘前/盘后清淡**：cluster 只捕捉同价位重复打单，大量单笔大单不会进 cluster，必须同时看 trades CSV
3. **client-id 冲突**：用 `--ibkr-client-id 20` 避免与用户自身 TWS 连接冲突（僵尸连接会触发 Error 322）
3. **ib_insync 未安装**：`pip install ib_insync`
4. **Gateway 必须运行**：IBKR 不连接时直接报错退出，不会 fallback 到 nasdaq 源（除非加 `--source nasdaq ibkr`）
5. **历史数据限制**：IBKR 免费账户历史 tick 数据最多回溯约 3 天

---

## 黑话映射（用于中文报告）

| 代码 | 别名 |
|------|------|
| AMD | 按摩店 |
| NVDA | 英伟达 |
| TSLA | 拉子 |
| MSFT | 软子 |
