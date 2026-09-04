#!/usr/bin/env python3
"""前向验证·验证器 —— 读 forward_tracking.json 历史记录，自动对照实际涨跌，累积命中率。

用途：把"试运行观察 5-10 个交易日"从人肉盯变成机器自动记。对每条记录，拉取选股日之后的
第 1 日、第 5 日收盘价，计算：
    · 次日涨跌（%）
    · 5日涨跌（%）
    · 5日超额（个股5日 − 沪深300 5日，控 beta）
并输出：胜率、平均收益、平均超额、命中率（跑赢沪深300的比例）。

数据源：腾讯日K（web.ifzq.gtimg.cn），零依赖、零 key（t0-band-watch skill 已验证）。
用法：
    python forward_verify.py              # 全量验证所有已到期样本
    python forward_verify.py --min-hold 5 # 只统计已满5个交易日的样本
"""
import json
import os
import sys
import argparse
import urllib.request
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TRACK_FILE = os.path.join(HERE, "forward_tracking.json")

HS300_CODE = "sh000300"


def _norm_code(code):
    """把 600519.SH -> sh600519 / 000001.SZ -> sz000001，适配腾讯接口。"""
    code = code.upper()
    if "." in code:
        num, mkt = code.split(".")
        prefix = "sh" if mkt == "SH" else ("sz" if mkt == "SZ" else "bj")
        return prefix + num
    # 无后缀时按首位推断
    if code.startswith(("6", "9")):
        return "sh" + code
    return "sz" + code


def fetch_kline(code, n=120):
    """腾讯日K，返回 [(date, close), ...] 升序。失败返回 []。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [warn] {code} 拉取失败：{e}", file=sys.stderr)
        return []
    try:
        node = data["data"][code]
        kline = node.get("qfqday") or node.get("day") or []
        out = []
        for row in kline:
            # row = [date, open, close, high, low, volume, ...]
            out.append((row[0], float(row[2])))
        return out
    except Exception as e:
        print(f"  [warn] {code} 解析失败：{e}", file=sys.stderr)
        return []


def _hold_days(kline, base_date):
    """在升序 kline 里找 base_date 当天，返回其后已过去多少个交易日（0=当天）。
    找不到 base_date 返回 None。兼容 YYYYMMDD 与 YYYY-MM-DD。"""
    base_date = str(base_date)
    if len(base_date) == 8 and base_date.isdigit():
        base_date = f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:8]}"
    dates = [k[0] for k in kline]
    if base_date not in dates:
        return None
    i = dates.index(base_date)
    return len(kline) - 1 - i  # 当天之后的交易日数


def _find_return(kline, base_date, horizon):
    """在升序 kline 里找 base_date 当天收盘，返回其后第 horizon 个交易日的涨跌%。
    找不到 base_date 或样本不足返回 None。兼容 YYYYMMDD 与 YYYY-MM-DD 两种日期格式。"""
    base_date = str(base_date)
    # 统一成 YYYY-MM-DD 便于与腾讯K线日期匹配
    if len(base_date) == 8 and base_date.isdigit():
        base_date = f"{base_date[:4]}-{base_date[4:6]}-{base_date[6:8]}"
    dates = [k[0] for k in kline]
    if base_date not in dates:
        return None
    i = dates.index(base_date)
    j = i + horizon
    if j >= len(kline):
        return None
    base_close = kline[i][1]
    fut_close = kline[j][1]
    if base_close <= 0:
        return None
    return (fut_close / base_close - 1) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-hold", type=int, default=0, help="只统计已满 N 个交易日的样本（默认0=全部尝试，含未到期样本显示*）")
    args = ap.parse_args()

    if not os.path.exists(TRACK_FILE):
        print("[error] 找不到 forward_tracking.json，请先运行 forward_record.py 记录样本", file=sys.stderr)
        sys.exit(1)

    with open(TRACK_FILE, "r", encoding="utf-8") as f:
        t = json.load(f)

    records = t.get("records", [])
    if not records:
        print("暂无前向验证样本。")
        return

    # 先拉沪深300日K，用于超额收益
    print(f"拉取沪深300日K（{HS300_CODE}）...", file=sys.stderr)
    hs300_k = fetch_kline(HS300_CODE)
    print(f"  沪深300 {len(hs300_k)} 根K线", file=sys.stderr)

    rows = []
    skipped_hold = 0
    for r in records:
        code = r["code"]
        date = r["date"]
        norm = _norm_code(code)
        k = fetch_kline(norm)
        if not k:
            print(f"  [warn] {code} 无K线，跳过", file=sys.stderr)
            continue
        # --min-hold：只统计已满 N 个交易日的样本（用个股K线数交易日，比自然日准）
        hd = _hold_days(k, date)
        if args.min_hold > 0 and (hd is None or hd < args.min_hold):
            skipped_hold += 1
            continue
        r1 = _find_return(k, date, 1)
        r5 = _find_return(k, date, 5)
        hs1 = _find_return(hs300_k, date, 1)
        hs5 = _find_return(hs300_k, date, 5)
        ex5 = (r5 - hs5) if (r5 is not None and hs5 is not None) else None
        rows.append({
            "date": date, "code": code, "name": r.get("name"),
            "price": r.get("price"), "score": r.get("score"),
            "r1": round(r1, 2) if r1 is not None else None,
            "r5": round(r5, 2) if r5 is not None else None,
            "ex5": round(ex5, 2) if ex5 is not None else None,
        })
        time.sleep(0.2)  # 限速，避免被封

    if not rows:
        print("无有效样本。")
        return

    # 统计
    def _stat(vals):
        vals = [v for v in vals if v is not None]
        if not vals:
            return None
        win = sum(1 for v in vals if v > 0)
        return {"n": len(vals), "mean": round(sum(vals) / len(vals), 2),
                "win_rate": round(win / len(vals) * 100, 1)}

    r1_stat = _stat([r["r1"] for r in rows])
    r5_stat = _stat([r["r5"] for r in rows])
    ex5_stat = _stat([r["ex5"] for r in rows])

    print("\n===== 前向验证报告（v6.1 试运行） =====\n")
    print(f"样本总数：{len(rows)} 只（来自 {len(t['records'])} 条记录）")
    if skipped_hold:
        print(f"（另有 {skipped_hold} 只因未满 {args.min_hold} 个交易日被 --min-hold 过滤）")
    print()
    if r1_stat:
        print(f"次日涨跌：平均 {r1_stat['mean']:+}%，胜率 {r1_stat['win_rate']}%（n={r1_stat['n']}）")
    if r5_stat:
        print(f"5日涨跌：平均 {r5_stat['mean']:+}%，胜率 {r5_stat['win_rate']}%（n={r5_stat['n']}）")
    if ex5_stat:
        print(f"5日超额(对沪深300)：平均 {ex5_stat['mean']:+}%，跑赢比例 {ex5_stat['win_rate']}%（n={ex5_stat['n']}）")

    print("\n明细（* = 尚未满5日，样本不足）：")
    print(f"{'选股日':<10}{'代码':<11}{'名称':<10}{'综合分':>6}{'次日%':>8}{'5日%':>8}{'5日超额%':>9}")
    for r in rows:
        def fmt(v):
            return f"{v:+}" if v is not None else "  *"
        print(f"{r['date']:<10}{r['code']:<11}{r['name']:<10}{r['score']:>6}{fmt(r['r1']):>8}{fmt(r['r5']):>8}{fmt(r['ex5']):>9}")

    print("\n提示：样本 < 10 只时结论仅供参考；建议累计 20+ 只（5-10 个交易日）再做权重固化判断。")


if __name__ == "__main__":
    main()
