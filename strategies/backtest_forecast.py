#!/usr/bin/env python3
"""业绩预告因子（v6 因子F）实证回测 —— 事件研究法（超额收益版）。

问题：v6 给"有预增/扭亏预告"最高 10 分（业绩提前确认 = 利好），这个方向对吗？

方法（事件研究法，控制市场 beta）：
  对每只票近 N 年的每一条"预增/扭亏"类预告（P_TYPECODE ∈ 预增10/略增3/扭亏4），
  取公告日 ANN_DATE，计算公告日之后 5/10/20 个交易日的：
    · 绝对收益（个股收盘涨幅）
    · 超额收益（个股涨幅 − 同期沪深300指数涨幅）  ← 关键：剔除牛熊市影响
  再看预增公告是否产生正向超额收益（跑赢大盘）。

  关键修正（相对 backtest_forecast.py 初版）：
    1. 用超额收益代替绝对收益，否则 2007/2015 牛市顶 + 2008/2018 熊市会淹没信号
    2. 限定近 N 年（默认 2023 年起），陈旧数据对当下选股无指导意义

数据源：星耀 AmazingData
  · get_profit_notice -> DataFrame，字段 ANN_DATE(int)/P_TYPECODE/P_CHANGE_MAX/MIN/
    NET_PROFIT_MAX/MIN/P_NET_PARENT_FIRM(去年同期归母净利)
  · 未来收益用 query_kline 日K；基准用 000300.SH 沪深300指数日K

输出：
  1. 预增/扭亏公告后 5/10/20 日 绝对收益 + 超额收益 + 胜率
  2. 对照：预减/亏损(利空)公告的同期表现
  3. 因子F 方向结论
"""
import sys, os, random, bisect
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_screen_v6 as ss


GOOD_TYPES = {"3", "4", "10", "11", "12", "13"}   # 略增/扭亏/预增/续盈
BAD_TYPES = {"1", "2", "5", "6", "7", "9", "14", "15", "16"}  # 预亏/预减/首亏

# 只统计 2023 年以来的预告（近3年），避免陈旧数据
SINCE = 20230101


def fetch_close_map(code, n=700):
    """拉单票日K收盘，返回 ({trade_date:int: close}, [有序日期])。"""
    try:
        CAL = ss.CAL
        end = int(str(CAL[-1]))
        begin = int(str(CAL[-n]))
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
            date_s = str(row["kline_time"]).split()[0]
            td = int(date_s.replace("-", ""))
            rows.append((td, float(row["close"])))
        except Exception:
            continue
    rows.sort(key=lambda x: x[0])
    return {td: c for td, c in rows}, [td for td, _ in rows]


