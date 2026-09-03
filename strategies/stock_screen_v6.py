#!/usr/bin/env python3
"""全市场A股选股 v6 —— 技术面 + 基本面（财务+资金面）结合版。

v6 相对 v5 的核心变化（胜率优化）：
  在 v5 财务因子基础上，新增两个「资金面」信号：
    · 融资余额变化（10分）：反向指标（实证回测证明），融资余额近20日暴增
      = 杠杆资金高位追涨 = 后续踩踏，应扣分；平稳/回落反而抗跌加分
    · 业绩预告（10分）：反向指标（实证回测），预增公告"见光死"扣分，预减/首亏
      公告"利空出尽"（困境反转）反而加分

  财务因子满分从 30 分扩到 50 分：
    净利增速10 + 营收增速5 + 筹码集中10 + 龙虎榜5 + 融资余额10 + 业绩预告10

  综合分 = 技术分(0-100) + 财务分(0-50)，满分 150。
  输出保留 tech_score 与 fund_score 两个独立分数，便于技基对比。

财务/资金面接口（星耀数智 AmazingData InfoData）：
  · get_income      -> {code: DataFrame}，字段 REPORTING_PERIOD / REPORT_TYPE(4=年报)
  · get_holder_num  -> DataFrame，字段 HOLDER_ENDDATE / HOLDER_NUM / HOLDER_TOTAL_NUM
  · get_long_hu_bang-> DataFrame，字段 TRADE_DATE / BUY_AMOUNT / SELL_AMOUNT / TOTAL_AMOUNT
  · get_margin_detail-> dict{code: DataFrame}，字段 BORROW_MONEY_BALANCE(融资余额)
                       / PURCH_WITH_BORROW_MONEY(融资买入)
  · get_profit_notice-> DataFrame，字段 P_CHANGE_MAX/MIN(预告增速区间) / ANN_DATE(公告日)
  · get_balance_sheet -> 接口不稳定（查询失败），故 ROE 暂不纳入，改用净利增速替代

依赖：
  - AmazingData SDK（tgw），凭据从 Windows 用户级环境变量读取
    AD_USERNAME / AD_PASSWORD / AD_HOST / AD_PORT
  - pandas

用法：
  python stock_screen_v6.py            # 默认跑全市场（技术+财务+资金面）
  python stock_screen_v6.py --quick    # 快速模式（仅扫描成交额前800只）
  python stock_screen_v6.py --no-fund  # 只看技术面（等价 v4，跳过财务）
"""
import os
import sys
import json
import time
import argparse
import winreg
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import pandas as pd
except ImportError:
    pd = None

# ---------------------------------------------------------------------------
# 凭据读取（Windows 用户级环境变量，Bash 子进程读不到，需用 winreg）
# ---------------------------------------------------------------------------

def _read_env(name: str):
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
        val, _ = winreg.QueryValueEx(k, name)
        winreg.CloseKey(k)
        return val
    except Exception:
        return None


def _load_credentials():
    u = _read_env("AD_USERNAME") or os.environ.get("AD_USERNAME")
    p = _read_env("AD_PASSWORD") or os.environ.get("AD_PASSWORD")
    h = _read_env("AD_HOST") or os.environ.get("AD_HOST") or "101.230.159.234"
    po = _read_env("AD_PORT") or os.environ.get("AD_PORT") or "8600"
    return u, p, h, int(po)


# ---------------------------------------------------------------------------
# 登录与数据对象
# ---------------------------------------------------------------------------

def login():
    u, p, h, po = _load_credentials()
    if not u or not p:
        raise RuntimeError("星耀凭据未配置：请设置 AD_USERNAME / AD_PASSWORD 环境变量")
    import AmazingData as ad
    ad.login(username=u, password=p, host=h, port=int(po))
    return ad


AD = None
CAL = None
MARKET = None
TSF = None  # TimeSeriesFunction
BASE = None
INFO = None  # InfoData（财务）

def init():
    global AD, CAL, MARKET, TSF, BASE, INFO
    AD = login()
    CAL = AD.BaseData().get_calendar()
    MARKET = AD.MarketData(CAL)
    TSF = AD.TimeSeriesFunction
    BASE = AD.BaseData()
    INFO = AD.InfoData()
    return CAL


