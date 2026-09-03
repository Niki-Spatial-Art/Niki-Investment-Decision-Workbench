#!/usr/bin/env python3
"""CSRANK 横截面相对强弱升级的实证回测 —— 绝对阈值打分 vs 截面排名打分。

背景
====
v6 财务/资金面因子（营收增速/融资变化/业绩预告）目前用「绝对阈值」打分。
本脚本验证：把绝对数值升级为「同一公告日截面内的相对排名」后，
因子对未来收益的预测力（IC）与分层单调性是否真的更好。

核心对照（关键，避免想当然）：
    方案A（绝对）  ：直接用因子绝对数值（如营收增速 %）算 IC / 分层。
    方案B（截面）  ：每个公告日，在该截面内对因子值做百分位排名（CSRANK），
                     用「排名」而非「绝对值」算 IC / 分层。

若方案B 的 IC 或分层单调性显著优于方案A，说明"相对强弱"确有意义，值得启用；
否则 CSRANK 升级无收益，维持绝对打分。

方法（复用 backtest_financial.py 的事件研究 + IC 框架）：
    · 因子值在 ANN_DATE（公告日）才被市场看见，按公告日对齐未来收益；
    · 未来收益 = 公告日后 20/60 日超额收益（个股 − 沪深300）；
    · IC = Spearman 秩相关（因子值 vs 未来超额收益）；
    · 分层 = 按因子分 5 档看未来收益单调性。

关键实现点：
    · 截面排名必须「按公告日分组」——同一公告日内的多只票才构成一个截面，
      不能把跨公告日的票混在一起排名（否则引入未来函数/时序污染）。
    · 因此先收集「同一公告日」的因子-收益对，再在截面内做排名。

数据源：星耀 AmazingData（get_income 营收增速为主验证因子；融资/预告因子
    为反向指标，方向已由 backtest_factors/forecast 实证，本脚本聚焦
    "截面排名是否优于绝对阈值"这一通用问题，用营收增速+净利增速做代表）。

输出：每个因子在「绝对」与「截面」两种方案下的 IC(20/60日) + 分层表对比。
"""
import sys, os, random, bisect
from statistics import mean
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_screen_v6 as ss


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except Exception:
        return None


def fetch_close_map(code, n=900):
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
    """返回 [(ann_date, 净利增速%, 营收增速%), ...] 按公告日，清洗极端值。"""
    try:
        r = ss.INFO.get_income([code], is_local=False)
    except Exception:
        return []
    df = r.get(code) if isinstance(r, dict) else r
    if df is None or not len(df) or "ANN_DATE" not in df.columns:
        return []
    if "STATEMENT_TYPE" in df.columns:
        df = df[df["STATEMENT_TYPE"].astype(str) == "1"]
    if not len(df):
        return []
    df = df.sort_values("REPORTING_PERIOD")
    by_period = {}
    for _, row in df.iterrows():
        try:
            by_period[int(row["REPORTING_PERIOD"])] = row
        except Exception:
            continue
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
        prev = by_period.get(rp - 10000)
        if prev is None or cur_np is None or cur_rev is None:
            continue
        prev_np = _f(prev.get("NET_PRO_EXCL_MIN_INT_INC"))
        prev_rev = _f(prev.get("OPERA_REV"))
        np_yoy = rev_yoy = None
        if prev_np not in (None, 0):
            np_yoy = (cur_np / prev_np - 1) * 100
            if abs(np_yoy) > 200:
                np_yoy = None
        if prev_rev not in (None, 0):
            rev_yoy = (cur_rev / prev_rev - 1) * 100
            if abs(rev_yoy) > 200:
                rev_yoy = None
        out.append((ann, np_yoy, rev_yoy))
    return out


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


def cross_section_rank(pairs_by_date):
    """把 {ann_date: [(abs_value, ex_ret), ...]} 在截面内做百分位排名。

    返回 [(pct_rank(0~1), ex_ret), ...]，pct_rank 越大 = 该截面内因子值越强。
    """
    out = []
    for ann, pairs in pairs_by_date.items():
        if len(pairs) < 3:  # 截面样本太少，排名无意义
            continue
        vals = [p[0] for p in pairs]
        # 截面内排序 -> 百分位
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        pct = [0.0] * len(vals)
        for rank_i, idx in enumerate(order):
            pct[idx] = (rank_i + 1) / len(vals)
        for i, p in enumerate(pairs):
            out.append((pct[i], p[1]))
    return out


