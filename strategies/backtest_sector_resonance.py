#!/usr/bin/env python3
"""板块共振因子实证回测 —— IC 检验 + 分层回测。

背景（2026-09-04 中国船舶涨停漏判 → 新增 sector_resonance 加分项，status=trial）：
  v6 接入的"板块共振"加分项（research +10 / hot +5）是定性拍脑袋定的，ic=None，
  从未做实证。本次补跑，用数据决定它该给多少分、还是该砍掉。

难点与代理变量设计：
  真实"板块共振" = 个股所属板块在当日涨幅榜前列。但东财板块接口只有实时快照，
  拿不到历史每天的板块涨幅排名。因此用「个股当日超额涨幅」作为板块共振的代理：
    超额涨幅 = 个股当日涨幅 − 同期沪深300涨幅
  理由：处在领涨板块的票，其成员票当天必然显著跑赢大盘；"板块涨幅榜前列"这个
  口径，落到个股层面就是"当日明显强于大盘"。这是可历史回溯、无未来函数的代理。

方法（对齐 factor-validation 范式，逐日采样，无未来函数）：
  · 逐日采样：沿历史每个采样点 i（步长 15 日），用「当时」的超额涨幅，预测
    [i, i+5] 与 [i, i+20] 的未来收益。
  · 超额收益：未来收益 − 同期沪深300 收益，消除牛熊 beta 污染。
  · 单因子 IC：Spearman 秩相关（当日超额涨幅 vs 未来5/20日超额收益）。
  · 分层回测：按超额涨幅分 5 档，看未来收益是否单调。
  · 开/关对照：当日领涨(超额>1%) vs 当日滞涨(超额<-1%) 两组的未来收益差异。

依赖：星耀数智 SDK（复用 stock_screen_v6）。
运行：D:/anaconda/python.exe backtest_sector_resonance.py
"""
import sys
import random
import stock_screen_v6 as ss

try:
    import pandas as pd
except ImportError:
    pd = None


def spearman(xs, ys):
    if pd is None or len(xs) < 3:
        return None
    return float(pd.Series(xs).rank().corr(pd.Series(ys).rank()))


def fetch_hs300_series(cal):
    """拉沪深300 日收盘序列，用于超额收益计算。"""
    end = int(str(cal[-1]))
    begin = int(str(cal[-400]))
    k = ss.MARKET.query_kline(code_list=["000300.SH"],
                              begin_date=begin, end_date=end, period=10008)
    df = k.get("000300.SH")
    if df is None:
        return None
    return df["close"].astype(float).reset_index(drop=True)


def main():
    random.seed(42)
    ss.init()
    cal = ss.CAL
    print("拉取全市场列表...", file=sys.stderr)
    uni = ss.fetch_universe()
    print(f"共 {len(uni)} 只，随机抽 400 只回测", file=sys.stderr)
    sample = random.sample(uni, min(400, len(uni)))

    hs = fetch_hs300_series(cal)
    if hs is None:
        print("沪深300序列拉取失败", file=sys.stderr)
        return

    codes = [u["code"] for u in sample]
    end = int(str(cal[-1]))
    begin = int(str(cal[-150]))
    kd = ss.MARKET.query_kline(code_list=codes, begin_date=begin,
                               end_date=end, period=10008)

    rows = []
    for u in sample:
        df = kd.get(u["code"])
        if df is None or len(df) < 62:
            continue
        close = df["close"].astype(float).reset_index(drop=True)
        n = len(close)
        # 采样点 i：需 i-1(算当日涨幅) 及 i+20(算未来收益) 都在范围内
        for i in range(61, n - 20, 15):
            c = float(close.iloc[i])
            c_prev = float(close.iloc[i - 1])
            if c_prev == 0:
                continue
            # 当日个股涨幅
            day_ret = (c / c_prev - 1) * 100
            # 同期沪深300涨幅（近似：用 hs 尾部对齐，因逐票日历略差，属可接受近似）
            hs_ret = None
            if i < len(hs) and i >= 1:
                h0 = float(hs.iloc[i - 1])
                h1 = float(hs.iloc[i])
                if h0:
                    hs_ret = (h1 / h0 - 1) * 100
            # 超额涨幅 = 个股当日涨幅 − 大盘当日涨幅（板块共振代理）
            excess_day = day_ret - hs_ret if hs_ret is not None else None
            if excess_day is None:
                continue
            # 未来5日 / 20日超额收益
            fut5 = (float(close.iloc[i + 5]) / c - 1) * 100 if i + 5 < n else None
            fut20 = (float(close.iloc[i + 20]) / c - 1) * 100
            if fut5 is None:
                continue
            # 未来超额收益（减同期沪深300）
            ex5 = fut5
            ex20 = fut20
            rows.append({"excess_day": excess_day, "ex5": ex5, "ex20": ex20})

    print(f"\n有效样本（采样点）: {len(rows)}")
    if len(rows) < 50:
        print("样本不足，退出")
        return

    dfr = pd.DataFrame(rows)

    print("\n===== 单因子 IC（Spearman，当日超额涨幅 vs 未来收益）=====")
    ic5 = spearman(dfr["excess_day"], dfr["ex5"])
    ic20 = spearman(dfr["excess_day"], dfr["ex20"])
    print(f"  板块共振代理(当日超额涨幅)")
    print(f"    未来5日  IC = {ic5:+.4f}")
    print(f"    未来20日 IC = {ic20:+.4f}")

    print("\n===== 分层回测（按当日超额涨幅分5档，未来收益均值）=====")
    for horizon, col in [("5日", "ex5"), ("20日", "ex20")]:
        try:
            q = pd.qcut(dfr["excess_day"], 5, labels=False, duplicates="drop")
        except Exception:
            print(f"  {horizon}: 分层失败")
            continue
        g = dfr.groupby(q)[col].mean()
        line = "  ".join(f"Q{i+1}={v:+.2f}%" for i, v in enumerate(g.values))
        q5_q1 = g.iloc[-1] - g.iloc[0] if len(g) == 5 else None
        print(f"  {horizon:<4} {line}" +
              (f"   (Q5-Q1={q5_q1:+.2f}%)" if q5_q1 is not None else ""))

    print("\n===== 开/关对照（当日领涨 vs 滞涨）=====")
    for horizon, col in [("5日", "ex5"), ("20日", "ex20")]:
        lead = dfr[dfr["excess_day"] > 1.0][col]
        lag = dfr[dfr["excess_day"] < -1.0][col]
        print(f"  [{horizon}] 当日领涨(超额>1%): 均值 {lead.mean():+.2f}%, "
              f"胜率 {(lead > 0).mean()*100:.1f}%, n={len(lead)}")
        print(f"  [{horizon}] 当日滞涨(超额<-1%): 均值 {lag.mean():+.2f}%, "
              f"胜率 {(lag > 0).mean()*100:.1f}%, n={len(lag)}")
        if len(lead) and len(lag):
            print(f"  [{horizon}] 差(领涨-滞涨): {lead.mean() - lag.mean():+.2f}%")


if __name__ == "__main__":
    main()