# ---------------------------------------------------------------------------
# 大盘三信号闸门
# ---------------------------------------------------------------------------

def _ma(series, n):
    return float(series.iloc[-n:].mean()) if len(series) >= n else None


def check_market_gate():
    code_map = {"沪深300": "000300.SH", "上证指数": "000001.SH", "中证1000": "000852.SH"}
    end = int(str(CAL[-1]))
    begin = int(str(CAL[-70]))
    k = MARKET.query_kline(code_list=list(code_map.values()),
                           begin_date=begin, end_date=end, period=10008)
    gate = {}
    for label, code in code_map.items():
        df = k.get(code)
        if df is None or len(df) < 20:
            gate[label] = {"现价": None, "MA20": None, "数据": "缺失"}
            continue
        close = df["close"]
        gate[label] = {"现价": round(float(close.iloc[-1]), 2),
                       "MA20": round(_ma(close, 20), 2)}
    hs = gate["沪深300"]["现价"]; hs_ma = gate["沪深300"]["MA20"]
    sh = gate["上证指数"]["现价"]
    zz = gate["中证1000"]["现价"]; zz_ma = gate["中证1000"]["MA20"]
    c1 = hs > hs_ma if hs and hs_ma else False
    c2 = sh > 4000 if sh else False
    c3 = zz > zz_ma if zz and zz_ma else False
    open3 = c1 and c2 and c3
    return open3, {
        "沪深300现价": hs, "沪深300_MA20": hs_ma, "沪深300站上MA20": c1,
        "上证现价": sh, "上证站上4000": c2,
        "中证1000现价": zz, "中证1000_MA20": zz_ma, "中证1000站上MA20": c3,
        "三信号全开": open3,
    }


# ---------------------------------------------------------------------------
# 全市场列表（星耀）
# ---------------------------------------------------------------------------

def fetch_universe():
    info = BASE.get_code_info(security_type="EXTRA_STOCK_A")
    rows = []
    for idx, r in info.iterrows():
        code = str(idx)
        if not (code.startswith(("00", "30", "60", "68"))):
            continue
        rows.append({
            "code": code,
            "name": str(r.get("symbol", "")),
            "security_status": r.get("security_status", ""),
            "pre_close": r.get("pre_close"),
            "high_limited": r.get("high_limited"),
            "low_limited": r.get("low_limited"),
            "list_day": r.get("list_day"),
        })
    return rows


# ---------------------------------------------------------------------------
# 日 K 线（星耀批量）
# ---------------------------------------------------------------------------

def fetch_kline_batch(codes, n=70):
    end = int(str(CAL[-1]))
    begin = int(str(CAL[-n]))
    return MARKET.query_kline(code_list=codes, begin_date=begin,
                              end_date=end, period=10008)


# ---------------------------------------------------------------------------
# 多因子打分（技术面，平衡权重，满分 100）
# ---------------------------------------------------------------------------

def score_stock(df):
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float)
    if len(close) < 61:
        return None
    c = float(close.iloc[-1])

    m5 = float(TSF.MA(close, 5).iloc[-1])
    m10 = float(TSF.MA(close, 10).iloc[-1])
    m20 = float(TSF.MA(close, 20).iloc[-1])
    m60 = float(TSF.MA(close, 60).iloc[-1])
    if not all(v == v for v in (m5, m10, m20, m60)):
        return None

    b20 = (c / m20 - 1) * 100
    r20 = (c / float(close.iloc[-21]) - 1) * 100
    r60 = (c / float(close.iloc[-61]) - 1) * 100
    hi60 = float(TSF.HHV(high, 60).iloc[-1])

    # 因子1 动量
    if r20 > 0 and r60 > 0:
        mom = min(r20, 25)
    elif r20 > 0 and r60 <= 0:
        mom = r20 * 0.4
    else:
        mom = r20 * 0.4
    mom_score = max(0, min(30, 15 + mom))

    # 因子2 回踩度
    bias_score = max(0, 30 - abs(b20) * 6)

    # 因子3 量能
    v5 = float(vol.iloc[-5:].mean())
    v20 = float(vol.iloc[-20:].mean())
    vol_ratio = v5 / v20 if v20 else 1
    if b20 < 1.0:
        vol_score = 20 if vol_ratio < 0.85 else (10 if vol_ratio < 1.1 else 3)
    else:
        vol_score = 12 if vol_ratio < 1.2 else 6
    vol_score = min(20, vol_score)

    # 因子4 趋势强度
    trend = 0
    if m5 > m10: trend += 5
    if m10 > m20: trend += 5
    if c > m20: trend += 5
    if c > m60: trend += 5
    trend_score = min(20, trend)

    # 因子5 波动
    atr = float((high.iloc[-10:] - low.iloc[-10:]).mean())
    atr_pct = atr / c * 100 if c else 0
    vol_score2 = max(0, min(10, 10 - (atr_pct - 3)))

    total = mom_score + bias_score + vol_score + trend_score + vol_score2
    return {"total": round(total, 1), "mom": round(mom_score, 1), "bias": round(bias_score, 1),
            "vol": round(vol_score, 1), "trend": round(trend_score, 1), "vol2": round(vol_score2, 1),
            "b20": round(b20, 2), "r20": round(r20, 2), "r60": round(r60, 2),
            "hi60": round(hi60, 2), "m20": round(m20, 2), "m5": round(m5, 2),
            "m10": round(m10, 2), "m60": round(m60, 2)}