def layered(pairs, nq=5):
    """按因子值分 nq 档，返回 [(档位, 平均超额, 胜率, 区间), ...]。"""
    if len(pairs) < nq * 5:
        return None
    sp = sorted(pairs, key=lambda x: x[0])
    n = len(sp); q = n // nq
    rows = []
    for qi in range(nq):
        lo = qi * q; hi = (qi + 1) * q if qi < nq else n
        g = sp[lo:hi]
        vals = [p[1] for p in g]
        wr = sum(1 for v in vals if v > 0) / len(vals) * 100
        rows.append((qi + 1, mean(vals), wr, g[0][0], g[-1][0]))
    return rows


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
    random.seed(11)
    sample = random.sample(codes, min(n_sample, len(codes)))
    print(f"抽样 {len(sample)} 只...", file=sys.stderr)

    # 收集「同一公告日」的因子-超额收益对
    # by_factor[ann_date] -> {"rev": [(rev_yoy, ex20, ex60), ...], "np": [...]}
    # 为两种方案分别积累：
    #   绝对方案：直接 (abs_value, ex_ret) 对
    #   截面方案：按公告日分组后再截面排名
    abs_pairs = {h: {"rev": [], "np": []} for h in (20, 60)}
    cs_by_date = {h: {"rev": defaultdict(list), "np": defaultdict(list)} for h in (20, 60)}

    done = 0
    for code in sample:
        done += 1
        if done % 30 == 0:
            print(f"  进度 {done}/{len(sample)}", file=sys.stderr)
        close_map, dates = fetch_close_map(code)
        if not dates:
            continue
        for ann, np_yoy, rev_yoy in income_factor(code):
            pos = bisect.bisect_left(dates, ann)
            bpos = bisect.bisect_left(bench_dates, ann)
            if pos >= len(dates) or bpos >= len(bench_dates):
                continue
            c0 = close_map[dates[pos]]; b0 = bench_map[bench_dates[bpos]]
            if c0 <= 0 or b0 <= 0:
                continue
            for h in (20, 60):
                if pos + h >= len(dates) or bpos + h >= len(bench_dates):
                    continue
                ex = (close_map[dates[pos+h]]/c0 - bench_map[bench_dates[bpos+h]]/b0) * 100
                if rev_yoy is not None:
                    abs_pairs[h]["rev"].append((rev_yoy, ex))
                    cs_by_date[h]["rev"][ann].append((rev_yoy, ex))
                if np_yoy is not None:
                    abs_pairs[h]["np"].append((np_yoy, ex))
                    cs_by_date[h]["np"][ann].append((np_yoy, ex))

    print(f"\n{'='*78}")
    print(f"CSRANK 相对强弱 vs 绝对阈值 对照回测（营收/净利增速，2015以来，超额收益）")
    print(f"{'='*78}")

    names = {"rev": "营收增速(同比)", "np": "净利增速(扣非同比)"}
    for key, label in names.items():
        print(f"\n【{label}】")
        print(f"{'方案':<8}{'期限':<8}{'样本':>8}{'IC':>10}{'判定':>10}")
        for h in (20, 60):
            # 绝对方案
            ap = abs_pairs[h][key]
            ic_abs = spearman_ic([p[0] for p in ap], [p[1] for p in ap]) if len(ap) >= 20 else None
            # 截面方案
            cp = cross_section_rank(cs_by_date[h][key])
            ic_cs = spearman_ic([p[0] for p in cp], [p[1] for p in cp]) if len(cp) >= 20 else None
            def verdict(ic):
                return ("有效" if ic is not None and ic > 0.03 else
                        "反向" if ic is not None and ic < -0.03 else "无效")
            print(f"{'绝对':<8}{'未来'+str(h)+'日':<8}{len(ap):>8}"
                  f"{(ic_abs if ic_abs is None else f'{ic_abs:.4f}'):>10}{verdict(ic_abs):>10}")
            print(f"{'截面':<8}{'未来'+str(h)+'日':<8}{len(cp):>8}"
                  f"{(ic_cs if ic_cs is None else f'{ic_cs:.4f}'):>10}{verdict(ic_cs):>10}")

        # 分层对照（h=60）
        print(f"\n  分层对照（未来60日，5档）")
        h = 60
        ap = abs_pairs[h][key]
        cp = cross_section_rank(cs_by_date[h][key])
        la = layered(ap)
        lc = layered(cp)
        if la:
            print(f"  【绝对阈值】{len(ap)} 对")
            print(f"  {'档':<4}{'平均超额':>10}{'胜率':>10}")
            for qi, avg, wr, lo, hi in la:
                print(f"  Q{qi:<3}{avg:>9.2f}%{wr:>9.1f}%")
        if lc:
            print(f"  【截面排名】{len(cp)} 对")
            print(f"  {'档':<4}{'平均超额':>10}{'胜率':>10}")
            for qi, avg, wr, lo, hi in lc:
                print(f"  Q{qi:<3}{avg:>9.2f}%{wr:>9.1f}%")


if __name__ == "__main__":
    main()
