#!/usr/bin/env python3
"""因子权重实证化回测 —— 验证 v6 新增资金面/财务因子的真实预测力。

方法：IC 检验 + 分层回测（双层验证）
  1. IC 检验：因子值(融资余额近20日变化率) 与 未来N日收益 的 Spearman 秩相关
     IC > 0 且 |IC| 稳定 = 因子有正向预测力；IC ≈ 0 = 因子无效，应降权/剔除
  2. 分层回测：按因子值分 5 档(Q1最低~Q5最高)，看未来收益是否单调递增
     好因子：Q5 收益显著 > Q1；坏因子：无单调性或反向

数据源：星耀数智 AmazingData
  · get_margin_detail -> dict{code: DataFrame}，TRADE_DATE 覆盖 2010~今(16年历史)
    字段 BORROW_MONEY_BALANCE(融资余额存量，核心因子)
  · 未来收益用 MarketData.query_kline 复权日K 计算

抽样策略：随机抽 200 只（全市场 5200+ 只 × 16 年融资数据太吃流量/时间）
  每只票按月采样(约21交易日)取因子-收益对，避免同一票重复采样导致伪相关

输出：
  1. 全样本 IC(5/10/20日) 及 t 值
  2. 分层回测表：Q1~Q5 各档未来5/10/20日均涨 + 胜率
  3. 因子有效性结论（该给多少权重）
"""
import sys, os, random, math
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_screen_v6 as ss


def spearman_ic(xs, ys):
    """Spearman 秩相关系数（IC）。输入等长 list。"""
    n = len(xs)
    if n < 20:
        return None, None
    def rank(a):
        s = sorted(range(n), key=lambda i: a[i])
        r = [0] * n
        for i in range(n):
            r[s[i]] = i
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = mean(rx), mean(ry)
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n)) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in rx) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ry) / n)
    if sx == 0 or sy == 0:
        return None, None
    ic = cov / (sx * sy)
    # t 值近似（大样本下 IC*sqrt(n) 近似标准正态，这里给个经验 t）
    t = ic * math.sqrt(n - 2) / math.sqrt(max(1 - ic * ic, 1e-9))
    return ic, t


def fetch_margin_history(code):
    """拉单票融资余额历史，返回 {TRADE_DATE(int): balance(float)}。"""
    try:
        r = ss.INFO.get_margin_detail([code], is_local=False)
    except Exception:
        return None
    if isinstance(r, dict):
        for c, df in r.items():
            if hasattr(df, "columns") and len(df) and "TRADE_DATE" in df.columns:
                d = {}
                for _, row in df.iterrows():
                    td = int(row["TRADE_DATE"])
                    bal = row.get("BORROW_MONEY_BALANCE")
                    if bal is None or bal != bal:  # NaN
                        continue
                    d[td] = float(bal)
                return d if d else None
            return None
    elif hasattr(r, "columns") and len(r):
        df = r[r["MARKET_CODE"] == code] if "MARKET_CODE" in r.columns else r
        if "TRADE_DATE" in df.columns:
            d = {}
            for _, row in df.iterrows():
                d[int(row["TRADE_DATE"])] = float(row["BORROW_MONEY_BALANCE"])
            return d if d else None
    return None


def fetch_close_history(code, n=400):
    """拉单票日K收盘，返回 [(trade_date:int, close:float), ...] 按日期升序。

    星耀 query_kline 返回 dict{code: DataFrame}，列 kline_time(日期字符串)/close。
    """
    try:
        CAL = ss.CAL
        end = int(str(CAL[-1]))
        begin = int(str(CAL[-n]))
        r = ss.MARKET.query_kline(code_list=[code], begin_date=begin,
                                  end_date=end, period=10008)
    except Exception:
        return None
    df = None
    if isinstance(r, dict):
        df = r.get(code)
    elif hasattr(r, "columns"):
        df = r
    if df is None or not len(df):
        return None
    closes = []
    for _, row in df.iterrows():
        try:
            # kline_time 格式 "20250110 00:00:00"（带时分），取空格前日期部分
            date_s = str(row["kline_time"]).split()[0]
            td = int(date_s.replace("-", ""))
            c = float(row["close"])
            closes.append((td, c))
        except Exception:
            continue
    closes.sort(key=lambda x: x[0])
    return closes if closes else None


def eval_factor_pairs(code, sample_step=21, horizon_days=(5, 10, 20)):
    """构造单票的因子-收益对。

    因子：融资余额近20日变化率（对齐 v6 因子E 定义）
    收益：未来 5/10/20 日复权收盘涨幅
    采样：每隔 sample_step 个交易日取一个时点，避免同一票过度采样
    """
    margin = fetch_margin_history(code)
    closes = fetch_close_history(code)
    if not margin or not closes:
        return []
    # 按交易日对齐：只用两边都有的日期
    close_map = {td: c for td, c in closes}
    common_dates = sorted(set(margin.keys()) & set(close_map.keys()))
    if len(common_dates) < 60:
        return []
    # 融资余额按日期排序
    bal_dates = sorted(margin.keys())
    bal_map = margin
    pairs = []
    for i in range(20, len(common_dates), sample_step):
        td = common_dates[i]
        # 融资余额近20日变化率：找 td 及之前最近的20个余额交易日
        prev_dates = [d for d in bal_dates if d <= td]
        if len(prev_dates) < 21:
            continue
        # 用最近的21个交易日余额（含当日），基准取最前面的那个
        recent = prev_dates[-21:]
        base = bal_map[recent[0]]
        cur = bal_map[recent[-1]]
        if base <= 0:
            continue
        chg = (cur / base - 1) * 100  # 融资余额近20日变化率(%)
        # 未来收益
        idx = common_dates.index(td)
        c0 = close_map[td]
        fut = {}
        for h in horizon_days:
            if idx + h < len(common_dates):
                c1 = close_map[common_dates[idx + h]]
                fut[h] = (c1 / c0 - 1) * 100
        if all(h in fut for h in horizon_days):
            pairs.append((chg, fut[5], fut[10], fut[20]))
    return pairs