# ---------------------------------------------------------------------------
# 逐票评估（技术面，在已拉取的日线 DataFrame 上计算）
# ---------------------------------------------------------------------------

def eval_one(item, df):
    code = item["code"]
    name = item["name"]
    if "ST" in name.upper() or "退" in name:
        return None
    list_day = item.get("list_day")
    if list_day:
        try:
            ld = int(str(list_day))
            if ld >= 20260501:
                return None
        except (TypeError, ValueError):
            pass
    pre_close = item.get("pre_close")
    c = float(df["close"].iloc[-1])
    if pre_close:
        try:
            pct = (c / float(pre_close) - 1) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            pct = 0
    else:
        pct = 0
    amount = float(df["amount"].iloc[-1]) if "amount" in df.columns else 0
    if amount < 2e8:
        return None
    if pct > 9.5 or pct < -5:
        return None

    sc = score_stock(df)
    if sc is None:
        return None

    if sc["b20"] > 6.0 or sc["b20"] < -4.0:
        return None
    if sc["m5"] <= sc["m10"]:
        return None
    if c <= sc["m20"]:
        return None
    if pct > 6.0:
        return None

    drawdown60 = (c / sc["hi60"] - 1) * 100
    if drawdown60 < -30:
        return None
    if sc["r20"] > 20 and pct < 1.0:
        return None

    buy = round(sc["m10"], 2)
    buy_low = round(min(sc["m10"], c * 0.985), 2)
    stop = round(buy_low * 0.965, 2)
    return {"code": code, "name": name, "pct": round(pct, 2), "amount": round(amount / 1e8, 2),
            "price": round(c, 2), "industry": "",
            "tech_score": sc["total"],
            "factor": {"动量": sc["mom"], "回踩": sc["bias"], "量能": sc["vol"],
                       "趋势": sc["trend"], "波动": sc["vol2"]},
            "b20": sc["b20"], "r20": sc["r20"], "r60": sc["r60"],
            "ma20": sc["m20"], "drawdown60": round(drawdown60, 1),
            "buy": buy, "buy_low": buy_low, "stop": stop}


# ---------------------------------------------------------------------------
# 财务因子打分（基本面，满分 30）
# ---------------------------------------------------------------------------

def _safe_float(v):
    try:
        f = float(v)
        return f if f == f else None  # NaN -> None
    except (TypeError, ValueError):
        return None


