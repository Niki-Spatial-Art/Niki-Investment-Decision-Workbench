# Niki Investment Decision Workbench

本地优先的 A 股研究与投资决策纪律工作台。它把账户快照、持仓风险、市场环境和盘后研究组织成一个可复核的流程，不连接券商、不自动交易，也不承诺收益。

[![A-share Market Watch](https://github.com/Niki-Spatial-Art/Niki-Investment-Decision-Workbench/actions/workflows/monitor.yml/badge.svg)](https://github.com/Niki-Spatial-Art/Niki-Investment-Decision-Workbench/actions/workflows/monitor.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![Human Review](https://img.shields.io/badge/execution-human%20review%20required-orange)](#安全边界)

## 工作流

1. 导入最新券商账户快照，确认总资产、可卖份额和持仓口径。
2. 优先处理已有持仓的风险和利润，不为收益目标强行开仓。
3. 使用市场快照判断环境强弱；候选标的只进入观察池。
4. 收盘后再用公告、资金流、龙虎榜和新闻维护观察池与交易复盘。

旧账户截图会被明确标记为“需人工确认”，不能被当作当前可交易仓位。

## 数据路由

默认行情路径：腾讯实时行情 -> 通达信 `mootdx` 日线 -> 腾讯前复权 K 线 -> AKShare。

星耀是本地可选研究增强，iFind 默认关闭，期权只用于研究/仿真。它们都不在默认刷新链或 GitHub Actions 中，避免不稳定连接和高噪声数据干扰持仓复核。

## 本地启动

```powershell
git clone https://github.com/Niki-Spatial-Art/Niki-Investment-Decision-Workbench.git
cd Niki-Investment-Decision-Workbench
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-a-stock.txt
./run_investment_workbench.ps1
```

打开 `http://127.0.0.1:8501/`。

如果已有包含行情依赖的隔离环境，可以指定它：

```powershell
$env:A_STOCK_PYTHON = 'C:\path\to\python.exe'
./run_investment_workbench.ps1
```

## GitHub Actions

`A-share Market Watch` 只生成公共市场快照工件。它不读取券商账户、私有配置、星耀凭据或期权数据。

本地工作台可选地读取 `data/trade_journal.local.csv`。它只记录你手动确认的成交，并与最新券商快照对照剩余份额；该文件被 Git 忽略，不会发送到 GitHub 或任何云端邮件。

`Whole Market Watch Daily` 是主日报：工作日A股收盘后先扫描全市场行业广度、成交和涨跌，再把结果压缩为“重点研究 / 可继续观察 / 价格强、证据待补 / 等待”的漏斗；只有三条可替换深研模块继续核对产业、财报、供需和美股验证。它发送独立的全市场QQ邮件，公开快照不含账户或仓位信息。

`Theme Watch Daily` 现在保留为手动触发的主题研究模块，`email-preview.yml` 也只保留手动预览。这样可以避免同一套观察池一天里发出多封重复邮件，把注意力集中在收盘后的人工复核。

## 安全边界

- 不连接券商，不自动下单。
- 行情源可能延迟、限流或失效；任何来源失败都会降级，而非伪造实时数据。
- 真实交易由用户独立确认和执行。
- `portfolio.local.json`、账户快照和运行数据必须保留在本地，不应提交到 GitHub。

## 文档

- [投资决策工作台说明](docs/investment_workbench.md)
- [候选证据与成交归因闭环](docs/evidence_and_attribution_workflow.md)
- [可替换主题观察模块与QQ日报](docs/theme_watch_modules.md)
- [全市场漏斗观察与QQ日报](docs/whole_market_watch.md)
- [项目记忆与运行约定](CODEX_PROJECT_MEMORY.md)
- [A 股数据依赖](requirements-a-stock.txt)
