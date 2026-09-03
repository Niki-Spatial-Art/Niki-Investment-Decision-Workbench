"""
星耀数智 AmazingData 连接器模块

独立封装星耀 SDK 的登录、行情、财务、基础数据接口。
遵循项目连接器原则：
- 凭据仅从环境变量读取，不写入代码
- 缺失数据视为风险态
- 返回结构化 dict/list，携带 source/timestamp

环境变量（兼容 AD_* 和 XINGYAO_* 两套命名）：
  AD_USERNAME / XINGYAO_USER
  AD_PASSWORD / XINGYAO_PASSWORD
  AD_HOST     / XINGYAO_HOST     (默认: 101.230.159.234)
  AD_PORT     / XINGYAO_PORT     (默认: 8600)

SDK 安装路径（用于 sys.path 注入）：
  XINGYAO_SDK_PATHS  (分号分隔的目录列表)
"""

import os
import sys
import json
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SDK discovery
# ---------------------------------------------------------------------------

def xingyao_sdk_paths() -> List[str]:
    """解析 XINGYAO_SDK_PATHS 环境变量，返回 SDK 目录列表。"""
    raw = os.environ.get("XINGYAO_SDK_PATHS", "")
    if not raw:
        # 也尝试 XINGYAO_SDK_ROOT 单目录模式
        root = os.environ.get("XINGYAO_SDK_ROOT", "")
        if root:
            raw = root
    paths = [p.strip() for p in raw.split(";") if p.strip()]
    return paths


def add_xingyao_sdk_paths() -> bool:
    """将 SDK 目录注入 sys.path。返回是否至少注入了一个路径。"""
    paths = xingyao_sdk_paths()
    added = False
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
            added = True
    return added


# ---------------------------------------------------------------------------
# 凭据解析
# ---------------------------------------------------------------------------

