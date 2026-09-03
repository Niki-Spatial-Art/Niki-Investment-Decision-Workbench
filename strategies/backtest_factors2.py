#!/usr/bin/env python3
"""v6 因子实证二期 —— 龙虎榜因子 + ROE 因子。

背景：v6 权重校准后，还剩下两个待实证的因子：
  · 龙虎榜（当前保守 3 分，方向未确认）
  · ROE（get_balance_sheet 接口已恢复，待实证是否纳入）

方法（事件研究法，按事件日/公告日对齐未来收益，控制 beta）：
  因子都是"事件型"数据，因子值在事件发生日才被市场看见，必须按事件日对齐，
  且用超额收益(个股 − 同期沪深300)控制 beta，绝对收益会被牛熊系统性拉低。

  【龙虎榜因子】
    上榜本身=异动事件(小盘/题材股专属，样本天然高波动)。
    实证两个问题：
      Q1 方向上榜：上榜日后 20/60 日超额收益是正还是负？
      Q2 净买入 vs 净卖出：净买入(买>卖) 与 净卖出(卖>买) 的票，未来收益有无差异？
    龙虎榜一日多行(不同营业部席位)，需按 TRADE_DATE+MARKET_CODE 聚合：
      每日净额 = sum(BUY_AMOUNT) - sum(SELL_AMOUNT)

  【ROE 因子】
    ROE = 扣非归母净利 / 归母净资产，用 TTM(滚动12月) 或单季，这里用年报/单期
    简化：ROE = NET_PRO_EXCL_MIN_INT_INC(扣非归母净利) / TOT_SHARE_EQUITY_INCL_MIN_INT(归母净资产)
    按公告日 ANN_DATE 对齐未来收益，IC 检验 + 分层看单调性。

数据源：星耀 AmazingData
  · get_long_hu_bang(code_list, is_local=False) -> DataFrame
      字段 MARKET_CODE/TRADE_DATE/BUY_AMOUNT/SELL_AMOUNT/REASON_TYPE/CHANGE_RANGE
  · get_balance_sheet(code_list, is_local=False) -> dict{code:DataFrame}
      字段 REPORTING_PERIOD/ANN_DATE/STATEMENT_TYPE/TOT_SHARE_EQUITY_INCL_MIN_INT
  · get_income -> 扣非归母净利 NET_PRO_EXCL_MIN_INT_INC
  · 基准 000300.SH 沪深300 日K

输出：龙虎榜分组对照 + ROE 的 IC(20/60日) + 分层表。
"""
import sys, os, random, bisect
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_screen_v6 as ss


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except Exception:
        return None


def fetch_close_map(code, n=900):
    """单票日K -> ({date:int:close}, [有序date])。"""
    try:
        end = int(str(ss.CAL[-1]))
        begin = int(str(ss.CAL[-n]))
        r = ss.MARKET.query_kline(code_list=[code], begin_date=begin,
                                  end_date=end, period=10008)
    except Exception:
        return None, None
    df = r.get(code) if isinstance(r, dict) else r
    if df is None or not len(df):
        return None, None
    rows = []
    for _, row in df.iterrows():
        try:
            td = int(str(row["kline_time"]).split()[0].replace("-", ""))
            rows.append((td, float(row["close"])))
        except Exception:
            continue
    rows.sort()
    return {td: c for td, c in rows}, [td for td, _ in rows]


def spearman_ic(xs, ys):
    n = len(xs)
    if n < 20:
        return None
    def rank(a):
        s = sorted(range(n), key=lambda i: a[i])
        r = [0] * n
        for i in range(n):
            r[s[i]] = i
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = mean(rx), mean(ry)
    cov = sum((rx[i]-mx)*(ry[i]-my) for i in range(n)) / n
    sx = (sum((x-mx)**2 for x in rx)/n) ** 0.5
    sy = (sum((y-my)**2 for y in ry)/n) ** 0.5
    if sx == 0 or sy == 0:
        return None
    return cov / (sx * sy)


def load_bench():
    try:
        r = ss.MARKET.query_kline(code_list=["000300.SH"], begin_date=20150101,
                                  end_date=int(str(ss.CAL[-1])), period=10008)
        df = r.get("000300.SH") if isinstance(r, dict) else r
        m, ds = {}, []
        for _, row in df.iterrows():
            td = int(str(row["kline_time"]).split()[0].replace("-", ""))
            m[td] = float(row["close"])
            ds.append(td)
        ds.sort()
        return m, ds
    except Exception:
        return None, None


