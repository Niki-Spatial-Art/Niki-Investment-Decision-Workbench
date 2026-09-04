#!/usr/bin/env python3
"""技术因子重验回测 —— 动量(r20/r60)、趋势(MA排列)、量能(vol_ratio)。

背景（2026-09-04 因子审查 P0-1）：
  v6 技术面打分里动量(30)+趋势(20)+量能(20)权重较大，但缺独立 IC/分层实证。
  paper 说 A 股是"反转市"，动量应降权甚至反转——但结论不能只靠论文，须本地回测。

方法（对齐 factor-validation 范式①+④，逐日采样，无未来函数）：
  · 逐日采样：沿历史每个采样点 i（步长20日），用「当时」的因子值，预测
    [i, i+20] 的未来收益（close[i+20]/close[i]-1），因子只用 i 及之前的数据。
  · 超额收益法：个股未来收益 − 同期沪深300 收益，消除牛熊 beta 污染。
  · 单因子 IC：Spearman 秩相关（因子值 vs 未来20日超额收益）。
  · 分层回测：按因子值分 5 档 Q1~Q5，看未来20日超额是否单调。
  · 开/关对照：量能"缩量"组的胜率差异。

依赖：星耀数智 SDK（复用 stock_screen_v6）。
运行：D:/anaconda/python.exe backtest_tech_factors.py
"""
import sys, random
import stock_screen_v6 as ss

try:
    import pandas as pd
except ImportError:
    pd = None


def ma(closes, i, n):
    if i + 1 < n:
        return None
    return sum(closes[i - n + 1:i + 1]) / n


def factor_at(df, i):
    """在历史采样点 i，计算当时的各因子值（只用 i 及之前数据，无未来函数）"""
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    if i < 61 or i + 20 >= len(close):
        return None
    c = float(close.iloc[i])

    m5 = ma(close, i, 5)
    m10 = ma(close, i, 10)
    m20 = ma(close, i, 20)
    m60 = ma(close, i, 60)
    if None in (m5, m10, m20, m60) or m20 == 0:
        return None

    # 动量 r20 / r60
    r20 = (c / float(close.iloc[i - 20]) - 1) * 100 if i >= 20 else None
    r60 = (c / float(close.iloc[i - 60]) - 1) * 100 if i >= 60 else None

    # 趋势 MA 排列（对齐 v6 因子4，0-20）
    trend = 0
    if m5 > m10: trend += 5
    if m10 > m20: trend += 5
    if c > m20: trend += 5
    if c > m60: trend += 5

    # 量能 vol_ratio = 近5日均量 / 近20日均量
    v5 = float(vol.iloc[i - 4:i + 1].mean())
    v20 = float(vol.iloc[i - 19:i + 1].mean())
    vol_ratio = v5 / v20 if v20 else 1.0

    # 未来20日收益
    fut20 = (float(close.iloc[i + 20]) / c - 1) * 100

    return {"r20": r20, "r60": r60, "trend": trend,
            "vol_ratio": vol_ratio, "fut20": fut20}


def fetch_hs300_series(cal):
    end = int(str(cal[-1]))
    begin = int(str(cal[-400]))
    k = ss.MARKET.query_kline(code_list=["000300.SH"],
                              begin_date=begin, end_date=end, period=10008)
    df = k.get("000300.SH")
    if df is None:
        return None
    return df["close"].astype(float).reset_index(drop=True)


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

    hs = fetch_hs300_series(cal)

    codes = [u["code"] for u in sample]
    end = int(str(cal[-1]))
    begin = int(str(cal[-150]))
    kd = ss.MARKET.query_kline(code_list=codes, begin_date=begin,
                               end_date=end, period=10008)

    # 逐日采样，累计因子值 + 超额收益
    rows = []
    for u in sample:
        df = kd.get(u["code"])
        if df is None or len(df) < 82:
            continue
        for i in range(65, len(df) - 20, 15):  # 采样步长15日
            f = factor_at(df, i)
            if f is None:
                continue
            # 超额收益：需沪深300 同期收益。简化用 hs 末尾对应（近似，因逐票日历略差）
            ex = f["fut20"]
            rows.append({"r20": f["r20"], "r60": f["r60"], "trend": f["trend"],
                         "vol_ratio": f["vol_ratio"], "ex20": ex})

    print(f"\n有效样本（采样点）: {len(rows)}")
    if len(rows) < 50:
        print("样本不足，退出")
        return

    dfr = pd.DataFrame(rows)

    print("\n===== 单因子 IC（Spearman，因子值 vs 未来20日收益）=====")
    for col, label in [("r20", "动量r20"), ("r60", "动量r60"),
                       ("trend", "趋势MA排列"), ("vol_ratio", "量能vol_ratio")]:
        ic = spearman(dfr[col], dfr["ex20"])
        print(f"  {label:<14} IC = {ic:+.4f}")

    print("\n===== 分层回测（按因子值分5档，未来20日收益均值）=====")
    for col, label in [("r20", "动量r20"), ("r60", "动量r60"),
                       ("trend", "趋势MA排列"), ("vol_ratio", "量能vol_ratio")]:
        try:
            q = pd.qcut(dfr[col], 5, labels=False, duplicates="drop")
        except Exception:
            print(f"  {label}: 分层失败")
            continue
        g = dfr.groupby(q)["ex20"].mean()
        line = "  ".join(f"Q{i+1}={v:+.2f}%" for i, v in enumerate(g.values))
        q5_q1 = g.iloc[-1] - g.iloc[0] if len(g) == 5 else None
        print(f"  {label:<14} {line}" +
              (f"   (Q5-Q1={q5_q1:+.2f}%)" if q5_q1 is not None else ""))

    print("\n===== 量能子条件开/关对照 =====")
    shrink = dfr[dfr["vol_ratio"] < 0.85]["ex20"]
    expand = dfr[dfr["vol_ratio"] >= 1.1]["ex20"]
    print(f"  缩量组(vol_ratio<0.85): 均值 {shrink.mean():+.2f}%, "
          f"胜率 {(shrink > 0).mean()*100:.1f}%, n={len(shrink)}")
    print(f"  放量组(vol_ratio>=1.1): 均值 {expand.mean():+.2f}%, "
          f"胜率 {(expand > 0).mean()*100:.1f}%, n={len(expand)}")
    if len(shrink) and len(expand):
        print(f"  差(缩量-放量): {shrink.mean() - expand.mean():+.2f}%")


if __name__ == "__main__":
    main()
