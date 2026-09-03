#!/usr/bin/env python3
"""大盘三信号闸门回测 —— 有效性 + 阈值校准。

背景：选股框架的"三信号齐才启动"闸门是核心防线，但从未实证过其择时价值，
      "上证>4000" 是拍脑袋的固定阈值，未验证是否合理。

回测问题：
  Q1 闸门有效性：三信号开启后的未来收益，是否显著优于关闭时？
      （若闸门开着反而更差，说明闸门是"追高陷阱"，应反转或废弃）
  Q2 阈值校准：上证>4000 固定值 vs 上证站上自身 MA20 / MA60 动态阈值，哪个更好？
  Q3 信号组合：单信号 vs 双信号 vs 三信号，边际贡献如何？

方法（事件研究，逐交易日扫描，超额收益）：
  对每个历史交易日 t（2015 以来，step=5 采样）：
    · 计算三信号状态：c1(沪深300站MA20) / c2(上证>4000) / c3(中证1000站MA20)
    · 未来收益 = 未来 5/10/20/60 日「沪深300 绝对收益」+「中证1000 相对沪深300 超额」
      分别看闸门对"大盘"和"小盘超额"的预测力
    · 按闸门开启/关闭分组统计未来收益均值 + 胜率 + 样本数

  注意：闸门本质是"大盘择时"，故收益标的用指数本身（沪深300/中证1000），
        额外看"中证1000 相对沪深300"的超额，检验闸门对风格轮动的预测力。

数据源：星耀 AmazingData（复用 stock_screen_v6 的 ss.init / MARKET）
  · 沪深300 000300.SH、上证 000001.SH、中证1000 000852.SH 日K

输出：闸门开关分组收益表 + 阈值对比表 + 信号组合边际贡献表。
"""
import sys, os, bisect
from statistics import mean

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stock_screen_v6 as ss


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except Exception:
        return None


def fetch_index(code, begin=20150101):
    """指数日K -> ({date:int:close}, [有序date])。"""
    try:
        end = int(str(ss.CAL[-1]))
        r = ss.MARKET.query_kline(code_list=[code], begin_date=begin,
                                  end_date=end, period=10008)
    except Exception:
        return None, None
    df = r.get(code) if isinstance(r, dict) else r
    if df is None or not len(df):
        return None, None
    m, ds = {}, []
    for _, row in df.iterrows():
        try:
            td = int(str(row["kline_time"]).split()[0].replace("-", ""))
            m[td] = float(row["close"])
            ds.append(td)
        except Exception:
            continue
    ds.sort()
    return m, ds


def ma_of(close_list, i, n):
    """close_list 到下标 i（含）末尾 n 日均值。"""
    if i + 1 < n:
        return None
    seg = close_list[i - n + 1: i + 1]
    return sum(seg) / n


def build_signals(hs_map, hs_ds, sh_map, sh_ds, zz_map, zz_ds, step=5):
    """逐日扫描，返回 list[dict]，每项含当日三信号状态 + 未来收益。

    c1 = 沪深300站MA20; c2 = 上证>4000(固定); c2d = 上证站MA20(动态);
    c2d60 = 上证站MA60; c3 = 中证1000站MA20.
    """
    hs_c = [hs_map[d] for d in hs_ds]
    sh_c = [sh_map[d] for d in sh_ds]
    zz_c = [zz_map[d] for d in zz_ds]

    rows = []
    n = min(len(hs_ds), len(sh_ds), len(zz_ds))
    for i in range(20, n - 60, step):
        # 三指数日期对齐（用 hs_ds 为基准，其余按日期 bisect 对齐）
        td = hs_ds[i]
        # 上证/中证1000 对齐到同一交易日
        sp = bisect.bisect_left(sh_ds, td)
        zp = bisect.bisect_left(zz_ds, td)
        if sp >= len(sh_ds) or zp >= len(zz_ds):
            continue
        if sh_ds[sp] != td or zz_ds[zp] != td:
            continue  # 三个指数交易日需严格对齐

        hs_c20 = ma_of(hs_c, i, 20)
        sh_c20 = ma_of(sh_c, sp, 20)
        sh_c60 = ma_of(sh_c, sp, 60)
        zz_c20 = ma_of(zz_c, zp, 20)
        if None in (hs_c20, sh_c20, sh_c60, zz_c20):
            continue

        c1 = hs_c[i] > hs_c20
        c2 = sh_c[sp] > 4000
        c2d = sh_c[sp] > sh_c20          # 动态：上证站MA20
        c2d60 = sh_c[sp] > sh_c60        # 动态：上证站MA60
        c3 = zz_c[zp] > zz_c20

        # 未来收益：沪深300绝对 + 中证1000绝对 + 中证1000相对沪深300超额
        fut = {}
        for h in (5, 10, 20, 60):
            if i + h < n and sp + h < len(sh_ds) and zp + h < len(zz_ds):
                hs_ret = (hs_c[i + h] / hs_c[i] - 1) * 100
                zz_ret = (zz_c[zp + h] / zz_c[zp] - 1) * 100
                zz_ex = zz_ret - hs_ret   # 中证1000相对沪深300超额
                fut[h] = (hs_ret, zz_ret, zz_ex)

        rows.append({"td": td, "c1": c1, "c2": c2, "c2d": c2d, "c2d60": c2d60,
                     "c3": c3, "fut": fut})
    return rows


