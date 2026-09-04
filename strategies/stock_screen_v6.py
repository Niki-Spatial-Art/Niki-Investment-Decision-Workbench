#!/usr/bin/env python3
"""全市场A股选股 v6 —— 技术面 + 基本面（财务+资金面）结合版。

v6 相对 v5 的核心变化（胜率优化）：
  在 v5 财务因子基础上，新增两个「资金面」信号：
    · 融资余额变化（10分）：反向指标（实证回测证明），融资余额近20日暴增
      = 杠杆资金高位追涨 = 后续踩踏，应扣分；平稳/回落反而抗跌加分
    · 业绩预告（10分）：反向指标（实证回测），预增公告"见光死"扣分，预减/首亏
      公告"利空出尽"（困境反转）反而加分

  财务/资金面因子权重（实证校准版，基于 IC 回测结论重新分配）：
    营收增速10 + 融资余额10 + 业绩预告10 + 龙虎榜3 = 满分 33 分
    净利增速 0（实证 IC≈0 无效，仅展示）
    筹码集中 0（实证 IC≈0 无效，仅展示）

  横截面 CSRANK 升级（2026-09-03 实证，结论=负向不采纳）：
    尝试把营收/净利增速从"绝对阈值"升级为"截面百分位排名"（CSPCTRANK），
    300 只 / 约 1 万对样本回测：IC 与分层单调性均未提升、反而变差
    （营收60日 IC +0.039→-0.013，分层单调→倒U）。根因=截面排名只是把
    基本面噪声重新排列，未提取新信息。故默认打分维持绝对阈值，
    --csrank 仅作可选开关保留（csrank_factors.py / backtest_csrank.py）。

  回踩买点因子重验（2026-09-03 实证，超额收益法，backtest_ma20_pullback.py）：
    400 只 / 40086 样本，用「个股−沪深300」超额收益逐日扫描回踩区间 [-4%,+6%]。
    结论：回踩深度严格单调，越浅越好——深破位[-4,-2) 20日超额 -0.20%、
    浅破位[-2,0) +0.32%、贴线[0,3) +0.56%、高位[3,6] +1.12%；"不破位 MA10"
    （c>MA10）是核心增强条件（+0.73% vs 破位 +0.14%）；最优买点=高位站上[3,6]
    +不破位MA10（20日 +1.18%、胜率49.3%）。"缩量企稳"是伪信号，放弃。
    印证 v6 现有 c>m20 + 回踩区间约束方向正确（贴线上方企稳而非破位抄底）。

  综合分 = 技术分(0-100) + 财务分(0-33)，满分 133。
  输出保留 tech_score 与 fund_score 两个独立分数，便于技基对比。

财务/资金面接口（星耀数智 AmazingData InfoData）：
  · get_income      -> {code: DataFrame}，字段 REPORTING_PERIOD / REPORT_TYPE(4=年报)
  · get_holder_num  -> DataFrame，字段 HOLDER_ENDDATE / HOLDER_NUM / HOLDER_TOTAL_NUM
  · get_long_hu_bang-> DataFrame，字段 TRADE_DATE / BUY_AMOUNT / SELL_AMOUNT / TOTAL_AMOUNT
  · get_margin_detail-> dict{code: DataFrame}，字段 BORROW_MONEY_BALANCE(融资余额)
                       / PURCH_WITH_BORROW_MONEY(融资买入)
  · get_profit_notice-> DataFrame，字段 P_CHANGE_MAX/MIN(预告增速区间) / ANN_DATE(公告日)
  · get_balance_sheet -> dict{code: DataFrame}，字段 TOT_SHARE_EQUITY_INCL_MIN_INT(归母净资产)
                       / REPORTING_PERIOD / ANN_DATE / STATEMENT_TYPE。
                       接口已恢复（2026-09-03 验证）。ROE 已实证为"排除型因子"
                       （IC20日+0.044/60日-0.034反向，分层倒U型），故不新增独立打分，
                       亏损股已由 _growth_nature 识别归零分，无需 ROE 二次排除。

依赖：
  - AmazingData SDK（tgw），凭据从 Windows 用户级环境变量读取
    AD_USERNAME / AD_PASSWORD / AD_HOST / AD_PORT
  - pandas

用法：
  python stock_screen_v6.py            # 默认跑全市场（技术+财务+资金面）
  python stock_screen_v6.py --quick    # 快速模式（仅扫描成交额前800只）
  python stock_screen_v6.py --no-fund  # 只看技术面（等价 v4，跳过财务）
  python stock_screen_v6.py --csrank   # 财务/资金面因子用「截面相对强弱」打分
                                        # （候选池内排名，替代绝对阈值）
"""
import os
import sys
import json
import time
import argparse
import winreg
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    requests = None

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from csrank_factors import build_all_scores, FACTOR_SPECS as CSRANK_SPECS
except ImportError:
    build_all_scores = None
    CSRANK_SPECS = {}