def lhb_factor(code):
    """龙虎榜：返回 [(上榜日:int, 净额:float, 净买标记:bool), ...] 按日聚合。

    净额 = sum(BUY_AMOUNT) - sum(SELL_AMOUNT)，>0 净买入。
    """
    try:
        r = ss.INFO.get_long_hu_bang([code], is_local=False)
    except Exception:
        return []
    df = r if hasattr(r, "columns") else (r.get(code) if isinstance(r, dict) else None)
    if df is None or not len(df) or "TRADE_DATE" not in df.columns:
        return []
    # 按日聚合
    daily = {}
    for _, row in df.iterrows():
        try:
            td = int(row["TRADE_DATE"])
        except Exception:
            continue
        if td < 20150101:
            continue
        buy = _f(row.get("BUY_AMOUNT")) or 0.0
        sell = _f(row.get("SELL_AMOUNT")) or 0.0
        agg = daily.setdefault(td, [0.0, 0.0])
        agg[0] += buy
        agg[1] += sell
    out = []
    for td in sorted(daily):
        buy, sell = daily[td]
        net = buy - sell
        out.append((td, net, net > 0))
    return out


def roe_factor(code):
    """ROE：返回 [(公告日:int, ROE%), ...]。

    ROE = 扣非归母净利 / 归母净资产(TOT_SHARE_EQUITY_INCL_MIN_INT) * 100。
    只用合并标准口径 STATEMENT_TYPE=1，按报告期对齐，公告日对齐收益。
    """
    try:
        bs = ss.INFO.get_balance_sheet([code], is_local=False)
        inc = ss.INFO.get_income([code], is_local=False)
    except Exception:
        return []
    bsdf = bs.get(code) if isinstance(bs, dict) else bs
    incdf = inc.get(code) if isinstance(inc, dict) else inc
    if bsdf is None or not len(bsdf) or "ANN_DATE" not in bsdf.columns:
        return []
    if incdf is None or not len(incdf):
        return []
    # 只取合并标准口径
    if "STATEMENT_TYPE" in bsdf.columns:
        bsdf = bsdf[bsdf["STATEMENT_TYPE"].astype(str) == "1"]
    if "STATEMENT_TYPE" in incdf.columns:
        incdf = incdf[incdf["STATEMENT_TYPE"].astype(str) == "1"]
    # 净利按报告期建索引
    np_by_period = {}
    for _, row in incdf.iterrows():
        try:
            rp = int(row["REPORTING_PERIOD"])
        except Exception:
            continue
        v = _f(row.get("NET_PRO_EXCL_MIN_INT_INC"))
        if v is not None:
            np_by_period[rp] = v
    out = []
    for _, row in bsdf.iterrows():
        try:
            ann = int(row["ANN_DATE"])
            rp = int(row["REPORTING_PERIOD"])
        except Exception:
            continue
        if ann < 20150101:
            continue
        equity = _f(row.get("TOT_SHARE_EQUITY_INCL_MIN_INT"))
        np_val = np_by_period.get(rp)
        if equity is None or equity <= 0 or np_val is None:
            continue
        roe = np_val / equity * 100
        # 清洗极端 ROE（净资产为负/接近0 或 异常）
        if abs(roe) > 80:
            continue
        out.append((ann, roe))
    return out


def _excess_return(close_map, dates, bench_map, bench_dates, ann, h):
    """公告日 ann 之后 h 日的超额收益(%)。"""
    pos = bisect.bisect_left(dates, ann)
    bpos = bisect.bisect_left(bench_dates, ann)
    if pos >= len(dates) or bpos >= len(bench_dates):
        return None
    c0, b0 = close_map[dates[pos]], bench_map[bench_dates[bpos]]
    if c0 <= 0 or b0 <= 0:
        return None
    if pos + h >= len(dates) or bpos + h >= len(bench_dates):
        return None
    return (close_map[dates[pos+h]]/c0 - bench_map[bench_dates[bpos+h]]/b0) * 100


