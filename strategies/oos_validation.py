#!/usr/bin/env python3
"""滚动样本外验证工具。

输入 CSV 至少包含 ``date,score,forward_return``，score 是任意候选因子/组合分数，
forward_return 为持有期收益(小数)。脚本按时间切分训练/测试窗口，在测试段统计
胜率、净收益、最大回撤，并扣除单程成本与滑点，避免把回测胜率当成可交易结果。
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WindowResult:
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    observations: int
    win_rate: float
    net_return: float
    max_drawdown: float


def _drawdown(returns: list[float]) -> float:
    equity = peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def rolling_oos(rows: list[dict], train_size: int = 252, test_size: int = 63,
                quantile: float = 0.2, cost_bps: float = 10.0,
                slippage_bps: float = 5.0) -> list[WindowResult]:
    rows = sorted(rows, key=lambda x: x["date"])
    results = []
    for start in range(0, len(rows) - train_size - test_size + 1, test_size):
        train = rows[start:start + train_size]
        test = rows[start + train_size:start + train_size + test_size]
        threshold = sorted(float(x["score"]) for x in train)[max(0, int(len(train) * (1 - quantile)) - 1)]
        selected = [x for x in test if float(x["score"]) >= threshold]
        if not selected:
            continue
        friction = (cost_bps + slippage_bps) / 10000.0
        returns = [float(x["forward_return"]) - friction for x in selected]
        compounded = 1.0
        for value in returns:
            compounded *= 1.0 + value
        results.append(WindowResult(train[0]["date"], train[-1]["date"], test[0]["date"], test[-1]["date"],
                                    len(returns), sum(v > 0 for v in returns) / len(returns),
                                    compounded - 1.0, _drawdown(returns)))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--train", type=int, default=252)
    parser.add_argument("--test", type=int, default=63)
    parser.add_argument("--cost-bps", type=float, default=10.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args()
    with args.csv.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    results = rolling_oos(rows, args.train, args.test, cost_bps=args.cost_bps, slippage_bps=args.slippage_bps)
    print("window,test,obs,win_rate,net_return,max_drawdown")
    for item in results:
        print(f"{item.test_start}..{item.test_end},{item.test_end},{item.observations},{item.win_rate:.4f},{item.net_return:.4f},{item.max_drawdown:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