# ---------------------------------------------------------------------------
# 因子权重唯一来源：factor_registry.json（裁判结论 → 执行器权重）
# ---------------------------------------------------------------------------
# score_stock 的权重与打分阈值全部从本文件读取，代码内不再硬编码。
# 改权重 = 只改 factor_registry.json，无需动本脚本。
# ---------------------------------------------------------------------------

_REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_registry.json")
FACTOR_WEIGHTS = {}          # {因子key: weight}
FACTOR_SCORE_MAP = {}        # {因子key: score_map 原始结构}
_REGISTRY_LOADED = False


def _load_registry():
    """加载 factor_registry.json；失败则回退内置默认权重并告警。"""
    global FACTOR_WEIGHTS, FACTOR_SCORE_MAP, _REGISTRY_LOADED
    default_weights = {
        "turnover_reversal": 40,
        "volume_pullback_shrink": 30,
        "low_volatility_atr": 20,
        "bias_ma20_near_zero": 10,
        "momentum_20_60": 0,
        "trend_ma_alignment": 0,
    }
    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            reg = json.load(f)
        tech = reg.get("技术因子", {})
        for key, cfg in tech.items():
            if not isinstance(cfg, dict):
                continue
            w = cfg.get("weight", 0)
            FACTOR_WEIGHTS[key] = float(w)
            if "score_map" in cfg:
                FACTOR_SCORE_MAP[key] = cfg["score_map"]
        if not FACTOR_WEIGHTS:
            raise ValueError("registry 技术因子为空")
        _REGISTRY_LOADED = True
        return FACTOR_WEIGHTS
    except Exception as e:
        FACTOR_WEIGHTS = dict(default_weights)
        _REGISTRY_LOADED = False
        print(f"[warn] factor_registry.json 加载失败({e})，已回退内置默认权重")
        return FACTOR_WEIGHTS


def _w(name: str, default: float) -> float:
    """取因子权重，registry 未加载时用默认值。"""
    return FACTOR_WEIGHTS.get(name, default)


# 板块共振上下文（main 选股时现拉后写入；score/eval 读取）
_SECTOR_RESEARCH = set()   # research 档行业名（满分加分）
_SECTOR_HOT = set()        # hot 档行业名（半分加分）
_SECTOR_IND_MAP = {}       # {code: 行业名}


def set_sector_context(research_names, hot_names, ind_map):
    global _SECTOR_RESEARCH, _SECTOR_HOT, _SECTOR_IND_MAP
    _SECTOR_RESEARCH = set(research_names or [])
    _SECTOR_HOT = set(hot_names or [])
    _SECTOR_IND_MAP = dict(ind_map or {})


def sector_bonus(code):
    """板块共振加分：research 档满分、hot 档半分、非热门 0。"""
    ind = _SECTOR_IND_MAP.get(code, "")
    if not ind:
        return 0.0, ""
    w = _w("sector_resonance", 10.0)
    if ind in _SECTOR_RESEARCH:
        return float(w), ind
    if ind in _SECTOR_HOT:
        return float(w) * 0.5, ind
    return 0.0, ind