def fetch_finance_batch(codes):
    """对候选池批量拉取三大财务数据，返回 {code: {...}} 缓存。"""
    cache = {c: {} for c in codes}
    # 1. 利润表（返回 {code: DataFrame}）
    try:
        inc = INFO.get_income(codes, is_local=False)
        if isinstance(inc, dict):
            for c, df in inc.items():
                if hasattr(df, "columns"):
                    cache[c]["income"] = df
        elif hasattr(inc, "columns"):
            # 兜底：若直接返回 DataFrame，按 MARKET_CODE 拆分
            for c in codes:
                sub = inc[inc["MARKET_CODE"] == c] if "MARKET_CODE" in inc.columns else inc
                cache[c]["income"] = sub
    except Exception as e:
        print(f"  利润表拉取失败：{e}", file=sys.stderr)
    # 2. 股东户数
    try:
        hn = INFO.get_holder_num(codes, is_local=False)
        # get_holder_num 返回 DataFrame（含所有 code），按 MARKET_CODE 分组
        if hasattr(hn, "columns"):
            for c in codes:
                sub = hn[hn["MARKET_CODE"] == c]
                if len(sub):
                    cache[c]["holder"] = sub
        elif isinstance(hn, dict):
            for c, df in hn.items():
                cache[c]["holder"] = df
    except Exception as e:
        print(f"  股东户数拉取失败：{e}", file=sys.stderr)
    # 3. 龙虎榜
    try:
        lhb = INFO.get_long_hu_bang(codes)
        if hasattr(lhb, "columns"):
            for c in codes:
                sub = lhb[lhb["MARKET_CODE"] == c]
                if len(sub):
                    cache[c]["lhb"] = sub
    except Exception as e:
        print(f"  龙虎榜拉取失败：{e}", file=sys.stderr)
    # 4. 融资融券明细（个股级两融，字段 BORROW_MONEY_BALANCE 融资余额）
    try:
        md = INFO.get_margin_detail(codes, is_local=False)
        if isinstance(md, dict):
            for c, df in md.items():
                if hasattr(df, "columns") and len(df):
                    cache[c]["margin"] = df
        elif hasattr(md, "columns"):
            for c in codes:
                sub = md[md["MARKET_CODE"] == c]
                if len(sub):
                    cache[c]["margin"] = sub
    except Exception as e:
        print(f"  融资融券拉取失败：{e}", file=sys.stderr)
    # 5. 业绩预告（字段 P_CHANGE_MAX/MIN 预告增速区间、ANN_DATE 公告日）
    try:
        pn = INFO.get_profit_notice(codes, is_local=False)
        if isinstance(pn, dict):
            for c, df in pn.items():
                if hasattr(df, "columns") and len(df):
                    cache[c]["notice"] = df
        elif hasattr(pn, "columns"):
            for c in codes:
                sub = pn[pn["MARKET_CODE"] == c]
                if len(sub):
                    cache[c]["notice"] = sub
    except Exception as e:
        print(f"  业绩预告拉取失败：{e}", file=sys.stderr)
    return cache