def stat(rows, pred, key="hs"):
    """按 pred(rows)->bool 分组，返回 {h: (mean, winrate, n)} for key 收益指标。"""
    grp = [r for r in rows if pred(r)]
    res = {}
    for h in (5, 10, 20, 60):
        vals = []
        for r in grp:
            if h in r["fut"]:
                idx = {"hs": 0, "zz": 1, "ex": 2}[key]
                vals.append(r["fut"][h][idx])
        if len(vals) < 20:
            res[h] = (None, None, len(vals))
        else:
            wr = sum(1 for v in vals if v > 0) / len(vals) * 100
            res[h] = (mean(vals), wr, len(vals))
    return res


def fmt_row(label, summ):
    cells = [label]
    for h in (5, 10, 20, 60):
        m, wr, n = summ[h]
        cells.append(f"{'—':>18}" if m is None else f"{m:>+6.2f}%/{wr:>5.1f}%({n})")
    return "  ".join(cells)


def print_table(title, rows, preds, key="hs"):
    print(f"\n【{title}】  (指标: {key})")
    print(f"{'组别':<28}{'未来5日':>18}{'未来10日':>18}{'未来20日':>18}{'未来60日':>18}")
    print("-" * 100)
    for label, pred in preds:
        print(fmt_row(label, stat(rows, pred, key)))


def main():
    ss.init()
    print("拉三大指数日K...", file=sys.stderr)
    hs_map, hs_ds = fetch_index("000300.SH")
    sh_map, sh_ds = fetch_index("000001.SH")
    zz_map, zz_ds = fetch_index("000852.SH")
    if hs_map is None or sh_map is None or zz_map is None:
        print("指数拉取失败", file=sys.stderr)
        return

    print("逐日扫描三信号状态...", file=sys.stderr)
    rows = build_signals(hs_map, hs_ds, sh_map, sh_ds, zz_map, zz_ds, step=2)
    print(f"有效交易日样本 {len(rows)} 天", file=sys.stderr)

    # 三信号门（固定阈值 4000）
    def gate_open(r):
        return r["c1"] and r["c2"] and r["c3"]
    # 三信号门（动态阈值 上证站MA20）
    def gate_open_dyn(r):
        return r["c1"] and r["c2d"] and r["c3"]
    # 三信号门（动态阈值 上证站MA60）
    def gate_open_dyn60(r):
        return r["c1"] and r["c2d60"] and r["c3"]

    print(f"\n{'='*100}")
    print("Q1 闸门有效性：三信号开启 vs 关闭（沪深300 未来收益）")
    print(f"{'='*100}")
    print_table("沪深300 未来收益（闸门择时价值）", rows,
                [("闸门开(>4000固定)", gate_open),
                 ("闸门关", lambda r: not gate_open(r))], key="hs")

    print(f"\n{'='*100}")
    print("Q1b 分年度：上证>4000 开启时的沪深300未来60日收益（看是否集中在2015牛顶）")
    print(f"{'='*100}")
    open4000 = [r for r in rows if r["c2"]]
    by_year = {}
    for r in open4000:
        y = r["td"] // 10000
        if 60 in r["fut"]:
            by_year.setdefault(y, []).append(r["fut"][60][0])
    for y in sorted(by_year):
        vals = by_year[y]
        wr = sum(1 for v in vals if v > 0) / len(vals) * 100
        print(f"  {y}年：{len(vals):>3}天，60日沪深300均涨 {mean(vals):>+6.2f}%，胜率 {wr:.0f}%")

    print(f"\n{'='*100}")
    print("Q2 阈值校准：上证>4000固定 vs 上证站MA20/MA60 动态（沪深300 未来收益）")
    print(f"{'='*100}")
    print_table("三种闸门定义对比（开启时沪深300收益）", rows,
                [("三信号(上证>4000)", gate_open),
                 ("三信号(上证站MA20)", gate_open_dyn),
                 ("三信号(上证站MA60)", gate_open_dyn60)], key="hs")

    print(f"\n{'='*100}")
    print("Q3 单信号边际贡献（沪深300 未来20日，各信号单独开启时）")
    print(f"{'='*100}")
    print_table("单信号开启时沪深300收益", rows,
                [("仅c1沪深300站MA20", lambda r: r["c1"]),
                 ("仅c2上证>4000", lambda r: r["c2"]),
                 ("仅c2d上证站MA20", lambda r: r["c2d"]),
                 ("仅c3中证1000站MA20", lambda r: r["c3"])], key="hs")

    print(f"\n{'='*100}")
    print("Q4 风格轮动：闸门对「中证1000相对沪深300超额」的预测力")
    print(f"{'='*100}")
    print_table("中证1000相对沪深300超额", rows,
                [("闸门开(>4000)", gate_open),
                 ("闸门关", lambda r: not gate_open(r))], key="ex")

    print(f"\n结论解读：")
    print("  Q1 若闸门开收益显著>关，闸门有择时价值；若接近或更差，闸门是无效/追高陷阱。")
    print("  Q2 看三种定义哪个开启时收益最高、胜率最稳，决定 4000 该保留还是改动态阈值。")
    print("  Q3 看哪个信号贡献最大，考虑是否精简闸门条件。")
    print("  Q4 看闸门是否同时预测小盘风格强弱（超额>0=小盘强）。")


if __name__ == "__main__":
    main()