def main():
    n_sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    ss.init()
    bench_map, bench_dates = load_bench()
    if bench_map is None:
        print("基准拉取失败", file=sys.stderr)
        return

    print("拉全市场列表...", file=sys.stderr)
    uni = ss.fetch_universe()
    codes = [r["code"] for r in uni]
    random.seed(12)
    sample = random.sample(codes, min(n_sample, len(codes)))
    print(f"抽样 {len(sample)} 只...", file=sys.stderr)

    # 龙虎榜分组：net_buy -> {h: [超额收益]}, net_sell -> {h: [...]}
    lhb = {"buy": {20: [], 60: []}, "sell": {20: [], 60: []}}
    # ROE: {h: [(roe, excess), ...]}
    roe = {20: [], 60: []}

    done = 0
    for code in sample:
        done += 1
        if done % 25 == 0:
            print(f"  进度 {done}/{len(sample)}", file=sys.stderr)
        close_map, dates = fetch_close_map(code)
        if not dates:
            continue
        # 龙虎榜
        for td, net, is_buy in lhb_factor(code):
            for h in (20, 60):
                ex = _excess_return(close_map, dates, bench_map, bench_dates, td, h)
                if ex is not None:
                    lhb["buy" if is_buy else "sell"][h].append(ex)
        # ROE
        for ann, roe_val in roe_factor(code):
            for h in (20, 60):
                ex = _excess_return(close_map, dates, bench_map, bench_dates, ann, h)
                if ex is not None:
                    roe[h].append((roe_val, ex))

    print(f"\n{'='*70}")
    print(f"v6 因子实证二期（龙虎榜 + ROE，超额收益，2015 年以来）")
    print(f"{'='*70}")

    # 龙虎榜分组对照
    print(f"\n【龙虎榜因子】上榜日后超额收益：净买入 vs 净卖出")
    print(f"{'期限':<8}{'净买入组':>14}{'净买入胜率':>14}{'净卖出组':>14}{'净卖出胜率':>14}{'差异':>12}")
    for h in (20, 60):
        b = lhb["buy"][h]
        s = lhb["sell"][h]
        if len(b) < 10 or len(s) < 10:
            print(f"未来{h}日{'样本不足':>40}")
            continue
        mb, ms = mean(b), mean(s)
        wb = sum(1 for v in b if v > 0)/len(b)*100
        ws = sum(1 for v in s if v > 0)/len(s)*100
        diff = mb - ms
        print(f"未来{h}日{'':>4}{mb:>13.2f}%{wb:>13.1f}%{ms:>13.2f}%{ws:>13.1f}%{diff:>+11.2f}%")
    print(f"  (净买入组 n={len(lhb['buy'][20])}, 净卖出组 n={len(lhb['sell'][20])}，未来20日口径)")

    # ROE IC + 分层
    print(f"\n【ROE 因子】IC 检验")
    print(f"{'期限':<8}{'样本':>8}{'IC':>10}{'方向判定':>12}")
    for h in (20, 60):
        pairs = roe[h]
        if len(pairs) < 30:
            print(f"未来{h}日{'':>4}{len(pairs):>8}{'-':>10}{'样本不足':>12}")
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        ic = spearman_ic(xs, ys)
        verdict = ("有效" if ic is not None and ic > 0.03 else
                   "反向" if ic is not None and ic < -0.03 else "无效")
        print(f"未来{h}日{len(pairs):>8}{ic if ic is None else f'{ic:>10.4f}'}{verdict:>12}")

    # ROE 分层
    pairs = roe[60]
    if len(pairs) >= 50:
        sp = sorted(pairs, key=lambda x: x[0])
        n = len(sp); q = n // 5
        print(f"\n【ROE 分层】未来60日超额，分5档（{n} 对）")
        print(f"{'档位':<8}{'ROE区间':>24}{'平均超额':>12}{'胜率':>10}")
        for qi in range(5):
            lo = qi*q; hi = (qi+1)*q if qi < 4 else n
            g = sp[lo:hi]
            rng = f"[{g[0][0]:.1f}, {g[-1][0]:.1f}]"
            vals = [p[1] for p in g]
            wr = sum(1 for v in vals if v > 0)/len(vals)*100
            print(f"Q{qi+1}{rng:>24}{mean(vals):>11.2f}%{wr:>9.1f}%")


if __name__ == "__main__":
    main()
