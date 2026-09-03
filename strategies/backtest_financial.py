#!/usr/bin/env python3
"""v5 财务因子实证回测 —— 净利增速 / 营收增速 / 筹码集中 / 龙虎榜。

背景：v6 两个新资金面因子(融资、预告)经实证都是反向指标。本脚本继续实证
      v5 就有的四个老财务因子，看方向是否也对。

方法（事件研究 + IC 检验，按公告日对齐，控制 beta）：
  财务因子是季频"事件型"数据——因子值在 ANN_DATE(公告日)才被市场看见。
  关键：必须按公告日对齐未来收益，不能用报告期末日(否则是未来函数)。

  对每个报告期 t（标准合并口径 STATEMENT_TYPE=1）：
    · 因子值：
        A 净利增速 = NET_PRO_EXCL_MIN_INT_INC 同比(扣非归母，去低基数/扭亏失真)
        B 营收增速 = OPERA_REV 同比
        C 筹码集中 = HOLDER_NUM 股东户数环比变化(负=集中)
    · 未来收益 = 公告日 ANN_DATE 之后 20/60 日超额收益(个股−沪深300)
    · IC 检验：因子值 vs 未来收益的 Spearman 秩相关
    · 分层：按因子分5档看未来收益单调性

  龙虎榜因子D是离散事件(上榜与否)，单独做分组对照：
    近90日上龙虎榜的票 vs 未上榜的票，未来收益对比。

数据源：星耀 AmazingData
  · get_income -> dict{code:DataFrame}，字段 NET_PRO_EXCL_MIN_INT_INC/OPERA_REV/
    REPORTING_PERIOD/REPORT_TYPE/ANN_DATE/STATEMENT_TYPE
  · get_holder_num -> dict{code:DataFrame}，字段 HOLDER_ENDDATE/HOLDER_NUM/ANN_DT
  · get_long_hu_bang -> DataFrame，字段 TRADE_DATE/BUY_AMOUNT/SELL_AMOUNT
  · 基准 000300.SH 沪深300 日K

输出：每个连续因子的 IC(20/60日) + 分层表；龙虎榜分组对照。
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


def income_factor(code):
    """返回 [(ann_date:int, 净利增速%, 营收增速%), ...] 按公告日。"""
    try:
        r = ss.INFO.get_income([code], is_local=False)
    except Exception:
        return []
    df = r.get(code) if isinstance(r, dict) else r
    if df is None or not len(df) or "ANN_DATE" not in df.columns:
        return []
    # 合并标准口径
    if "STATEMENT_TYPE" in df.columns:
        df = df[df["STATEMENT_TYPE"].astype(str) == "1"]
    if not len(df):
        return []
    # 按报告期排序，构造去年同期映射
    df = df.sort_values("REPORTING_PERIOD")
    by_period = {}
    for _, row in df.iterrows():
        try:
            rp = int(row["REPORTING_PERIOD"])
        except Exception:
            continue
        by_period[rp] = row
    out = []
    for _, row in df.iterrows():
        try:
            ann = int(row["ANN_DATE"])
            rp = int(row["REPORTING_PERIOD"])
        except Exception:
            continue
        if ann < 20150101:
            continue
        cur_np = _f(row.get("NET_PRO_EXCL_MIN_INT_INC"))
        cur_rev = _f(row.get("OPERA_REV"))
        # 去年同期同报告期
        yy = rp - 10000
        prev = by_period.get(yy)
        if prev is None or cur_np is None or cur_rev is None:
            continue
        prev_np = _f(prev.get("NET_PRO_EXCL_MIN_INT_INC"))
        prev_rev = _f(prev.get("OPERA_REV"))
        np_yoy = None
        rev_yoy = None
        if prev_np not in (None, 0):
            np_yoy = (cur_np / prev_np - 1) * 100
            # 清洗极端值：同比增速超 ±200% 视为低基数/扭亏失真，剔除
            if abs(np_yoy) > 200:
                np_yoy = None
        if prev_rev not in (None, 0):
            rev_yoy = (cur_rev / prev_rev - 1) * 100
            if abs(rev_yoy) > 200:
                rev_yoy = None
        out.append((ann, np_yoy, rev_yoy))
    return out


def holder_factor(code):
    """返回 [(ann_date:int, 户数环比%), ...]。"""
    try:
        r = ss.INFO.get_holder_num([code], is_local=False)
    except Exception:
        return []
    df = r.get(code) if isinstance(r, dict) else r
    if df is None or not len(df) or "HOLDER_NUM" not in df.columns:
        return []
    df = df.sort_values("HOLDER_ENDDATE")
    rows = []
    prev_num = None
    for _, row in df.iterrows():
        ann_field = "ANN_DT" if "ANN_DT" in df.columns else "HOLDER_ENDDATE"
        try:
            ann = int(row[ann_field])
        except Exception:
            continue
        if ann < 20150101:
            continue
        num = _f(row.get("HOLDER_NUM"))
        if num is None or num <= 0:
            continue
        if prev_num is not None and prev_num > 0:
            chg = (num / prev_num - 1) * 100
            if abs(chg) <= 200:   # 清洗极端值
                rows.append((ann, chg))
        prev_num = num
    return rows


def main():
    n_sample = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    ss.init()
    bench_map, bench_dates = load_bench()
    if bench_map is None:
        print("基准拉取失败", file=sys.stderr)
        return

    print("拉全市场列表...", file=sys.stderr)
    uni = ss.fetch_universe()
    codes = [r["code"] for r in uni]
    random.seed(11)
    sample = random.sample(codes, min(n_sample, len(codes)))
    print(f"抽样 {len(sample)} 只...", file=sys.stderr)

    # 累计因子-收益对
    # 结构: factor_name -> {horizon: [(factor_value, excess_return), ...]}
    fac = {h: {"np": [], "rev": [], "holder": []} for h in (20, 60)}

    done = 0
    for code in sample:
        done += 1
        if done % 20 == 0:
            print(f"  进度 {done}/{len(sample)}", file=sys.stderr)
        close_map, dates = fetch_close_map(code)
        if not dates:
            continue
        # 净利/营收
        for ann, np_yoy, rev_yoy in income_factor(code):
            pos = bisect.bisect_left(dates, ann)
            bpos = bisect.bisect_left(bench_dates, ann)
            if pos >= len(dates) or bpos >= len(bench_dates):
                continue
            c0, b0 = close_map[dates[pos]], bench_map[bench_dates[bpos]]
            if c0 <= 0 or b0 <= 0:
                continue
            for h in (20, 60):
                if pos + h < len(dates) and bpos + h < len(bench_dates):
                    ex = (close_map[dates[pos+h]]/c0 - bench_map[bench_dates[bpos+h]]/b0) * 100
                    if np_yoy is not None:
                        fac[h]["np"].append((np_yoy, ex))
                    if rev_yoy is not None:
                        fac[h]["rev"].append((rev_yoy, ex))
        # 筹码
        for ann, chg in holder_factor(code):
            pos = bisect.bisect_left(dates, ann)
            bpos = bisect.bisect_left(bench_dates, ann)
            if pos >= len(dates) or bpos >= len(bench_dates):
                continue
            c0, b0 = close_map[dates[pos]], bench_map[bench_dates[bpos]]
            if c0 <= 0 or b0 <= 0:
                continue
            for h in (20, 60):
                if pos + h < len(dates) and bpos + h < len(bench_dates):
                    ex = (close_map[dates[pos+h]]/c0 - bench_map[bench_dates[bpos+h]]/b0) * 100
                    fac[h]["holder"].append((chg, ex))

    print(f"\n{'='*70}")
    print(f"v5 财务因子实证回测（超额收益，公告日对齐，2015 年以来）")
    print(f"{'='*70}")

    names = {"np": "净利增速(扣非同比)", "rev": "营收增速(同比)",
             "holder": "筹码集中(户数环比)"}
    for key, label in names.items():
        print(f"\n【{label}】")
        print(f"{'期限':<8}{'样本':>8}{'IC':>10}{'方向判定':>12}")
        for h in (20, 60):
            pairs = fac[h][key]
            if len(pairs) < 30:
                print(f"未来{h}日{'':>4}{len(pairs):>8}{'-':>10}{'样本不足':>12}")
                continue
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            ic = spearman_ic(xs, ys)
            verdict = ("有效" if ic is not None and ic > 0.03 else
                       "反向" if ic is not None and ic < -0.03 else "无效")
            print(f"未来{h}日{len(pairs):>8}{ic if ic is None else f'{ic:>10.4f}'}{verdict:>12}")

    # 分层（只对样本最多的 h=60 做净利/筹码，看单调性）
    print(f"\n{'='*70}")
    print("分层回测（按因子值分5档，看未来60日超额是否单调）")
    for key, label in [("np", "净利增速"), ("holder", "筹码集中")]:
        pairs = fac[60][key]
        if len(pairs) < 50:
            continue
        sp = sorted(pairs, key=lambda x: x[0])
        n = len(sp); q = n // 5
        print(f"\n【{label}】未来60日超额，分5档（{n} 对）")
        print(f"{'档位':<8}{'因子区间':>24}{'平均超额':>12}{'胜率':>10}")
        for qi in range(5):
            lo = qi*q; hi = (qi+1)*q if qi < 4 else n
            g = sp[lo:hi]
            rng = f"[{g[0][0]:.1f}, {g[-1][0]:.1f}]"
            vals = [p[1] for p in g]
            wr = sum(1 for v in vals if v > 0)/len(vals)*100
            print(f"Q{qi+1}{rng:>24}{mean(vals):>11.2f}%{wr:>9.1f}%")


if __name__ == "__main__":
    main()