# 启动即加载（模块 import 时执行一次）
_load_registry()

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
    """大盘三信号闸门。

    2026-09-03 实证（backtest_market_gate.py，1359交易日，2015以来）：
    「上证>4000」固定阈值是顶部追高陷阱（开启后沪深300未来60日 -9.53%、
    胜率22.6%；2015牛顶30天60日均跌22.22%、胜率0%）。故 c2 已改为
    「上证站上MA20」动态阈值（60日 -0.50%，仍弱但远好于4000固定值）。
    注：闸门本质是"避免极端追高"的保险丝，非正期望择时信号。
    """
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
    sh = gate["上证指数"]["现价"]; sh_ma = gate["上证指数"]["MA20"]
    zz = gate["中证1000"]["现价"]; zz_ma = gate["中证1000"]["MA20"]
    c1 = hs > hs_ma if hs and hs_ma else False
    c2 = sh > sh_ma if sh and sh_ma else False   # 原"上证>4000"已改动态MA20
    c3 = zz > zz_ma if zz and zz_ma else False
    open3 = c1 and c2 and c3
    return open3, {
        "沪深300现价": hs, "沪深300_MA20": hs_ma, "沪深300站上MA20": c1,
        "上证现价": sh, "上证_MA20": sh_ma, "上证站上MA20": c2,
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
# 板块共振快照（选股时现拉全市场行业涨幅，识别 research/hot 板块）
# ---------------------------------------------------------------------------
# 2026-09-04 起因：中国船舶(600150) +9.99% 涨停暴露 v6 单一票 MA 压制的信号死角——
# 单票技术指标在"板块级合力"（板块共振）面前失效。故引入板块共振加分项：
#   选股时现拉东财/新浪全市场快照（复用 monitor.py 同款接口逻辑，自包含实现
#   避免 import monitor.py 的循环依赖与副作用），按 f100 行业聚合 avg_pct/breadth，
#   复用 whole_market_watchlist.json 的 sector_rules 识别 research/hot 板块，
#   处于这些板块的候选票额外加分（详见 factor_registry.json 的 sector_resonance）。
# 阈值与"板块漏斗"（tools/whole_market_watch_report.py classify_sectors）完全一致，
# 保证同一套"板块涨幅榜前列"口径。
# ---------------------------------------------------------------------------

_SECTOR_RULES_DEFAULT = {
    "minimum_sector_count": 3,
    "research_avg_pct": 1.5,
    "research_breadth_ratio": 0.55,
    "observe_avg_pct": 0.5,
    "observe_breadth_ratio": 0.45,
    "hot_avg_pct": 1.0,
    "hot_breadth_ratio": 0.35,
    "max_research_sectors": 5,
    "max_observe_sectors": 10,
    "max_hot_sectors": 8,
}


def _load_sector_rules():
    """从 whole_market_watchlist.json 读 sector_rules，失败回退默认值。"""
    try:
        wl_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "watchlists", "whole_market_watchlist.json")
        with open(wl_path, "r", encoding="utf-8") as f:
            rules = json.load(f).get("sector_rules", {})
        merged = dict(_SECTOR_RULES_DEFAULT)
        merged.update({k: v for k, v in rules.items() if k in merged})
        return merged
    except Exception:
        return dict(_SECTOR_RULES_DEFAULT)


def _em_get(url, params, timeout=3, retries=1):
    """东财 clist 接口（push2 + push2delay fallback）。"""
    if requests is None:
        raise RuntimeError("requests 未安装")
    headers = {"User-Agent": "Mozilla/5.0 ETF Strategy Monitor",
               "Referer": "https://quote.eastmoney.com/"}
    urls = [url]
    if "push2.eastmoney.com" in url:
        urls.append(url.replace("push2.eastmoney.com", "push2delay.eastmoney.com"))
    last = None
    for _ in range(retries):
        for cand in urls:
            try:
                r = requests.get(cand, params=params, headers=headers, timeout=timeout)
                r.raise_for_status()
                return r.json()
            except requests.RequestException as exc:
                last = exc
        time.sleep(1)
    raise last


def _fetch_snapshot_page_eastmoney(page, page_size=100):
    data = _em_get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        {"pn": page, "pz": page_size, "po": "1", "np": "1", "fltt": "2",
         "invt": "2", "fid": "f6",
         "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
         "fields": "f12,f14,f2,f3,f6,f8,f10,f17,f18,f100",
         "ut": "fa5fd1943c7b386f172d6893dbfba10b"},
    ).get("data") or {}
    return data.get("diff") or []


def _parse_sina_rows(text):
    rows = []
    try:
        payload = json.loads(text or "[]")
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, list):
        for v in payload:
            if not isinstance(v, dict):
                continue
            code = str(v.get("code") or v.get("symbol", "")[-6:])
            if len(code) != 6:
                continue
            rows.append({"f12": code, "f14": v.get("name") or code,
                         "f3": v.get("changepercent"),
                         "f6": v.get("amount"), "f100": v.get("industry") or ""})
        return rows
    import re as _re
    for m in _re.finditer(r"\{([^{}]+)\}", text or ""):
        vals = {}
        for item in _re.finditer(r"([A-Za-z_][A-Za-z0-9_]*):(?:\"([^\"]*)\"|([^,}]+))", m.group(1)):
            key = item.group(1)
            vals[key] = item.group(2) if item.group(2) is not None else item.group(3)
        code = str(vals.get("code") or vals.get("symbol", "")[-6:])
        if len(code) != 6:
            continue
        rows.append({"f12": code, "f14": vals.get("name") or code,
                     "f3": vals.get("changepercent"), "f6": vals.get("amount"),
                     "f100": vals.get("industry") or ""})
    return rows


