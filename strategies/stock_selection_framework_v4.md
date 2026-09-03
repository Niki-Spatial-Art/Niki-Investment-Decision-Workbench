# A股选股框架 v4：星耀数智数据源 + 三闸门多因子

> 版本：v4（2026-09-03 升级）
> 核心变化：**数据层从腾讯/东财公开接口，切换到星耀数智 AmazingData SDK（机构级数据源）**。
> 逻辑层（三闸门 + 多因子 + 风险排除）保持不变，技术指标改用 SDK 的 TimeSeriesFunction（通达信同款函数）。

---

## 一、v3 → v4 改了什么

| 层 | v3（公开接口） | v4（星耀数智） |
|---|---|---|
| 大盘闸门 | 腾讯 fqkline 日线 | 星耀 `query_kline(period=day)` |
| 全市场列表 | 东财 clist（f12/f14/f100） | 星耀 `get_code_info`（名称/上市日/涨跌停价/昨收） |
| 日 K 线 | 腾讯 fqkline 单票串行 | 星耀 `query_kline` **批量**（100只/批） |
| 技术指标 | 手工算 MA/乖离/振幅 | SDK `TimeSeriesFunction`（MA/EMA/HHV/LLV/REF） |
| 涨跌停/次新 | 名称字符串猜 ST/次新 | `security_status` + `list_day` 字段精确判断 |

**收益：**
1. 数据更权威（银河证券星耀数智，你开户的机构级数据）。
2. 技术指标是通达信同款函数引擎，和你的通达信公式体系一致。
3. 批量拉取，全市场 5215 只日线 2分45秒跑完（v3 腾讯单票串行约 20 分钟）。

---

## 二、三闸门 + 多因子（逻辑不变）

**第一道闸门：大盘择时**（三信号全开才买）
- ① 沪深300 站上 MA20（约 4631）
- ② 上证指数 站上 4000
- ③ 中证1000 站上 MA20（约 7683）

**第二道闸门：多因子打分**（满分 100）

| 因子 | 权重 | 看什么 |
|---|---|---|
| 动量 | 30 | 20日/60日涨幅（趋势向上不追高） |
| 回踩度 | 30 | 乖离 MA20（核心：跌到均线附近） |
| 量能 | 20 | 回踩缩量=洗盘，放量=出货 |
| 趋势 | 20 | MA5>MA10>MA20 多头排列 |
| 波动 | 10 | 低振幅胜率高（附加参考） |

**第三道闸门：风险排除**
- ST / 退市 / 次新（上市 <4 个月，用 `list_day` 判断）
- 成交额 < 2 亿（低流动性）
- 腰斩反弹（60 日回撤 >30%）
- 高位放量滞涨（20日涨超20% 且当日滞涨）
- 涨停(>9.5%) 或大跌(<-5%) 极端票

---

## 三、星耀 SDK 关键接口

```python
import AmazingData as ad
ad.login(username=..., password=..., host="101.230.159.234", port=8600)

cal = ad.BaseData().get_calendar()                      # 交易日历
info = ad.BaseData().get_code_info("EXTRA_STOCK_A")      # 全市场A股：symbol/security_status/pre_close/high_limited/low_limited/list_day
md = ad.MarketData(cal)
k = md.query_kline(code_list=[...], begin_date, end_date, period=10008)  # 日线批量
snap = md.query_snapshot(code_list=[...], ...)           # 实时快照（五档盘口）

tsf = ad.TimeSeriesFunction
ma20 = tsf.MA(close_series, 20)   # 通达信同款 MA
hhv60 = tsf.HHV(high_series, 60)  # 60日最高
```

**Period 枚举**：`day=10008`、`week=10009`、`month=10010`、`min1=10000`、`min5=10002`...

**凭据**：Windows 用户级环境变量 `AD_USERNAME / AD_PASSWORD / AD_HOST / AD_PORT`（Bash 子进程读不到，需用 `winreg` 或 PowerShell 读）。

---

## 四、运行

```bash
python stock_screen_v4.py --top 20          # 全市场
python stock_screen_v4.py --quick           # 快速模式（高价活跃票 800 只）
```

输出：`screen_result_v4.json`（闸门状态 + Top N + 全部候选），每只含回踩买点价 + 止损价。

---

## 五、当日状态（2026-09-03 收盘）

- 闸门**未开**：沪深300 4552.58（MA20 4631.54）、上证 3942.09（差 58 点）、中证1000 7600.10（MA20 7683.09），三信号全灭。
- 全市场扫 5215 只，筛出 357 只候选，均为**预备池，不买**。
- v4 与 v3 交叉验证：5 只观察票（甘李药业/安琪酵母/芯朋微/明泰铝业/中国海油）买卖点完全吻合，证明数据源切换正确。

---

## 六、一句话记住整套逻辑

> 大盘闸门没开 = 只看不买；闸门开了 = 从回踩票里按综合分挑，买点缩量企稳、破止损就走、单票 ≤2.5%。