def _read_user_env(key: str) -> str:
    """读取 Windows 用户级环境变量（注册表 HKCU\\Environment）。

    星耀凭据常配置在用户级环境变量中，Bash/子进程的 os.environ 未必包含，
    因此需兜底读取注册表。非 Windows 或读不到时返回空字符串。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, key)
        return str(val)
    except Exception:
        return ""


def _env(key: str, fallback: str = "") -> str:
    """读取环境变量，支持 AD_* 和 XINGYAO_* 两套命名。

    读取顺序：进程 os.environ → Windows 用户级注册表（兜底）。
    """
    val = os.environ.get(key, "")
    if not val:
        val = _read_user_env(key)
    if val:
        return val
    # fallback: AD_ → XINGYAO_ 或反之
    if key.startswith("AD_"):
        alt = "XINGYAO_" + key[3:]
    else:
        alt = "AD_" + key[len("XINGYAO_"):]
    alt_val = os.environ.get(alt, "")
    if not alt_val:
        alt_val = _read_user_env(alt)
    return alt_val or fallback


def credentials() -> Dict[str, Any]:
    """返回登录所需的凭据字典。密码以掩码显示在日志中。"""
    user = _env("AD_USERNAME")
    pwd = _env("AD_PASSWORD")
    host = _env("AD_HOST", "101.230.159.234")
    port = int(_env("AD_PORT", "8600"))
    return {
        "username": user,
        "password": pwd,
        "host": host,
        "port": port,
    }


def is_configured() -> bool:
    """检查是否已配置凭据。"""
    cred = credentials()
    return bool(cred["username"] and cred["password"])


def is_enabled() -> bool:
    """检查是否启用星耀数据源。"""
    return os.environ.get("XINGYAO_ENABLED", "").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# 登录与会话
# ---------------------------------------------------------------------------

_ad_session: Any = None
_ad_login_time: Optional[datetime] = None


def login() -> Any:
    """登录 AmazingData SDK，返回 session 对象。
    
    幂等：如果已登录且距上次登录不超过 23 小时，复用会话。
    """
    global _ad_session, _ad_login_time
    now = datetime.now()

    if _ad_session is not None and _ad_login_time is not None:
        if (now - _ad_login_time).total_seconds() < 23 * 3600:
            return _ad_session

    # 注入 SDK 路径
    add_xingyao_sdk_paths()

    try:
        import AmazingData as ad
    except ImportError:
        raise RuntimeError(
            "AmazingData SDK 未安装。请将 SDK 目录加入 XINGYAO_SDK_PATHS 环境变量，"
            "或安装 tgw 和 AmazingData wheel 包。"
        )

    cred = credentials()
    if not cred["username"] or not cred["password"]:
        raise RuntimeError("星耀凭据未配置。请设置 AD_USERNAME / AD_PASSWORD 环境变量。")

    ad.login(
        username=cred["username"],
        password=cred["password"],
        host=cred["host"],
        port=cred["port"],
    )

    _ad_session = ad
    _ad_login_time = now

    logger.info(
        "星耀登录成功 host=%s port=%s user=%s***",
        cred["host"], cred["port"], cred["username"][:3] if len(cred["username"]) > 3 else cred["username"]
    )
    return ad


# ---------------------------------------------------------------------------
# 基础数据
# ---------------------------------------------------------------------------

def get_calendar() -> List[str]:
    """获取交易日历列表。"""
    ad = login()
    base = ad.BaseData()
    return base.get_calendar()


def get_stock_list() -> List[str]:
    """获取 A 股代码列表。"""
    ad = login()
    base = ad.BaseData()
    return base.get_code_list(security_type="EXTRA_STOCK_A")


def get_etf_list() -> List[str]:
    """获取 ETF 代码列表。"""
    ad = login()
    base = ad.BaseData()
    return base.get_code_list(security_type="EXTRA_ETF")


# ---------------------------------------------------------------------------
# 行情数据
# ---------------------------------------------------------------------------

def get_kline(
    codes: List[str],
    begin_date: int,
    end_date: int,
    period: str = "day",
) -> Dict[str, Any]:
    """获取历史 K 线数据。

    Args:
        codes: 股票/ETF 代码列表，如 ['510050.SH', '588000.SH']
        begin_date: 开始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD
        period: 周期 'day' | 'week' | 'month' | 'min1' | 'min5' | 'min15' | 'min30' | 'min60'

    Returns:
        {code: DataFrame} 字典
    """
    ad = login()
    calendar = get_calendar()
    market = ad.MarketData(calendar)

    period_map = {
        "day": ad.constant.Period.day.value,
        "week": ad.constant.Period.week.value,
        "month": ad.constant.Period.month.value,
        "min1": ad.constant.Period.min1.value,
        "min5": ad.constant.Period.min5.value,
        "min15": ad.constant.Period.min15.value,
        "min30": ad.constant.Period.min30.value,
        "min60": ad.constant.Period.min60.value,
    }
    p = period_map.get(period, ad.constant.Period.day.value)

    return market.query_kline(
        code_list=codes,
        begin_date=begin_date,
        end_date=end_date,
        period=p,
    )


def get_snapshot(
    codes: List[str],
    begin_date: Optional[int] = None,
    end_date: Optional[int] = None,
) -> Dict[str, Any]:
    """获取实时快照。

    Args:
        codes: 代码列表

    Returns:
        {code: dict} 字典，包含最新价、涨跌幅、成交量等
    """
    ad = login()
    calendar = get_calendar()
    market = ad.MarketData(calendar)
    trade_date = int(datetime.now().strftime("%Y%m%d"))
    return market.query_snapshot(
        code_list=codes,
        begin_date=begin_date or trade_date,
        end_date=end_date or trade_date,
    )


# ---------------------------------------------------------------------------
# 财务数据
# ---------------------------------------------------------------------------

def get_income(codes: List[str], is_local: bool = False) -> Dict[str, Any]:
    """获取利润表。

    Args:
        codes: 代码列表
        is_local: 是否使用本地缓存

    Returns:
        {code: DataFrame}
    """
    ad = login()
    info = ad.InfoData()
    return info.get_income(codes, is_local=is_local)


def get_balance(codes: List[str], is_local: bool = False) -> Dict[str, Any]:
    """获取资产负债表。"""
    ad = login()
    info = ad.InfoData()
    return info.get_balance(codes, is_local=is_local)


def get_cashflow(codes: List[str], is_local: bool = False) -> Dict[str, Any]:
    """获取现金流量表。"""
    ad = login()
    info = ad.InfoData()
    return info.get_cashflow(codes, is_local=is_local)


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------

def diagnostics() -> Dict[str, Any]:
    """运行连接诊断，返回状态摘要。"""
    result = {
        "source": "xingyao/AmazingData",
        "timestamp": datetime.now().isoformat(),
        "configured": is_configured(),
        "enabled": is_enabled(),
        "status": "unknown",
        "checks": {},
    }

    if not result["enabled"]:
        result["status"] = "disabled"
        return result

    if not result["configured"]:
        result["status"] = "unconfigured"
        return result

    try:
        ad = login()
        result["checks"]["login"] = "ok"

        calendar = get_calendar()
        result["checks"]["calendar"] = f"{len(calendar)} trading days, latest={calendar[-1]}"

        stocks = get_stock_list()
        result["checks"]["stocks"] = f"{len(stocks)} A-shares"

        etfs = get_etf_list()
        result["checks"]["etfs"] = f"{len(etfs)} ETFs"

        result["status"] = "healthy"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


# ---------------------------------------------------------------------------
# CLI 入口（独立诊断）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(diagnostics(), ensure_ascii=False, indent=2))