def _norm(df):
    """标准化利润表：REPORT_TYPE/REPORTING_PERIOD 转 int，只保留合并报表口径(ST=1)。"""
    if df is None or not hasattr(df, "columns"):
        return None
    d = df.copy()
    for c in ("REPORT_TYPE", "REPORTING_PERIOD"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    # 只保留合并报表标准口径（STATEMENT_TYPE=1），无该列则全保留
    if "STATEMENT_TYPE" in d.columns:
        d = d[d["STATEMENT_TYPE"].astype(str) == "1"]
    return d


def _period_value(df, col, offset=0):
    """取最新报告期（或往前 offset 个同报告期）的某字段值。

    策略：取「最新报告期」的合并口径值，offset=1 时取「去年同期同报告期」
    （即报告期年份-1、月份日期相同），用于同比。返回 (报告期, 值)。
    """
    d = _norm(df)
    if d is None or len(d) == 0:
        return None, None
    if col not in d.columns:
        return None, None
    d = d.dropna(subset=[col]).sort_values("REPORTING_PERIOD")
    if len(d) == 0:
        return None, None

    if offset == 0:
        latest_rp = int(d["REPORTING_PERIOD"].iloc[-1])
        latest = d[d["REPORTING_PERIOD"] == latest_rp]
        val = _safe_float(latest[col].iloc[-1])
        return latest_rp, val

    # offset=1：找去年同期同报告期（年份-1，MMDD 相同）
    latest_rp = int(d["REPORTING_PERIOD"].iloc[-1])
    ly = latest_rp // 10000 - 1
    mmdd = latest_rp % 10000
    target = ly * 10000 + mmdd
    prev = d[d["REPORTING_PERIOD"] == target]
    if len(prev) == 0:
        return None, None
    val = _safe_float(prev[col].iloc[-1])
    return target, val


def _yoy(cur, prev):
    """同比增速（%），cur/prev 为 None 或 prev<=0 时返回 None。"""
    if cur is None or prev is None:
        return None
    if prev <= 0:
        return None
    return (cur / prev - 1) * 100


def _growth_nature(cur, prev):
    """判断利润增速的「性质」，用于稳健性修正。

    返回 (性质标签, 是否失真)：
      - "扭亏"    ：去年净利 <=0，今年 >0（重大利好，但无法算同比）
      - "低基数"  ：去年净利为正但 < 今年净利的 20%（增速会虚高，如 1561%）
      - "正常"    ：其余情况
    """
    if cur is None or prev is None:
        return "缺失", True
    if cur <= 0:
        return "亏损", True  # 今年仍亏损，无增长可言
    if prev <= 0:
        return "扭亏", True  # 去年亏今年赚
    # 低基数：去年净利 < 今年净利的 20%，增速失真
    # （半年报/季报绝对量小、波动大，20% 阈值比 5% 更贴合直觉）
    if abs(prev) < abs(cur) * 0.20:
        return "低基数", True
    return "正常", False


def score_fundamental(cache):
    """对单只候选计算财务因子分（0-50），返回 (总分, 明细dict)。

    v6 在 v5 基础上新增两个资金面因子：
      · 融资余额变化（10分）：反向指标，融资余额近20日暴增 = 杠杆追涨应扣分
      · 业绩预告（10分）：反向指标（实证回测），预增公告"见光死"扣分，预减/首亏
      公告"利空出尽"（困境反转）反而加分
    """
    inc = cache.get("income")
    holder = cache.get("holder")
    lhb = cache.get("lhb")
    margin = cache.get("margin")
    notice = cache.get("notice")

    detail = {"净利增速": None, "增速性质": "缺失", "营收增速": None, "筹码集中": None, "资金关注": None,
              "融资变化": None, "业绩预告": None,
              "净利增速得分": 0, "营收增速得分": 0, "筹码得分": 0, "龙虎榜得分": 0,
              "融资得分": 0, "预告得分": 0}

    # 因子A 净利润增速（10分）
    _, cur_np = _period_value(inc, "NET_PRO_EXCL_MIN_INT_INC", offset=0)
    _, prev_np = _period_value(inc, "NET_PRO_EXCL_MIN_INT_INC", offset=1)
    np_yoy = _yoy(cur_np, prev_np)
    nature, distorted = _growth_nature(cur_np, prev_np)
    detail["净利增速"] = round(np_yoy, 1) if np_yoy is not None else None
    detail["增速性质"] = nature
    if nature == "扭亏":
        # 扭亏为盈：无法算同比，但属于重大改善，给中高分（8）
        detail["净利增速得分"] = 8
    elif nature == "低基数":
        # 低基数高增长：增速失真，封顶 7 分（承认增长但不过度奖励）
        detail["净利增速得分"] = 7
    elif nature == "亏损":
        # 今年仍亏损：0 分
        detail["净利增速得分"] = 0
    elif np_yoy is not None:
        # 正常区间：按增速分档，>200% 封顶 10 分
        capped = min(np_yoy, 200.0)
        if capped > 30:
            detail["净利增速得分"] = 10
        elif capped > 15:
            detail["净利增速得分"] = 8
        elif capped > 0:
            detail["净利增速得分"] = 6
        elif capped > -10:
            detail["净利增速得分"] = 3
        else:
            detail["净利增速得分"] = 0

    # 因子B 营收增速（5分）
    _, cur_rev = _period_value(inc, "OPERA_REV", offset=0)
    _, prev_rev = _period_value(inc, "OPERA_REV", offset=1)
    rev_yoy = _yoy(cur_rev, prev_rev)
    detail["营收增速"] = round(rev_yoy, 1) if rev_yoy is not None else None
    if rev_yoy is not None:
        if rev_yoy > 20:
            detail["营收增速得分"] = 5
        elif rev_yoy > 10:
            detail["营收增速得分"] = 4
        elif rev_yoy > 0:
            detail["营收增速得分"] = 3
        elif rev_yoy > -10:
            detail["营收增速得分"] = 1
        else:
            detail["营收增速得分"] = 0

    # 因子C 筹码集中度（10分）：股东户数环比减少 = 筹码集中
    if holder is not None and hasattr(holder, "columns") and len(holder):
        h = holder.sort_values("HOLDER_ENDDATE")
        if "HOLDER_NUM" in h.columns and len(h) >= 2:
            latest_hn = _safe_float(h["HOLDER_NUM"].iloc[-1])
            prev_hn = _safe_float(h["HOLDER_NUM"].iloc[-2])
            if latest_hn and prev_hn and prev_hn > 0:
                chg = (latest_hn / prev_hn - 1) * 100  # 负 = 户数减少 = 集中
                detail["筹码集中"] = round(chg, 1)
                if chg < -8:
                    detail["筹码得分"] = 10
                elif chg < -3:
                    detail["筹码得分"] = 8
                elif chg < 0:
                    detail["筹码得分"] = 6
                elif chg < 5:
                    detail["筹码得分"] = 3
                else:
                    detail["筹码得分"] = 0

    # 因子D 资金关注度（5分）：近60日是否上龙虎榜 + 净买入
    if lhb is not None and hasattr(lhb, "columns") and len(lhb):
        # 只看近约3个月（90自然日）内的龙虎榜
        recent = lhb
        if "TRADE_DATE" in lhb.columns:
            try:
                td = lhb["TRADE_DATE"].astype(int)
                recent = lhb[td >= int(str(CAL[-1])) - 9000]
            except (TypeError, ValueError):
                recent = lhb
        if len(recent):
            detail["资金关注"] = 1  # 标记：近期上榜
            if "BUY_AMOUNT" in recent.columns and "SELL_AMOUNT" in recent.columns:
                buys = sum(_safe_float(v) or 0 for v in recent["BUY_AMOUNT"])
                sells = sum(_safe_float(v) or 0 for v in recent["SELL_AMOUNT"])
                if buys > sells:
                    detail["龙虎榜得分"] = 5
                else:
                    detail["龙虎榜得分"] = 2
            else:
                detail["龙虎榜得分"] = 3

    # 因子E 融资余额变化（10分）：实证回测(200只/2479对)证明其为反向指标
    #   IC(未来5/10/20日) = -0.062/-0.055/-0.054，t值均<-2.7，显著反向
    #   融资余额暴增 = 杠杆资金高位追涨 = 后续踩踏，应扣分；平稳/回落反而抗跌
    if margin is not None and hasattr(margin, "columns") and len(margin):
        m = margin.sort_values("TRADE_DATE")
        if "BORROW_MONEY_BALANCE" in m.columns and len(m) >= 2:
            latest_bal = _safe_float(m["BORROW_MONEY_BALANCE"].iloc[-1])
            # 取约20个交易日前（或最早）的余额作基准
            base_idx = max(0, len(m) - 21)
            base_bal = _safe_float(m["BORROW_MONEY_BALANCE"].iloc[base_idx])
            if latest_bal and base_bal and base_bal > 0:
                mchg = (latest_bal / base_bal - 1) * 100  # 正 = 余额上升 = 杠杆追涨
                detail["融资变化"] = round(mchg, 1)
                # 反向打分：融资余额大幅上升(追涨)扣分，回落(去杠杆)加分
                if mchg > 15:
                    detail["融资得分"] = 0
                elif mchg > 8:
                    detail["融资得分"] = 2
                elif mchg > 3:
                    detail["融资得分"] = 4
                elif mchg > -5:
                    detail["融资得分"] = 6
                elif mchg > -15:
                    detail["融资得分"] = 8
                else:
                    detail["融资得分"] = 10

    # 因子F 业绩预告（10分）：实证回测(300只/1275事件)证明预增"见光死"、预减"利空出尽"
    #   事件研究(超额收益，控制沪深300 beta)：
    #     预增/扭亏公告后20日超额 +3.55%(胜率53.2%) → 无正向预测力
    #     预减/亏损公告后20日超额 +5.27%(胜率70.1%) → 利空出尽，困境反转
    #   故反转打分：预减/首亏(利空出尽)加分，预增/略增(见光死)扣分
    if notice is not None and hasattr(notice, "columns") and len(notice):
        # 只看最新一条预告（按 ANN_DATE 公告日取最新）
        n = notice.sort_values("ANN_DATE")
        latest = n.iloc[-1]
        p_change_max = _safe_float(latest.get("P_CHANGE_MAX"))
        p_change_min = _safe_float(latest.get("P_CHANGE_MIN"))
        p_typecode = str(latest.get("P_TYPECODE", ""))
        # 去年同期归母净利（保留用于展示，反转打分不再依赖增速）
        prev_parent = _safe_float(latest.get("P_NET_PARENT_FIRM"))
        # 预告增速取区间中值（仅用于展示，不参与反转打分）
        if p_change_max is not None and p_change_min is not None:
            pct_mid = (p_change_max + p_change_min) / 2
        elif p_change_max is not None:
            pct_mid = p_change_max
        elif p_change_min is not None:
            pct_mid = p_change_min
        else:
            pct_mid = None
        detail["业绩预告"] = round(pct_mid, 1) if pct_mid is not None else None
        # 反转打分：按 P_TYPECODE 直接判定方向
        #   P_TYPECODE: 1=预亏 2=首亏 4=扭亏 5=续亏 6=预减 7=略减 9=略减(另一编码)
        #               3=略增 10=预增 11=续盈 12=续增 13=预增(另一编码)
        #   利空型(预亏/首亏/续亏/预减/略减) = 困境反转机会 → 加分
        #   利好型(预增/略增/续盈) = 见光死 → 扣分
        GOOD = {"3", "10", "11", "12", "13"}          # 预增/略增/续盈
        BAD = {"1", "2", "5", "6", "7", "9", "14", "15", "16"}  # 预亏/首亏/续亏/预减/略减
        if p_typecode in BAD:
            # 利空出尽：预减/首亏/预亏，困境反转，重点加分
            if p_typecode in {"1", "2", "5"}:
                detail["预告得分"] = 10     # 预亏/首亏/续亏（反转空间最大）
            else:
                detail["预告得分"] = 7      # 预减/略减
        elif p_typecode in GOOD:
            detail["预告得分"] = 0          # 预增/略增：见光死，不给分
        else:
            # 其他/扭亏型定性表述：中性基础分
            detail["预告得分"] = 3

    total = (detail["净利增速得分"] + detail["营收增速得分"]
             + detail["筹码得分"] + detail["龙虎榜得分"]
             + detail["融资得分"] + detail["预告得分"])
    return total, detail


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="快速模式：仅扫描高价活跃票")
    ap.add_argument("--quick-n", type=int, default=800, help="快速模式扫描数量（默认800）")
    ap.add_argument("--top", type=int, default=20, help="输出Top N（默认20）")
    ap.add_argument("--no-fund", action="store_true", help="跳过财务因子（等价 v4）")
    args = ap.parse_args()

    print("=== 步骤0/5：登录星耀数智并初始化 ===", file=sys.stderr)
    init()
    print(f"  交易日历 {len(CAL)} 天，最新 {CAL[-1]}", file=sys.stderr)

    print("=== 步骤1/5：判断大盘三信号闸门 ===", file=sys.stderr)
    gate_open, gate = check_market_gate()
    print(json.dumps(gate, ensure_ascii=False, indent=2))
    if not gate_open:
        print("\n⚠️  大盘闸门未开：以下所有票仅作'预备弹药池'，暂不买入！", file=sys.stderr)

    print("\n=== 步骤2/5：拉全市场A股列表 ===", file=sys.stderr)
    uni = fetch_universe()
    print(f"  全市场 A 股（沪深主板/创业/科创）{len(uni)} 只", file=sys.stderr)

    if args.quick:
        def _size_key(u):
            pc = u.get("pre_close") or 0
            try:
                return float(pc)
            except (TypeError, ValueError):
                return 0
        uni = sorted(uni, key=_size_key, reverse=True)[:args.quick_n]
        print(f"  [quick] 按昨收价取前 {len(uni)} 只", file=sys.stderr)

    print("\n=== 步骤3/5：第一段——技术面批量拉日线并打分 ===", file=sys.stderr)
    codes = [u["code"] for u in uni]
    results = []
    BATCH = 100
    for i in range(0, len(codes), BATCH):
        chunk = codes[i:i + BATCH]
        try:
            kdata = fetch_kline_batch(chunk, n=70)
        except Exception as e:
            print(f"  批次 {i//BATCH} 拉取失败：{e}", file=sys.stderr)
            continue
        for item in uni[i:i + BATCH]:
            df = kdata.get(item["code"])
            if df is None:
                continue
            try:
                r = eval_one(item, df)
            except Exception:
                r = None
            if r:
                results.append(r)
        print(f"  进度 {min(i + BATCH, len(codes))}/{len(codes)}，已筛出 {len(results)} 只", file=sys.stderr)

    # 去重
    seen = set(); dedup = []
    for r in results:
        if r["code"] not in seen:
            seen.add(r["code"]); dedup.append(r)
    results = dedup
    print(f"\n  技术面通过 {len(results)} 只", file=sys.stderr)

    # ------------------------------------------------------------------
    # 第二段：财务因子（仅对候选池）
    # ------------------------------------------------------------------
    if not args.no_fund and results:
        print(f"\n=== 步骤4/5：第二段——对候选 {len(results)} 只拉财务因子 ===", file=sys.stderr)
        cand_codes = [r["code"] for r in results]
        t0 = time.time()
        fcache = fetch_finance_batch(cand_codes)
        t1 = time.time()
        print(f"  财务数据拉取完成，耗时 {round(t1 - t0, 1)}s", file=sys.stderr)
        for r in results:
            c = r["code"]
            fund_total, fund_detail = score_fundamental(fcache.get(c, {}))
            r["fund_score"] = fund_total
            r["fund_detail"] = fund_detail
            r["score"] = round(r["tech_score"] + fund_total, 1)
    else:
        for r in results:
            r["fund_score"] = 0
            r["fund_detail"] = {"净利增速": None, "增速性质": "缺失", "营收增速": None, "筹码集中": None, "资金关注": None,
                                "融资变化": None, "业绩预告": None}
            r["score"] = r["tech_score"]
        if args.no_fund:
            print(f"\n  [--no-fund] 已跳过财务因子，仅技术面", file=sys.stderr)

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    results.sort(key=lambda x: -x["score"])
    print(f"\n===== 通过筛选 {len(results)} 只，按综合分（技术+财务+资金面）排序 =====\n")
    top = results[:args.top]
    print(f"{'排名':<4}{'代码':<10}{'名称':<10}{'现价':>7}{'回踩买点':>8}{'止损价':>7}{'技术分':>7}{'财务分':>7}{'综合分':>7}")
    for i, r in enumerate(top, 1):
        fd = r["fund_detail"]
        print(f"{i:<4}{r['code']:<10}{r['name']:<10}{r['price']:>7.2f}{r['buy_low']:>8.2f}{r['stop']:>7.2f}"
              f"{r['tech_score']:>7.1f}{r['fund_score']:>7.0f}{r['score']:>7.1f}")
        print(f"     技术[动量{r['factor']['动量']}/回踩{r['factor']['回踩']}/量能{r['factor']['量能']}/趋势{r['factor']['趋势']}/波动{r['factor']['波动']}] "
              f"乖离20={r['b20']}% 20日={r['r20']}% 成交额={r['amount']}亿")
        np_label = fd.get('净利增速')
        np_nature = fd.get('增速性质', '')
        np_str = f"{np_label}%({np_nature})" if np_label is not None and np_nature and np_nature != "正常" else (f"{np_label}%" if np_label is not None else "无")
        print(f"     财务[净利增速{np_str}/营收增速{fd.get('营收增速')}%/筹码{fd.get('筹码集中')}%/龙虎榜{'有' if fd.get('资金关注') else '无'}]")
        margin_str = f"{fd.get('融资变化')}%" if fd.get('融资变化') is not None else "无"
        notice_str = f"{fd.get('业绩预告')}%" if fd.get('业绩预告') is not None else "无"
        print(f"     资金[融资余额变化{margin_str}/业绩预告{notice_str}]")

    out = {"source": "星耀数智 AmazingData", "version": "v6 技术+财务+资金面",
           "gate": gate, "gate_open": gate_open,
           "total": len(results), "top": top, "all": results}
    with open("screen_result_v6.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n结果已存 screen_result_v6.json（共 {len(results)} 只，Top{args.top} 见上）")


if __name__ == "__main__":
    main()