def main():
    n_sample = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    ss.init()

    # 拉全市场代码列表（复用 v6 的 fetch_universe）
    print("拉取全市场A股列表...", file=sys.stderr)
    try:
        uni = ss.fetch_universe()
        codes = [r["code"] for r in uni]
    except Exception as e:
        print(f"拉列表失败: {e}", file=sys.stderr)
        return
    random.seed(42)
    sample = random.sample(codes, min(n_sample, len(codes)))
    print(f"抽样 {len(sample)} 只回测（全市场 {len(codes)} 只）...", file=sys.stderr)

    all_pairs = []
    done = 0
    for code in sample:
        done += 1
        if done % 20 == 0:
            print(f"  进度 {done}/{len(sample)}", file=sys.stderr)
        try:
            pairs = eval_factor_pairs(code)
            all_pairs.extend(pairs)
        except Exception:
            continue

    if len(all_pairs) < 100:
        print(f"\n有效样本不足({len(all_pairs)}对)，无法回测", file=sys.stderr)
        return

    print(f"\n{'='*60}")
    print(f"融资余额变化因子 · 实证回测（有效样本 {len(all_pairs)} 对）")
    print(f"{'='*60}")

    # 1. IC 检验
    print("\n【一、IC 检验】因子值 vs 未来收益 秩相关")
    print(f"{'期限':<10}{'IC':>10}{'t值':>10}{'结论':>12}")
    for h, col in [(5, 1), (10, 2), (20, 3)]:
        xs = [p[0] for p in all_pairs]
        ys = [p[col] for p in all_pairs if p[col] is not None]
        # IC 需要等长，取完整的三元组
        sub = [(p[0], p[col]) for p in all_pairs if p[col] is not None]
        if len(sub) < 20:
            continue
        ic, t = spearman_ic([x[0] for x in sub], [x[1] for x in sub])
        verdict = "有效" if (ic is not None and ic > 0.02) else ("无效" if ic is not None and abs(ic) <= 0.02 else "反向")
        print(f"未来{h}日{ic if ic is None else f'{ic:>10.4f}'}{t if t is None else f'{t:>10.2f}'}{verdict:>12}")

    # 2. 分层回测（按因子值分5档）
    print("\n【二、分层回测】按融资余额变化率分5档，看未来收益是否单调")
    sorted_pairs = sorted(all_pairs, key=lambda x: x[0])
    n = len(sorted_pairs)
    qsize = n // 5
    print(f"{'档位':<10}{'因子区间':>22}{'未来5日':>10}{'5日胜率':>9}{'未来10日':>10}{'10日胜率':>10}{'未来20日':>10}{'20日胜率':>10}")
    for qi in range(5):
        lo = qi * qsize
        hi = (qi + 1) * qsize if qi < 4 else n
        grp = sorted_pairs[lo:hi]
        if not grp:
            continue
        rng = f"[{grp[0][0]:.1f}%, {grp[-1][0]:.1f}%]"
        def avgc(col):
            vals = [p[col] for p in grp if p[col] is not None]
            return mean(vals) if vals else float('nan')
        def wrc(col):
            vals = [p[col] for p in grp if p[col] is not None]
            return sum(1 for v in vals if v > 0) / len(vals) * 100 if vals else float('nan')
        print(f"Q{qi+1}{rng:>22}{avgc(1):>9.2f}%{wrc(1):>8.1f}%{avgc(2):>9.2f}%{wrc(2):>8.1f}%{avgc(3):>9.2f}%{wrc(3):>8.1f}%")

    # 3. Q5 vs Q1 单调性
    q1 = sorted_pairs[:qsize]
    q5 = sorted_pairs[-qsize:]
    ex5 = mean([p[1] for p in q5]) - mean([p[1] for p in q1])
    ex20 = mean([p[3] for p in q5]) - mean([p[3] for p in q1])
    print(f"\n【三、单调性】Q5(高融资流入) - Q1(低/流出)：5日超额 {ex5:+.2f}%，20日超额 {ex20:+.2f}%")
    if ex5 > 0.5 and ex20 > 0.5:
        print("结论：因子有效，融资余额上升确有正向预测力，保留10分权重合理")
    elif ex5 > 0 or ex20 > 0:
        print("结论：因子弱有效，建议降权至5分或缩小阈值")
    else:
        print("结论：因子无效或反向，建议剔除或反转逻辑")


if __name__ == "__main__":
    main()
