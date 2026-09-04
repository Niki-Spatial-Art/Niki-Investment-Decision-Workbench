#!/usr/bin/env python3
"""前向验证·记录器 —— 每天跑完 stock_screen_v6.py 后，把当日 Top N 票追加进 forward_tracking.json。

用途：v6.1 试运行期的"机器自动记账"。把每天筛出的 Top N 票连同选股日价格、综合分存下来，
供 forward_verify.py 在次日/第5日自动对照实际涨跌，累积命中率，判断"成交量收缩40分/低振幅20分"
是否真的经得起前向检验（而非只靠历史回测）。

用法：
    python forward_record.py --top 20                # 记录今天 screen_result_v6.json 的 Top20
    python forward_record.py --top 20 --date 20260904 # 显式指定选股日（默认自动取结果文件最新日期）
    python forward_record.py --list                  # 只看已记录的历史，不新增

依赖：无（纯标准库）。读取 stock_screen_v6.py 同目录下的 screen_result_v6.json。
"""
import json
import os
import sys
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
RESULT_FILE = os.path.join(HERE, "screen_result_v6.json")
TRACK_FILE = os.path.join(HERE, "forward_tracking.json")


def _load_tracking():
    if os.path.exists(TRACK_FILE):
        with open(TRACK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"records": [], "_meta": {"说明": "v6.1 前向验证记录。每只票记录选股日与价格，供 forward_verify.py 对照次日/5日表现。"}}


def _save_tracking(t):
    with open(TRACK_FILE, "w", encoding="utf-8") as f:
        json.dump(t, f, ensure_ascii=False, indent=2)


def _pick_date(gate):
    """从结果文件的 gate 里取交易日，fallback 今天。"""
    # gate 里通常有 "上证现价" 等，但没有直接日期；从结果文件顶层无日期字段，
    # 用文件 mtime 作为选股日（运行时即当日）。
    mt = os.path.getmtime(RESULT_FILE)
    return datetime.fromtimestamp(mt).strftime("%Y%m%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20, help="记录 Top N（默认20）")
    ap.add_argument("--date", default=None, help="选股日 YYYYMMDD，默认取结果文件生成日")
    ap.add_argument("--list", action="store_true", help="仅查看已记录历史")
    args = ap.parse_args()

    t = _load_tracking()

    if args.list:
        recs = t["records"]
        print(f"已记录 {len(recs)} 条前向验证样本：")
        for r in recs:
            print(f"  {r.get('date')} {r.get('code')} {r.get('name')} 价{r.get('price')} 综合分{r.get('score')}")
        return

    if not os.path.exists(RESULT_FILE):
        print("[error] 找不到 screen_result_v6.json，请先运行 stock_screen_v6.py", file=sys.stderr)
        sys.exit(1)

    with open(RESULT_FILE, "r", encoding="utf-8") as f:
        out = json.load(f)

    top = out.get("top", [])
    if not top:
        print("[error] 结果文件里没有 top 列表", file=sys.stderr)
        sys.exit(1)

    date = args.date or _pick_date(out)
    gate_open = out.get("gate_open", None)

    existing_dates = {r["date"] for r in t["records"]}
    if date in existing_dates:
        print(f"[warn] 选股日 {date} 已有记录，跳过（避免重复记账）。若确需重记，先手动删 forward_tracking.json 里该日记录。")
        return

    new_records = []
    for r in top[:args.top]:
        new_records.append({
            "date": date,
            "code": r["code"],
            "name": r["name"],
            "price": r["price"],
            "score": r.get("score", r.get("tech_score")),
            "tech_score": r.get("tech_score"),
            "b20": r.get("b20"),
            "turn_chg": r.get("turn_chg"),
            "vol_ratio": r.get("vol_ratio"),
            "gate_open": gate_open,
        })

    t["records"].extend(new_records)
    _save_tracking(t)
    print(f"✅ 已记录 {len(new_records)} 只票到 forward_tracking.json（选股日 {date}，闸门{'开' if gate_open else '未开'}）")
    for r in new_records:
        print(f"  {r['code']} {r['name']} 价{r['price']} 综合分{r['score']}")


if __name__ == "__main__":
    main()