def main():
    n_sample = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    ss.init()

    # 基准：沪深300指数日K
    try:
        r = ss.MARKET.query_kline(code_list=["000300.SH"], begin_date=20221201,
                                  end_date=int(str(ss.CAL[-1])), period=10008)
        bdf = r.get("000300.SH") if isinstance(r, dict) else r
        bench_map = {}
        bench_dates = []
        for _, row in bdf.iterrows():
            date_s = str(row["kline_time"]).split()[0]
            td = int(date_s.replace("-", ""))
            bench_map[td] = float(row["close"])
            bench_dates.append(td)
        bench_dates.sort()
    except Exception as e:
        print(f"基准拉取失败: {e}", file=sys.stderr)
        return

    print("拉取全市场A股列表...", file=sys.stderr)
    try:
        uni = ss.fetch_universe()
        codes = [r["code"] for r in uni]
    except Exception as e:
        print(f"拉列表失败: {e}", file=sys.stderr)
        return
    random.seed(7)
    sample = random.sample(codes, min(n_sample, len(codes)))
    print(f"抽样 {len(sample)} 只，仅统计 {SINCE} 以来预告...", file=sys.stderr)

    good_abs = {h: [] for h in (5, 10, 20)}
    good_ex = {h: [] for h in (5, 10, 20)}
    bad_abs = {h: [] for h in (5, 10, 20)}
    bad_ex = {h: [] for h in (5, 10, 20)}

    done = 0
    for code in sample:
        done += 1
        if done % 25 == 0:
            print(f"  进度 {done}/{len(sample)}", file=sys.stderr)
        try:
            r = ss.INFO.get_profit_notice([code], is_local=False)
        except Exception:
            continue
        df = r if hasattr(r, "columns") else (r.get(code) if isinstance(r, dict) else None)
        if df is None or not len(df) or "ANN_DATE" not in df.columns:
            continue
        close_map, dates = fetch_close_map(code)
        if not dates or len(dates) < 60:
            continue
        for _, row in df.iterrows():
            try:
                ann = int(row["ANN_DATE"])
            except Exception:
                continue
            if ann < SINCE:
                continue
            tc = str(row.get("P_TYPECODE", ""))
            good = tc in GOOD_TYPES
            bad = tc in BAD_TYPES
            if not good and not bad:
                continue
            # 个股起点
            pos = bisect.bisect_left(dates, ann)
            if pos >= len(dates):
                continue
            c0 = close_map[dates[pos]]
            if c0 <= 0:
                continue
            # 基准起点
            bpos = bisect.bisect_left(bench_dates, ann)
            if bpos >= len(bench_dates):
                continue
            b0 = bench_map[bench_dates[bpos]]
            if b0 <= 0:
                continue
            for h in (5, 10, 20):
                if pos + h < len(dates) and bpos + h < len(bench_dates):
                    c1 = close_map[dates[pos + h]]
                    b1 = bench_map[bench_dates[bpos + h]]
                    abs_r = (c1 / c0 - 1) * 100
                    ex_r = abs_r - (b1 / b0 - 1) * 100
                    if good:
                        good_abs[h].append(abs_r)
                        good_ex[h].append(ex_r)
                    else:
                        bad_abs[h].append(abs_r)
                        bad_ex[h].append(ex_r)

    print(f"\n{'='*66}")
    print(f"业绩预告因子 · 事件研究回测（超额收益版，{SINCE} 以来）")
    print(f"{'='*66}")
    print(f"预增/扭亏(利好) vs 预减/亏损(利空)  事件数："
          f"{len(good_abs[20])} vs {len(bad_abs[20])}")

    def block(title, absd, exd):
        if len(exd[20]) < 10:
            print(f"\n[{title}] 事件不足({len(exd[20])})，跳过")
            return None
        print(f"\n【{title}】({len(exd[20])} 条)")
        print(f"{'期限':<8}{'绝对收益':>12}{'超额收益':>12}{'超额胜率':>10}")
        last_ex = None
        for h in (5, 10, 20):
            a = mean(absd[h]) if absd[h] else 0
            e = mean(exd[h]) if exd[h] else 0
            wr = sum(1 for v in exd[h] if v > 0) / len(exd[h]) * 100 if exd[h] else 0
            print(f"未来{h}日{a:>11.2f}%{e:>11.2f}%{wr:>9.1f}%")
            last_ex = e
        return last_ex

    g20 = block("预增/扭亏(利好)", good_abs, good_ex)
    b20 = block("预减/亏损(利空)", bad_abs, bad_ex)

    if g20 is not None and b20 is not None:
        print(f"\n{'='*66}")
        print(f"【结论】预增 vs 预减 未来20日超额收益：{g20:+.2f}% vs {b20:+.2f}%")
        if g20 > b20 + 0.5:
            print("因子F 方向正确：预增公告后确能跑赢预减，10分合理")
        elif abs(g20 - b20) <= 0.5:
            print("因子F 无区分度：预增/预减公告后超额收益无差异，建议降权或剔除")
        else:
            print("因子F 反向：预增公告后反而跑输预减(利好兑现见光死)，建议反转或剔除")


if __name__ == "__main__":
    main()
