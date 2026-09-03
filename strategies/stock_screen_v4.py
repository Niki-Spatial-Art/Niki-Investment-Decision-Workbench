#!/usr/bin/env python3
"""全市场A股选股 v4 —— 星耀数智数据源版。

v4 相对 v3 的核心变化：
  数据层：腾讯/东财接口 → 星耀数智 AmazingData SDK（你的机构级数据源）
    · 大盘闸门   ：星耀日线(query_kline)算 MA20
    · 全市场列表 ：星耀 get_code_list + get_code_info（拿到名称/上市日/涨跌停价）
    · 日 K 线    ：星耀 query_kline(period=day)，批量拉取
    · 技术指标   ：星耀 TimeSeriesFunction（MA/EMA/HHV/LLV/REF 通达信同款函数）
  逻辑层：完整保留 v3 的「三闸门 + 多因子打分 + 风险排除 + 回踩买点/止损」

三闸门（大盘择时）：
  ① 沪深300 站上 MA20  ② 上证指数 站上 4000  ③ 中证1000 站上 MA20
  三信号全开才允许买入，否则所有票仅作"预备弹药池"。

多因子打分（满分 100，平衡权重）：
  动量30 + 回踩30 + 量能20 + 趋势20（波动10 为附加参考），详见 score_stock()。

依赖：
  - AmazingData SDK（tgw），凭据从 Windows 用户级环境变量读取
    AD_USERNAME / AD_PASSWORD / AD_HOST / AD_PORT
  - pandas（SDK 返回 DataFrame）

用法：
  python stock_screen_v4.py            # 默认跑全市场
  python stock_screen_v4.py --quick    # 快速模式（仅扫描成交额前800只）
"""
import os
import sys
import json
import time
import argparse
import winreg
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def init():
    global AD, CAL, MARKET, TSF, BASE
    AD = login()
    CAL = AD.BaseData().get_calendar()
    MARKET = AD.MarketData(CAL)
    TSF = AD.TimeSeriesFunction
    BASE = AD.BaseData()
    return CAL


# ---------------------------------------------------------------------------
# 大盘三信号闸门
# ---------------------------------------------------------------------------

def _ma(series, n):
    return float(series.iloc[-n:].mean()) if len(series) >= n else None


def check_market_gate():
    """用星耀日线算三大指数的 MA20，判断三信号闸门。"""
    code_map = {"沪深300": "000300.SH", "上证指数": "000001.SH", "中证1000": "000852.SH"}
    # 拉 70 个交易日足够算 MA20
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
    """用星耀 get_code_info 拿全市场 A 股：代码/名称/上市日/涨跌停价/昨收。"""
    info = BASE.get_code_info(security_type="EXTRA_STOCK_A")
    rows = []
    for idx, r in info.iterrows():
        code = str(idx)
        # 只保留沪深主板/创业板/科创板（排除北交所 8 开头、B 股 2/9 开头等）
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
    """批量拉日线，返回 {code: DataFrame}。n 个交易日。"""
    end = int(str(CAL[-1]))
    begin = int(str(CAL[-n]))
    return MARKET.query_kline(code_list=codes, begin_date=begin,
                              end_date=end, period=10008)


# ---------------------------------------------------------------------------
# 多因子打分（平衡权重，满分 100）
# ---------------------------------------------------------------------------

def score_stock(df):
    """df = 单只股票的日线 DataFrame（列含 open/high/low/close/volume/amount）。
    返回综合分 0-100 及明细，或 None（数据不足）。"""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float)
    if len(close) < 61:
        return None
    c = float(close.iloc[-1])

    # 用 SDK 的 TimeSeriesFunction 算均线（通达信同款）
    m5 = float(TSF.MA(close, 5).iloc[-1])
    m10 = float(TSF.MA(close, 10).iloc[-1])
    m20 = float(TSF.MA(close, 20).iloc[-1])
    m60 = float(TSF.MA(close, 60).iloc[-1])
    if not all(v == v for v in (m5, m10, m20, m60)):  # NaN 检查
        return None

    b20 = (c / m20 - 1) * 100                 # 乖离 MA20
    r20 = (c / float(close.iloc[-21]) - 1) * 100
    r60 = (c / float(close.iloc[-61]) - 1) * 100
    hi60 = float(TSF.HHV(high, 60).iloc[-1])

    # 因子1 动量（20/60日涨幅）满分30
    mom = 0
    if r20 > 0 and r60 > 0:
        mom = min(r20, 25)
    elif r20 > 0 and r60 <= 0:
        mom = r20 * 0.4
    else:
        mom = r20 * 0.4
    mom_score = max(0, min(30, 15 + mom))

    # 因子2 回踩度（乖离MA20）满分30
    bias_score = max(0, 30 - abs(b20) * 6)

    # 因子3 量能（回踩缩量=洗盘）满分20
    v5 = float(vol.iloc[-5:].mean())
    v20 = float(vol.iloc[-20:].mean())
    vol_ratio = v5 / v20 if v20 else 1
    if b20 < 1.0:
        vol_score = 20 if vol_ratio < 0.85 else (10 if vol_ratio < 1.1 else 3)
    else:
        vol_score = 12 if vol_ratio < 1.2 else 6
    vol_score = min(20, vol_score)

    # 因子4 趋势强度（多头排列）满分20
    trend = 0
    if m5 > m10: trend += 5
    if m10 > m20: trend += 5
    if c > m20: trend += 5
    if c > m60: trend += 5
    trend_score = min(20, trend)

    # 因子5 波动（振幅）满分10（附加参考）
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
# 逐票评估（在已拉取的日线 DataFrame 上计算）
# ---------------------------------------------------------------------------

