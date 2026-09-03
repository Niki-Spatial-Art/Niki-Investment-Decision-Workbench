#!/usr/bin/env python3
"""回踩买点信号重验 —— 超额收益法（个股−沪深300）控制 beta。

背景：v3 回踩因子（乖离 MA20 ∈ [-4%, +6%]）旧回测 backtest_bias.py 用「绝对收益」，
      未控制市场 beta，且未验证更细的确认信号。本脚本修正这两点：

方法（历史采样 + 事件研究）：
  1. 对每只抽样票，沿历史交易日逐日扫描（每日一次，采样间隔 step 天），
     找到「回踩买点候选」：当日收盘站上 MA20 且乖离 MA20 ∈ 回踩区间。
  2. 未来收益 = 未来 5/10/20 日「超额收益」= 个股累计涨幅 − 沪深300累计涨幅，
     消除大盘 beta 影响（这是相对旧回测的核心修正）。
  3. 确认信号（子条件）逐项测试，看哪个组合胜率/超额最高：
       a. 缩量企稳：近5日均量 / 近20日均量 < 0.85（回踩不放量）
       b. 不破位：回踩当日收盘 > 前一日 MA10（未跌破短中期均线）
       c. 回踩深度分档：浅回踩 [0,+6%]、中回踩 [-2,+2%]、深回踩 [-4,-2%]
       d. 站回 MA5：回踩当日收盘 > MA5（短线已企稳）
  4. 对照组：乖离 MA20 > 8%（追高区），看追高的超额表现作为基线。

数据源：星耀 AmazingData（复用 stock_screen_v6 的 ss.init / MARKET / TSF）
  · MARKET.query_kline -> DataFrame，字段 close/high/low/volume/amount/kline_time
  · 基准 000300.SH 沪深300 日K

输出：各子条件的 5/10/20 日平均超额收益 + 胜率 + 样本数，找出胜率最高组合。
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


def fetch_ohlc(code, n=900):
    """单票日K -> dict{date:int -> dict(close/high/low/vol)} + 有序日期列表。"""
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
            rec = {"c": float(row["close"])}
            for k in ("high", "low", "volume", "amount"):
                if k in df.columns:
                    v = _f(row[k])
                    if v is not None:
                        rec[k] = v
            rows.append((td, rec))
        except Exception:
            continue
    rows.sort()
    return {td: r for td, r in rows}, [td for td, _ in rows]


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


def ma(seq, n):
    """seq: 浮点列表（按时序，可能含 None），返回末尾 n 个有效值的均值。"""
    valid = [x for x in seq[-n:] if x is not None]
    return sum(valid) / len(valid) if len(valid) >= n else None


def scan_one(code, bench_map, bench_dates, step=5):
    """扫描单只票，返回 [(样本dict, 组标签)]。

    每个样本 dict 含：
      b20: 乖离MA20(%)，fut{h}: 未来h日超额收益(%)，
      vol_ratio: 近5日/近20日均量，above_ma5, above_ma10, 回踩深度标签
    组标签 ∈ {hit(回踩命中), chase(追高对照)}。
    """
    ohlc, dates = fetch_ohlc(code)
    if not dates or len(dates) < 80:
        return []
    closes = [ohlc[d]["c"] for d in dates]
    vols = [ohlc[d].get("volume") for d in dates]

    out = []
    for i in range(20, len(dates) - 20, step):
        c = closes[i]
        m20 = ma(closes[:i + 1], 20)
        if m20 is None or m20 <= 0:
            continue
        b20 = (c / m20 - 1) * 100

        # 未来超额收益（个股 − 沪深300）
        td = dates[i]
        bpos = bisect.bisect_left(bench_dates, td)
        if bpos >= len(bench_dates):
            continue
        b0 = bench_map[bench_dates[bpos]]
        if b0 <= 0:
            continue
        fut = {}
        for h in (5, 10, 20):
            if i + h < len(dates) and bpos + h < len(bench_dates):
                stock_ret = (closes[i + h] / c - 1) * 100
                bench_ret = (bench_map[bench_dates[bpos + h]] / b0 - 1) * 100
                fut[h] = stock_ret - bench_ret

        # 确认信号（子条件）
        v5 = ma(vols[:i + 1], 5)
        v20 = ma(vols[:i + 1], 20)
        vol_ratio = (v5 / v20) if (v5 and v20) else None
        m5 = ma(closes[:i + 1], 5)
        m10 = ma(closes[:i + 1], 10)
        above_ma5 = (m5 is not None and c > m5)
        above_ma10 = (m10 is not None and c > m10)

        # 回踩深度分档（覆盖跌破与站上两类回踩场景）
        if -4.0 <= b20 < -2.0:
            depth = "deep"      # 深破位回踩 [-4,-2)
        elif -2.0 <= b20 < 0:
            depth = "mid_low"   # 浅破位回踩 [-2,0)
        elif 0 <= b20 < 3.0:
            depth = "mid_high"  # 贴线站上回踩 [0,3)
        elif 3.0 <= b20 <= 6.0:
            depth = "shallow"   # 高位站上回踩 [3,6]
        else:
            depth = None

        sample = {"b20": b20, "fut": fut, "vol_ratio": vol_ratio,
                  "above_ma5": above_ma5, "above_ma10": above_ma10, "depth": depth}

        # 命中组：回踩区间 [-4,+6]（不再强制站上MA20，覆盖跌破/站上两类回踩）
        if -4.0 <= b20 <= 6.0:
            out.append((sample, "hit"))
        # 对照组：追高（乖离>8%，不要求站上MA20，天然站上）
        elif b20 > 8.0:
            out.append((sample, "chase"))
    return out


def summarize(samples):
    """samples: list[sample]，返回 {h: (mean_excess, winrate, n)}。"""
    res = {}
    for h in (5, 10, 20):
        vals = [s["fut"][h] for s in samples if h in s["fut"]]
        if len(vals) < 20:
            res[h] = (None, None, len(vals))
            continue
        wr = sum(1 for v in vals if v > 0) / len(vals) * 100
        res[h] = (mean(vals), wr, len(vals))
    return res


def fmt_row(label, summ):
    cells = [label]
    for h in (5, 10, 20):
        m, wr, n = summ[h]
        if m is None:
            cells.append(f"{'—':>18}")
        else:
            cells.append(f"{m:>+6.2f}%/{wr:>5.1f}%({n})")
    return "  ".join(cells)


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
    random.seed(42)
    sample = random.sample(codes, min(n_sample, len(codes)))
    print(f"抽样 {len(sample)} 只，逐日扫描回踩买点...", file=sys.stderr)

    hit = []      # 全部回踩命中
    chase = []    # 追高对照
    # 子条件分组
    grp_vol_shrink = []   # 缩量企稳（量比<0.85）
    grp_vol_norm = []     # 非缩量（量比>=0.85）
    grp_hold10 = []       # 不破位（c>MA10）
    grp_break10 = []      # 破位（c<=MA10）
    grp_above5 = []       # 站回MA5
    grp_depth = {"deep": [], "mid_low": [], "mid_high": [], "shallow": []}

    done = 0
    for code in sample:
        done += 1
        if done % 50 == 0:
            print(f"  进度 {done}/{len(sample)}", file=sys.stderr)
        try:
            pairs = scan_one(code, bench_map, bench_dates)
        except Exception:
            pairs = []
        for samp, label in pairs:
            if label == "chase":
                chase.append(samp)
                continue
            hit.append(samp)
            if samp["vol_ratio"] is not None:
                (grp_vol_shrink if samp["vol_ratio"] < 0.85 else grp_vol_norm).append(samp)
            if samp["above_ma10"]:
                grp_hold10.append(samp)
            else:
                grp_break10.append(samp)
            if samp["above_ma5"]:
                grp_above5.append(samp)
            if samp["depth"]:
                grp_depth[samp["depth"]].append(samp)

    print(f"\n{'='*76}")
    print(f"回踩买点信号重验（超额收益法，个股−沪深300，2015年以来）")
    print(f"{'='*76}")
    print(f"回踩命中样本 {len(hit)}，追高对照样本 {len(chase)}\n")
    print("表头格式：平均超额收益 / 胜率 / (样本数)")
    print(f"{'组别':<14}{'未来5日':>20}{'未来10日':>20}{'未来20日':>20}")
    print("-" * 76)
    print(fmt_row("① 追高对照(>8%)", summarize(chase)))
    print(fmt_row("② 回踩命中(全部)", summarize(hit)))
    print("-" * 76)
    print(fmt_row("③ 缩量企稳(<0.85)", summarize(grp_vol_shrink)))
    print(fmt_row("④ 非缩量(>=0.85)", summarize(grp_vol_norm)))
    print("-" * 76)
    print(fmt_row("⑤ 不破位(c>MA10)", summarize(grp_hold10)))
    print(fmt_row("⑥ 破位(c<=MA10)", summarize(grp_break10)))
    print(fmt_row("⑦ 站回MA5", summarize(grp_above5)))
    print("-" * 76)
    depth_label = {"deep": "深破位[-4,-2)", "mid_low": "浅破位[-2,0)",
                   "mid_high": "贴线[0,3)", "shallow": "高位[3,6]"}
    for dk in ("deep", "mid_low", "mid_high", "shallow"):
        print(fmt_row(f"⑧ {depth_label[dk]}", summarize(grp_depth[dk])))
    print("-" * 76)

    # 二维交互：回踩深度 × 不破位(MA10)，找出精确买点组合
    print("-" * 76)
    print("【二维交互】回踩深度 × 不破位(MA10)：")
    for dk in ("deep", "mid_low", "mid_high", "shallow"):
        hold = [s for s in grp_depth[dk] if s["above_ma10"]]
        brk = [s for s in grp_depth[dk] if not s["above_ma10"]]
        print(fmt_row(f"  {depth_label[dk]}+不破位", summarize(hold)))
        print(fmt_row(f"  {depth_label[dk]}+破位", summarize(brk)))

    # 组合信号：缩量 + 不破位 + 站回MA5（最严苛确认）
    combo = [s for s in hit
             if s["vol_ratio"] is not None and s["vol_ratio"] < 0.85
             and s["above_ma10"] and s["above_ma5"]]
    print(fmt_row("⑨ 缩量+不破位+站MA5", summarize(combo)))
    print(f"\n结论解读：对比②(回踩)与①(追高)的超额差，看回踩因子在剔除大盘 beta 后是否仍有超额；")
    print("         再对比③~⑨各子条件，找胜率最高、超额最稳的回踩买点确认组合。")


if __name__ == "__main__":
    main()
