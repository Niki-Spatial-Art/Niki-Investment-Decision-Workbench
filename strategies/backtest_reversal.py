#!/usr/bin/env python3
"""反转因子回测 —— IVOL 特质波动率 + 换手率反转。

背景（2026-09-04 因子审查）：
  1. IVOL（特质波动率）：论文称高 IVOL 未来收益更低，A 股日线是否成立？须本地实证。
  2. 换手率反转：A 股"缩量=关注度低=未来超额高"是最强反转效应之一。

方法（对齐 factor-validation 范式①+④，逐日采样，无未来函数）：
  · 逐日采样点 i（步长15日），用「当时」因子值预测 [i, i+20] 未来收益。
  · IVOL：近20日「个股日收益 − 全市场均值日收益」残差标准差（简化：日收益 std）。
  · 换手率反转 turn_chg：近20日成交额 / 前20日成交额 − 1（负=缩量）。
    turn_level：log(近20日均成交额)，对照（测是否只是流动性/市值混杂）。
  · 单因子 IC（Spearman）+ 分层 + 缩量/放量分组对照。

依赖：星耀数智 SDK（复用 stock_screen_v6）。
运行：D:/anaconda/python.exe backtest_reversal.py
"""
import sys, random
import stock_screen_v6 as ss

try:
    import pandas as pd
    import numpy as np
except ImportError:
    pd = None
    np = None


def factor_at(df, i):
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    if i < 41 or i + 20 >= len(close):
        return None
    c = float(close.iloc[i])

    # IVOL：近20日日收益标准差（%）
    ret = close.iloc[i - 19:i + 1].pct_change().dropna()
    if len(ret) < 15:
        return None
    iv = float(ret.std() * 100)

    # 换手率反转：近20日成交额 vs 前20日成交额
    amt = (close * vol).astype(float)
    v_recent = float(amt.iloc[i - 19:i + 1].mean())
    v_prev = float(amt.iloc[i - 39:i - 19].mean())
    turn_chg = (v_recent / v_prev - 1) * 100 if v_prev else None
    turn_level = float(np.log(v_recent + 1)) if v_recent else None

    fut20 = (float(close.iloc[i + 20]) / c - 1) * 100

    return {"ivol": iv, "turn_chg": turn_chg, "turn_level": turn_level,
            "fut20": fut20}


def spearman(xs, ys):
    if pd is None or len(xs) < 3:
        return None
    return float(pd.Series(xs).rank().corr(pd.Series(ys).rank()))


def main():
    random.seed(42)
    ss.init()
    cal = ss.CAL
    print("拉取全市场列表...", file=sys.stderr)
    uni = ss.fetch_universe()
    print(f"共 {len(uni)} 只，随机抽 400 只回测", file=sys.stderr)
    sample = random.sample(uni, min(400, len(uni)))

    codes = [u["code"] for u in sample]
    end = int(str(cal[-1]))
    begin = int(str(cal[-150]))
    kd = ss.MARKET.query_kline(code_list=codes, begin_date=begin,
                               end_date=end, period=10008)

    rows = []
    for u in sample:
        df = kd.get(u["code"])
        if df is None or len(df) < 82:
            continue
        for i in range(45, len(df) - 20, 15):
            f = factor_at(df, i)
            if f is None:
                continue
            rows.append({"ivol": f["ivol"], "turn_chg": f["turn_chg"],
                         "turn_level": f["turn_level"], "ex20": f["fut20"]})

    print(f"\n有效样本（采样点）: {len(rows)}")
    if len(rows) < 50:
        print("样本不足，退出")
        return

    dfr = pd.DataFrame(rows)

    print("\n===== 单因子 IC（Spearman vs 未来20日收益）=====")
    for col, label in [("ivol", "IVOL特质波动率"), ("turn_chg", "换手率变化"),
                       ("turn_level", "换手率水平(log成交额)")]:
        ic = spearman(dfr[col], dfr["ex20"])
        print(f"  {label:<20} IC = {ic:+.4f}")

    print("\n===== 分层回测（5档，未来20日收益均值）=====")
    for col, label in [("ivol", "IVOL"), ("turn_chg", "换手率变化"),
                       ("turn_level", "换手率水平")]:
        try:
            q = pd.qcut(dfr[col], 5, labels=False, duplicates="drop")
        except Exception:
            print(f"  {label}: 分层失败")
            continue
        g = dfr.groupby(q)["ex20"].mean()
        line = "  ".join(f"Q{i+1}={v:+.2f}%" for i, v in enumerate(g.values))
        q5_q1 = g.iloc[-1] - g.iloc[0] if len(g) == 5 else None
        print(f"  {label:<20} {line}" +
              (f"   (Q5-Q1={q5_q1:+.2f}%)" if q5_q1 is not None else ""))

    print("\n===== 缩量/放量分组对照 =====")
    shrink = dfr[dfr["turn_chg"] <= -30]
    expand = dfr[dfr["turn_chg"] >= 30]
    if len(shrink):
        print(f"  缩量组(turn_chg<=-30%): 均值 {shrink['ex20'].mean():+.2f}%, "
              f"胜率 {(shrink['ex20'] > 0).mean()*100:.1f}%, n={len(shrink)}")
    if len(expand):
        print(f"  放量组(turn_chg>=+30%): 均值 {expand['ex20'].mean():+.2f}%, "
              f"胜率 {(expand['ex20'] > 0).mean()*100:.1f}%, n={len(expand)}")
    if len(shrink) and len(expand):
        print(f"  差(缩量-放量): {shrink['ex20'].mean() - expand['ex20'].mean():+.2f}%")


if __name__ == "__main__":
    main()