def eval_one(item, df):
    code = item["code"]
    name = item["name"]
    # 风险排除：ST / 退市 / 次新
    if "ST" in name.upper() or "退" in name:
        return None
    list_day = item.get("list_day")
    if list_day:
        try:
            ld = int(str(list_day))
            # 排除上市不足 120 个自然日的次新股（数据不足 + 波动不稳）
            if ld >= 20260501:   # 上市未满约4个月
                return None
        except (TypeError, ValueError):
            pass
    # 涨跌幅与成交额（由日线最后一天算）
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
    if amount < 2e8:              # 成交额 < 2 亿，流动性不足
        return None
    if pct > 9.5 or pct < -5:     # 涨停不追、大跌不接
        return None

    sc = score_stock(df)
    if sc is None:
        return None

    # 基础形态筛选（保留 v3 核心）
    if sc["b20"] > 6.0 or sc["b20"] < -4.0:
        return None
    if sc["m5"] <= sc["m10"]:
        return None
    if c <= sc["m20"]:
        return None
    if pct > 6.0:
        return None

    # 风险排除①：60日高点回撤超30% = 腰斩反弹陷阱
    drawdown60 = (c / sc["hi60"] - 1) * 100
    if drawdown60 < -30:
        return None
    # 风险排除②：近20日涨超20%且当日滞涨（出货迹象）
    if sc["r20"] > 20 and pct < 1.0:
        return None

    buy = round(sc["m10"], 2)
    buy_low = round(min(sc["m10"], c * 0.985), 2)
    stop = round(buy_low * 0.965, 2)
    return {"code": code, "name": name, "pct": round(pct, 2), "amount": round(amount / 1e8, 2),
            "price": round(c, 2), "industry": "",
            "score": sc["total"],
            "factor": {"动量": sc["mom"], "回踩": sc["bias"], "量能": sc["vol"],
                       "趋势": sc["trend"], "波动": sc["vol2"]},
            "b20": sc["b20"], "r20": sc["r20"], "r60": sc["r60"],
            "ma20": sc["m20"], "drawdown60": round(drawdown60, 1),
            "buy": buy, "buy_low": buy_low, "stop": stop}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="快速模式：仅扫描高价活跃票")
    ap.add_argument("--quick-n", type=int, default=800, help="快速模式扫描数量（默认800）")
    ap.add_argument("--top", type=int, default=20, help="输出Top N（默认20）")
    args = ap.parse_args()

    print("=== 步骤0/4：登录星耀数智并初始化 ===", file=sys.stderr)
    init()
    print(f"  交易日历 {len(CAL)} 天，最新 {CAL[-1]}", file=sys.stderr)

    print("=== 步骤1/4：判断大盘三信号闸门 ===", file=sys.stderr)
    gate_open, gate = check_market_gate()
    print(json.dumps(gate, ensure_ascii=False, indent=2))
    if not gate_open:
        print("\n⚠️  大盘闸门未开：以下所有票仅作'预备弹药池'，暂不买入！", file=sys.stderr)

    print("\n=== 步骤2/4：拉全市场A股列表 ===", file=sys.stderr)
    uni = fetch_universe()
    print(f"  全市场 A 股（沪深主板/创业/科创）{len(uni)} 只", file=sys.stderr)

    # 快速模式：按昨收价 × 预估股本近似规模排序，优先取"价格较高"活跃票（创业板/科创板不会被截断）
    # 说明：无股本数据时用昨收价作弱代理；更严谨的做法是先拉全市场日线按成交额筛，此处折中
    if args.quick:
        def _size_key(u):
            pc = u.get("pre_close") or 0
            try:
                return float(pc)
            except (TypeError, ValueError):
                return 0
        uni = sorted(uni, key=_size_key, reverse=True)[:args.quick_n]
        print(f"  [quick] 按昨收价取前 {len(uni)} 只", file=sys.stderr)

    print("\n=== 步骤3/4：批量拉日线并打分 ===", file=sys.stderr)
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

    results.sort(key=lambda x: -x["score"])
    print(f"\n===== 通过筛选 {len(results)} 只，按综合分排序 =====\n")
    top = results[:args.top]
    print(f"{'排名':<4}{'代码':<10}{'名称':<10}{'现价':>7}{'回踩买点':>8}{'止损价':>7}{'综合分':>7}")
    for i, r in enumerate(top, 1):
        print(f"{i:<4}{r['code']:<10}{r['name']:<10}{r['price']:>7.2f}{r['buy_low']:>8.2f}{r['stop']:>7.2f}{r['score']:>7.1f}")
        print(f"     因子[动量{r['factor']['动量']}/回踩{r['factor']['回踩']}/量能{r['factor']['量能']}/趋势{r['factor']['趋势']}/波动{r['factor']['波动']}] "
              f"乖离20={r['b20']}% 20日={r['r20']}% 60日回撤={r['drawdown60']}% 成交额={r['amount']}亿")

    out = {"source": "星耀数智 AmazingData", "gate": gate, "gate_open": gate_open,
           "total": len(results), "top": top, "all": results}
    with open("screen_result_v4.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n结果已存 screen_result_v4.json（共 {len(results)} 只，Top{args.top} 见上）")


if __name__ == "__main__":
    main()
