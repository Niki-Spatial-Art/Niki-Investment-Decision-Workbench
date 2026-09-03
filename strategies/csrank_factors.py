#!/usr/bin/env python3
"""全市场横截面因子计算模块（CSRANK/CSPCTRANK 相对强弱升级）。

动机
====
v6 财务/资金面因子（营收增速 / 融资变化 / 业绩预告方向）目前用「绝对阈值」打分，
例如营收增速 >30% 得 10 分。但 A 股 5500+ 只票横跨不同板块、市值、行业，
同样的营收增速在小盘成长股和大盘蓝筹里含义完全不同，固定阈值天然有偏。

本模块把「绝对数值」升级为「同一交易日截面内的相对强弱」：
    1. 收集全市场（或候选池）每只票的因子绝对值，拼成矩阵
       DataFrame[行 = 截面/日期, 列 = 标的代码]；
    2. 用 CrossSectionFunction.CSPCTRANK 得到 0~1 的截面百分位
       （已统一为「越大越强」，NaN 保留 NaN）；
    3. 按 direction（+1 正向 / -1 反向）把百分位映射为 0~满分的相对分数。

CSPCTRANK 语义（源码级确认，实测）：
    CSPCTRANK(x: DataFrame[行=日期,列=标的]) -> DataFrame
      · 对每一行（每个截面）单独算百分位排名，返回 0~1；
      · 1.0 = 该截面内值最大（最强），0 = 值最小（最弱）；
      · NaN 不参与，输出仍为 NaN。
    CSRANK(x, ascending) 则是排名（1 起），ascending=False 时值越大 rank 越小。

设计原则（与"不可想当然纳入"一致）
================================
    本模块只做「相对强弱」的工程实现，不做「方向该加分还是扣分」的判断——
    方向必须由回测脚本 backtest_csrank.py 实证（已知：融资反向、预告反向、
    营收弱有效正向）。因此每个因子配置 direction（+1 正向 / -1 反向），
    由实证结论决定，避免把直觉写死进打分。

用法
====
    from csrank_factors import build_csrank_scores
    scores = build_csrank_scores(factor_matrix)
    # factor_matrix: DataFrame[行=截面, 列=标的]，单截面用单行 DataFrame
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 因子配置：direction 由回测实证结论决定（+1 正向 / -1 反向 / 0 仅排名不参与）
# ---------------------------------------------------------------------------
FACTOR_SPECS = {
    # 财务面：营收增速弱有效（IC 60日 +0.031）→ 正向
    "营收增速":   {"direction": +1, "max_score": 10},
    # 资金面：融资余额变化反向（IC -0.05x 显著反向）→ 值越大越弱，应反向
    "融资变化":   {"direction": -1, "max_score": 10},
    # 资金面：业绩预告「利好见光死」反向 → 预告越"好"越弱（反向）
    "预告方向":   {"direction": -1, "max_score": 10},
    # 技术面相对强弱（可选，未纳入综合分，供对比实验）
    "动量r20":    {"direction": +1, "max_score": 30},
    "回踩b20":    {"direction": +1, "max_score": 30},
}


def pct_rank(matrix: pd.DataFrame) -> pd.DataFrame:
    """截面百分位排名（封装 CSPCTRANK），返回 0~1 的 DataFrame，越大越强。

    matrix: DataFrame[行=截面/日期, 列=标的]，单截面用单行 DataFrame。
    """
    from AmazingData import CrossSectionFunction
    return CrossSectionFunction().CSPCTRANK(matrix)


def _pct_to_score(pct: pd.Series, max_score: float, direction: int) -> pd.Series:
    """把截面百分位（0~1，越大越强）映射为 0~max_score。

    分位数阶梯（而非绝对阈值）：前20%满分、前40%拿80%、…，使每个截面
    内分数分布稳定，不受绝对数值水平影响。
    direction=+1：越强分越高；direction=-1：越强分越低（反向因子）。
    """
    s = pct.astype(float).clip(0, 1)
    if direction >= 0:
        score = np.select([s >= 0.8, s >= 0.6, s >= 0.4, s >= 0.2],
                          [1.0, 0.8, 0.6, 0.4], default=0.2)
    else:
        # 反向因子：越强（pct 大）分越低
        score = np.select([s <= 0.2, s <= 0.4, s <= 0.6, s <= 0.8],
                          [1.0, 0.8, 0.6, 0.4], default=0.2)
    result = score * max_score
    result = pd.Series(result, index=s.index)
    # NaN 因子值保持 NaN（不参与截面，也不给保底分）
    result = result.where(s.notna(), np.nan)
    return result.round(1)


def build_single_factor_matrix(values: dict) -> pd.DataFrame:
    """把 {code: factor_value} 组装成单截面因子矩阵（1 行 × 多标的）。

    这是选股场景最常用的形态：某个截面（今天）全市场每只票的一个因子值。
    之后直接喂给 pct_rank() 得到截面百分位，再映射为分数。
    """
    s = pd.Series(values, dtype=float)
    return pd.DataFrame([s])  # 1 行 × N 标的


def score_single_factor(values: dict, fname: str, specs: dict = None) -> pd.Series:
    """单因子便捷入口：{code: value} -> code: 截面相对分数。

    values : {code: 因子绝对数值}
    fname  : 因子名，须在 specs 中（默认 FACTOR_SPECS）
    返回   : pd.Series[code -> 0~max_score]
    """
    specs = specs or FACTOR_SPECS
    if fname not in specs:
        raise KeyError(f"因子 {fname} 未配置，可用：{list(specs)}")
    cfg = specs[fname]
    mat = build_single_factor_matrix(values)
    pct = pct_rank(mat).iloc[0]  # 1 行 -> Series[code -> 0~1]
    return _pct_to_score(pct, cfg["max_score"], cfg["direction"])


def build_all_scores(factor_values: dict, specs: dict = None) -> dict:
    """多因子批量入口：{code: {factor: value}} -> {factor: Series[code->分数]}。

    factor_values : {code: {factor_name: value}}
    返回         : {factor_name: pd.Series[code -> 0~max_score]}
    """
    specs = specs or FACTOR_SPECS
    # 收集每个因子的 {code: value}
    per_factor = {f: {} for f in specs}
    for code, fdict in factor_values.items():
        if not isinstance(fdict, dict):
            continue
        for f, v in fdict.items():
            if f in per_factor:
                per_factor[f][code] = v
    out = {}
    for f, vals in per_factor.items():
        if vals:
            out[f] = score_single_factor(vals, f, specs)
    return out