def _fetch_snapshot_page_sina(page, page_size=100):
    r = requests.get(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
        params={"page": page, "num": min(page_size, 200), "sort": "amount",
                "asc": "0", "node": "hs_a", "symbol": "", "_s_r_a": "init"},
        headers={"User-Agent": "Mozilla/5.0 ETF Strategy Monitor",
                 "Referer": "https://vip.stock.finance.sina.com.cn/"},
        timeout=3)
    r.raise_for_status()
    r.encoding = "gbk"
    return _parse_sina_rows(r.text)


def _sfloat(v):
    try:
        if v in (None, "-", ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_sector_resonance(max_pages=30, page_size=100):
    """选股时现拉全市场快照，返回 (code_to_industry, hot_sector_names, research_sector_names)。

    返回：
      code_to_industry: {6位代码: 行业名}
      hot_sectors:      热门板块名单（research + hot 两档，按 avg_pct 排序）
      detail:           各行业 avg_pct/breadth 明细（供诊断输出）
    数据源东财优先、新浪兜底，全失败时返回空映射（板块共振加分自动失效，不阻塞选股）。
    """
    code_to_ind = {}
    ind_stats = {}
    scanned = 0
    sources = ["eastmoney", "sina"]
    for source in sources:
        empty = 0
        for page in range(1, max_pages + 1):
            try:
                if source == "eastmoney":
                    rows = _fetch_snapshot_page_eastmoney(page, page_size)
                else:
                    rows = _fetch_snapshot_page_sina(page, page_size)
            except Exception:
                break
            if not rows:
                empty += 1
                if empty >= 2:
                    break
                continue
            for row in rows:
                code = str(row.get("f12") or "").strip()
                name = str(row.get("f14") or "").strip()
                if len(code) != 6 or code in code_to_ind:
                    continue
                if any(t in name for t in ("ST", "*ST", "退", "N", "C")):
                    continue
                pct = _sfloat(row.get("f3"))
                if pct is None:
                    continue
                scanned += 1
                ind = str(row.get("f100") or "").strip() or "未分类"
                code_to_ind[code] = ind
                st = ind_stats.setdefault(ind, {"count": 0, "advancers": 0, "pct_sum": 0.0, "amount": 0.0})
                st["count"] += 1
                st["advancers"] += int(pct > 0)
                st["pct_sum"] += pct
                st["amount"] += _sfloat(row.get("f6")) or 0.0
        if scanned >= 4000:
            break

    rules = _load_sector_rules()
    min_cnt = int(rules.get("minimum_sector_count", 3))
    research, hot = [], []
    detail = []
    for ind, st in ind_stats.items():
        cnt = st["count"]
        if cnt < min_cnt:
            continue
        breadth = st["advancers"] / cnt if cnt else 0
        avg_pct = st["pct_sum"] / cnt if cnt else 0.0
        detail.append({"industry": ind, "count": cnt, "avg_pct": round(avg_pct, 2),
                       "breadth_ratio": round(breadth, 4), "amount": round(st["amount"], 0)})
        if avg_pct >= float(rules.get("research_avg_pct", 1.5)) and breadth >= float(rules.get("research_breadth_ratio", 0.55)):
            research.append((ind, avg_pct))
        elif avg_pct >= float(rules.get("observe_avg_pct", 0.5)) and breadth >= float(rules.get("observe_breadth_ratio", 0.45)):
            hot.append((ind, avg_pct))  # observe 档也纳入"加分"（板块涨幅榜前列）
        elif avg_pct >= float(rules.get("hot_avg_pct", 1.0)) and breadth >= float(rules.get("hot_breadth_ratio", 0.35)):
            hot.append((ind, avg_pct))
    research.sort(key=lambda x: -x[1])
    hot.sort(key=lambda x: -x[1])
    research = [x for x in research if x[0] != "未分类"]
    hot = [x for x in hot if x[0] != "未分类"]
    research_names = [x[0] for x in research[: int(rules.get("max_research_sectors", 5))]]
    hot_names = [x[0] for x in hot[: int(rules.get("max_hot_sectors", 8))]]
    detail.sort(key=lambda x: -x["avg_pct"])
    return code_to_ind, hot_names, research_names, detail, scanned


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

    # 量能基础量（供成交量收缩反转与回踩缩量共用）
    v5 = float(vol.iloc[-5:].mean())
    v20 = float(vol.iloc[-20:].mean())
    vol_ratio = v5 / v20 if v20 else 1.0
    # 成交量收缩变化率：近20日均量 / 前20日均量 - 1（%），负=缩量=关注度低=未来超额高
    if len(vol) >= 40:
        v_prev20 = float(vol.iloc[-40:-20].mean())
    else:
        v_prev20 = v20
    turn_chg = (v20 / v_prev20 - 1) * 100 if v_prev20 else 0.0

    # ------------------------------------------------------------------
    # 因子权重唯一来源：factor_registry.json（FACTOR_WEIGHTS，模块加载时读入）。
    #   成交量收缩反转 40（IC -0.108 强有效）+ 回踩缩量 30（缩量组胜率48.6% vs 放量33.8%）
    #   + 低振幅 20 + 乖离贴0 10。动量/趋势已实证强反向（IC -0.17/-0.29/-0.11），归 0 仅展示。
    #   权重值与分档阈值 = FACTOR_WEIGHTS + FACTOR_SCORE_MAP，改权重只改 registry。
    # ------------------------------------------------------------------
    W_TURN = _w("turnover_reversal", 40.0)
    W_VOL = _w("volume_pullback_shrink", 30.0)
    W_VOL2 = _w("low_volatility_atr", 20.0)
    W_BIAS = _w("bias_ma20_near_zero", 10.0)

    # 因子1 成交量收缩反转（40分）：缩量(负变化)给高分
    # 分档系数按权重等比缩放，满分=W_TURN
    if turn_chg <= -40:
        turn_score = W_TURN * 1.0
    elif turn_chg <= -15:
        turn_score = W_TURN * 0.8
    elif turn_chg <= 0:
        turn_score = W_TURN * 0.6
    elif turn_chg <= 15:
        turn_score = W_TURN * 0.3
    elif turn_chg <= 50:
        turn_score = W_TURN * 0.15
    else:
        turn_score = 0.0

    # 因子2 回踩缩量（30分）：乖离MA20贴线 + 缩量，才是有效"回踩洗盘"信号
    if b20 < 1.0 and vol_ratio < 0.85:
        vol_score = W_VOL * 1.0
    elif b20 < 1.0 and vol_ratio < 1.1:
        vol_score = W_VOL * 0.6
    elif vol_ratio < 0.85:
        vol_score = W_VOL * 0.4
    else:
        vol_score = W_VOL * 0.2

    # 因子3 低振幅（20分）：振幅越小越稳（回踩洗盘特征）。证据偏定性，试运行权重。
    atr = float((high.iloc[-10:] - low.iloc[-10:]).mean())
    atr_pct = atr / c * 100 if c else 0
    vol_score2 = max(0.0, min(W_VOL2, W_VOL2 - (atr_pct - 3) * 2))

    # 因子4 乖离贴0（10分）：乖离MA20越接近0越稳（配合性条件，非独立买点）
    bias_score = max(0.0, W_BIAS - abs(b20) * 2)

    # 动量/趋势：已实证反向，归 0（仅展示，供参考）
    mom_score = 0.0
    trend = 0
    if m5 > m10: trend += 5
    if m10 > m20: trend += 5
    if c > m20: trend += 5
    if c > m60: trend += 5
    trend_score = 0.0  # 反向，归0

    total = turn_score + vol_score + vol_score2 + bias_score
    return {"total": round(total, 1),
            "turn": round(turn_score, 1), "vol": round(vol_score, 1),
            "vol2": round(vol_score2, 1), "bias": round(bias_score, 1),
            "mom": round(mom_score, 1), "trend": round(trend_score, 1),
            "b20": round(b20, 2), "r20": round(r20, 2), "r60": round(r60, 2),
            "hi60": round(hi60, 2), "m20": round(m20, 2), "m5": round(m5, 2),
            "m10": round(m10, 2), "m60": round(m60, 2),
            "turn_chg": round(turn_chg, 2), "vol_ratio": round(vol_ratio, 3),
            "atr_pct": round(atr_pct, 2), "trend_raw": trend}


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
    sbonus, industry = sector_bonus(code)
    return {"code": code, "name": name, "pct": round(pct, 2), "amount": round(amount / 1e8, 2),
            "price": round(c, 2), "industry": industry,
            "sector_bonus": round(sbonus, 1),
            "tech_score": round(sc["total"] + sbonus, 1),
            "factor": {"成交量收缩": sc["turn"], "回踩缩量": sc["vol"],
                       "低振幅": sc["vol2"], "乖离": sc["bias"],
                       "动量": sc["mom"], "趋势": sc["trend"],
                       "板块共振": sbonus},
            "b20": sc["b20"], "r20": sc["r20"], "r60": sc["r60"],
            "turn_chg": sc["turn_chg"], "vol_ratio": sc["vol_ratio"],
            "atr_pct": sc["atr_pct"],
            "ma20": sc["m20"], "drawdown60": round(drawdown60, 1),
            "buy": buy, "buy_low": buy_low, "stop": stop}


# ---------------------------------------------------------------------------
# 财务/资金面因子打分（基本面，实证校准后满分 33）
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
    """对单只候选计算财务/资金面因子分（0-33），返回 (总分, 明细dict)。

    v6 因子权重经全量 IC 实证回测校准（提高胜率的核心抓手）：
      · 融资余额变化（10分）：反向指标，融资余额近20日暴增 = 杠杆追涨应扣分
      · 业绩预告（10分）：反向指标，预增公告"见光死"扣分，预减/首亏
        公告"利空出尽"（困境反转）反而加分
      · 营收增速（10分）：唯一弱有效财务因子(IC 60日+0.031)，升权 5→10
      · 净利增速（0分）：实证 IC≈0 无效，降权归零仅展示
      · 筹码集中（0分）：实证 IC≈0 无效，降权归零仅展示
      · 龙虎榜（3分）：未单独实证，保守从 5 分降至 3 分
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

    # 因子A 净利润增速（实证无效，IC≈0，已降权为 0 分，仅展示）
    #   回测结论(200只/6614对)：IC 未来20日+0.009/60日+0.005 = 纯噪音。
    #   市场对财报定价极快，"好业绩"早已反映在股价里，无法靠净利增速跑赢。
    _, cur_np = _period_value(inc, "NET_PRO_EXCL_MIN_INT_INC", offset=0)
    _, prev_np = _period_value(inc, "NET_PRO_EXCL_MIN_INT_INC", offset=1)
    np_yoy = _yoy(cur_np, prev_np)
    nature, distorted = _growth_nature(cur_np, prev_np)
    detail["净利增速"] = round(np_yoy, 1) if np_yoy is not None else None
    detail["增速性质"] = nature
    detail["净利增速得分"] = 0   # 无效因子，不参与排序（仅展示）

    # 因子B 营收增速（弱有效，升权重 5→10 分）
    #   回测结论：IC 未来60日 +0.031，是四个老财务因子里唯一过线的。
    #   营收增长比利润增长更"硬"(不易被会计调节)，市场反应稍慢，有微弱信号。
    _, cur_rev = _period_value(inc, "OPERA_REV", offset=0)
    _, prev_rev = _period_value(inc, "OPERA_REV", offset=1)
    rev_yoy = _yoy(cur_rev, prev_rev)
    detail["营收增速"] = round(rev_yoy, 1) if rev_yoy is not None else None
    if rev_yoy is not None:
        if rev_yoy > 30:
            detail["营收增速得分"] = 10
        elif rev_yoy > 20:
            detail["营收增速得分"] = 8
        elif rev_yoy > 10:
            detail["营收增速得分"] = 6
        elif rev_yoy > 0:
            detail["营收增速得分"] = 4
        elif rev_yoy > -10:
            detail["营收增速得分"] = 2
        else:
            detail["营收增速得分"] = 0

    # 因子C 筹码集中度（实证无效，IC≈0，已降权为 0 分，仅展示）
    #   回测结论(200只/13699对)：IC 未来20日-0.025/60日-0.001 = 噪音。
    #   "股东户数减少=筹码集中=看多"在A股短线不成立(户数减少也可能是无人问津)。
    if holder is not None and hasattr(holder, "columns") and len(holder):
        h = holder.sort_values("HOLDER_ENDDATE")
        if "HOLDER_NUM" in h.columns and len(h) >= 2:
            latest_hn = _safe_float(h["HOLDER_NUM"].iloc[-1])
            prev_hn = _safe_float(h["HOLDER_NUM"].iloc[-2])
            if latest_hn and prev_hn and prev_hn > 0:
                chg = (latest_hn / prev_hn - 1) * 100  # 负 = 户数减少 = 集中
                detail["筹码集中"] = round(chg, 1)
                detail["筹码得分"] = 0   # 无效因子，不参与排序（仅展示）

    # 因子D 资金关注度（3分）：近60日是否上龙虎榜 + 净买入
    #   未单独实证回测，保守从 5 分降至 3 分（避免未验证因子权重过高稀释技术面）
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
                    detail["龙虎榜得分"] = 3
                else:
                    detail["龙虎榜得分"] = 1
            else:
                detail["龙虎榜得分"] = 2

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
# 截面相对强弱打分（CSRANK 升级，候选池内排名）
# ---------------------------------------------------------------------------

def score_fundamental_csrank(caches: dict):
    """对候选池整体做截面相对强弱打分（替代单票绝对阈值打分）。

    caches : {code: cache}（fetch_finance_batch 的输出）。
    先把每只票的绝对因子值抽出来（营收增速/融资变化/预告方向），
    再在候选池内做截面百分位排名，映射为 0~33 的相对分数。
    返回 {code: (fund_total, fund_detail)}，detail 结构与 score_fundamental 兼容，
    额外带一个 "截面排名" 说明字段。

    注意：龙虎榜（3分）是离散事件型因子，截面排名意义弱，保留绝对打分；
          净利/筹码已归零不参与。截面排名只作用于营收/融资/预告三个连续因子。
    """
    # 1. 先复用绝对打分逻辑，拿到每只票的绝对因子值（营收增速/融资变化/预告方向）
    abs_detail = {}
    for code, cache in caches.items():
        _, d = score_fundamental(cache)
        abs_detail[code] = d

    # 2. 抽出三个连续因子的绝对数值，供截面排名
    #    营收增速（正向，越大越好）
    #    融资变化（反向，越大越差）—— 注意方向在 csrank 的 direction 里已配 -1
    #    预告方向：P_TYPECODE 是分类值，截面排名无意义，改用「反转打分后的分数」
    #             但反转分数本身是绝对阈值，这里改为：预告利空型记"强"信号，
    #             直接复用绝对打分的"预告得分"作为强度，再做截面排名。
    factor_values = {}
    for code, d in abs_detail.items():
        fv = {}
        # 营收增速（%）—— 正向
        if d.get("营收增速") is not None:
            fv["营收增速"] = d["营收增速"]
        # 融资变化（%）—— 反向
        if d.get("融资变化") is not None:
            fv["融资变化"] = d["融资变化"]
        # 预告：用绝对打分的"预告得分"（0/3/7/10）作为截面强度
        # 预告得分越高 = 越"利空出尽/困境反转"，是正向的选股信号
        fv["预告方向"] = float(d.get("预告得分", 0))
        factor_values[code] = fv

    # 3. 截面百分位排名 + 映射分数
    if not build_all_scores or not factor_values:
        # 兜底：模块不可用时回退绝对打分
        return {c: score_fundamental(cache) for c, cache in caches.items()}

    cs_scores = build_all_scores(factor_values)

    # 4. 组装返回：营收/融资/预告用截面分，龙虎榜保留绝对分，净利/筹码归零
    result = {}
    for code, d in abs_detail.items():
        detail = dict(d)  # 复制绝对 detail，保留展示字段
        rev_cs = cs_scores.get("营收增速", pd.Series()).get(code, float("nan")) if cs_scores.get("营收增速") is not None else float("nan")
        mar_cs = cs_scores.get("融资变化", pd.Series()).get(code, float("nan")) if cs_scores.get("融资变化") is not None else float("nan")
        pre_cs = cs_scores.get("预告方向", pd.Series()).get(code, float("nan")) if cs_scores.get("预告方向") is not None else float("nan")

        rev_cs = 0 if rev_cs != rev_cs else rev_cs  # NaN -> 0
        mar_cs = 0 if mar_cs != mar_cs else mar_cs
        pre_cs = 0 if pre_cs != pre_cs else pre_cs

        detail["营收增速得分"] = round(rev_cs, 1)
        detail["融资得分"] = round(mar_cs, 1)
        detail["预告得分"] = round(pre_cs, 1)
        detail["截面排名"] = True

        total = (detail["净利增速得分"] + detail["营收增速得分"]
                 + detail["筹码得分"] + detail["龙虎榜得分"]
                 + detail["融资得分"] + detail["预告得分"])
        result[code] = (total, detail)
    return result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="快速模式：仅扫描高价活跃票")
    ap.add_argument("--quick-n", type=int, default=800, help="快速模式扫描数量（默认800）")
    ap.add_argument("--top", type=int, default=20, help="输出Top N（默认20）")
    ap.add_argument("--no-fund", action="store_true", help="跳过财务因子（等价 v4）")
    ap.add_argument("--csrank", action="store_true", help="财务/资金面因子用截面相对强弱打分")
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

    # ------------------------------------------------------------------
    # 步骤2.5：板块共振快照（选股时现拉全市场行业涨幅，识别热门板块）
    #   2026-09-04 中国船舶涨停教训：单一票 MA 压制漏判，需补板块级合力维度。
    #   复用板块漏斗 sector_rules 口径（板块涨幅榜前列）给候选票加分。
    # ------------------------------------------------------------------
    print("\n=== 步骤2.5/5：板块共振快照（现拉全市场行业涨幅） ===", file=sys.stderr)
    sector_ctx = {"research": [], "hot": [], "scanned": 0, "detail": []}
    try:
        ind_map, hot_names, research_names, detail, scanned = fetch_sector_resonance()
        set_sector_context(research_names, hot_names, ind_map)
        sector_ctx.update({"research": research_names, "hot": hot_names,
                           "scanned": scanned, "detail": detail})
        print(f"  已拉 {scanned} 只，行业映射 {len(ind_map)} 只", file=sys.stderr)
        print(f"  research 板块：{research_names or '无'}", file=sys.stderr)
        print(f"  hot 板块：{hot_names or '无'}", file=sys.stderr)
    except Exception as e:
        print(f"  [warn] 板块共振快照失败({e})，本次板块共振加分自动失效（不阻塞选股）", file=sys.stderr)
        set_sector_context([], [], {})

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

        if args.csrank and build_all_scores:
            # 截面相对强弱打分：对候选池整体排名后统一映射分数
            print(f"  [--csrank] 财务/资金面因子用截面相对强弱打分", file=sys.stderr)
            cs_result = score_fundamental_csrank(fcache)
            for r in results:
                c = r["code"]
                fund_total, fund_detail = cs_result.get(c, score_fundamental(fcache.get(c, {})))
                r["fund_score"] = fund_total
                r["fund_detail"] = fund_detail
                r["score"] = round(r["tech_score"] + fund_total, 1)
        else:
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
        ind = r.get("industry") or ""
        sbonus = r.get("sector_bonus", 0)
        ind_str = f"[{ind}]{'+'+str(sbonus) if sbonus else ''}" if ind else ""
        print(f"{i:<4}{r['code']:<10}{r['name']:<10}{r['price']:>7.2f}{r['buy_low']:>8.2f}{r['stop']:>7.2f}"
              f"{r['tech_score']:>7.1f}{r['fund_score']:>7.0f}{r['score']:>7.1f}  {ind_str}")
        print(f"     技术[成交量收缩{r['factor']['成交量收缩']}/回踩缩量{r['factor']['回踩缩量']}/低振幅{r['factor']['低振幅']}/乖离{r['factor']['乖离']}/板块共振{r['factor'].get('板块共振', 0)}] "
              f"乖离20={r['b20']}% 量收缩={r.get('turn_chg', '?')}% 量比={r.get('vol_ratio', '?')} 振幅={r.get('atr_pct', '?')}% 成交额={r['amount']}亿")
        np_label = fd.get('净利增速')
        np_nature = fd.get('增速性质', '')
        np_str = f"{np_label}%({np_nature})" if np_label is not None and np_nature and np_nature != "正常" else (f"{np_label}%" if np_label is not None else "无")
        print(f"     财务[净利增速{np_str}/营收增速{fd.get('营收增速')}%/筹码{fd.get('筹码集中')}%/龙虎榜{'有' if fd.get('资金关注') else '无'}]")
        margin_str = f"{fd.get('融资变化')}%" if fd.get('融资变化') is not None else "无"
        notice_str = f"{fd.get('业绩预告')}%" if fd.get('业绩预告') is not None else "无"
        print(f"     资金[融资余额变化{margin_str}/业绩预告{notice_str}]")

    trade_date = int(str(CAL[-1]))  # 交易日历最新交易日（权威交易日，非本地时钟）
    out = {"source": "星耀数智 AmazingData", "version": "v6 技术+财务+资金面",
           "trade_date": trade_date,
           "gate": gate, "gate_open": gate_open,
           "sector_resonance": sector_ctx,
           "total": len(results), "top": top, "all": results}
    with open("screen_result_v6.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n结果已存 screen_result_v6.json（共 {len(results)} 只，Top{args.top} 见上）")


if __name__ == "__main__":
    main()
