# Backtests

Backtesting is intentionally small at this stage. The current code records paper-trade plans and outcomes; the next step is to turn that journal into repeatable backtest/evaluation reports.

## Current State

Available now:

- export action cards from `reports/latest.json`
- append non-duplicate rows to `data/paper_trade_journal.csv`
- summarize manually closed trades
- evaluate current positions with iFinD history through `tools/ifind_position_backtest.py`

Research roadmap:

- [lean_research_layer.md](lean_research_layer.md)

Commands:

```powershell
python tools/action_audit.py export-plan --report reports/latest.json --journal data/paper_trade_journal.csv
python tools/action_audit.py summarize --journal data/paper_trade_journal.csv
python tools/ifind_position_backtest.py --days 120
python tools/portfolio_sleeve_backtest.py --dry-run
python tools/portfolio_sleeve_backtest.py
```

## Planned Backtest Metrics

- next-day entry hit rate
- T+1 exit availability
- first take-profit hit rate
- second take-profit hit rate
- stop-loss hit rate
- average win/loss
- max paper drawdown
- behavior error tags such as chase, revenge trade, oversized position

## 分层埋伏组合回测

`watchlists/portfolio_sleeve_backtest.json` 将用户关注的 AI 光互连、服务器互连、小金属和 PCB/元件分为三种节奏：

- `deep`：约60个交易日重评，模拟较长期埋伏；
- `swing`：约20个交易日重评，模拟一季以内的中段波段；
- `shallow`：约5个交易日重评，模拟较浅的观察/波段，不把A股个股当成T+0。

每个信号都在收盘计算，下一交易日才开始计入收益，避免未来函数。报告同时输出2026年上半年、2026年至今、最近约三年和全部可获得约五年样本的累计收益、年化收益、最大回撤、波动率、Sharpe、换手及进入/退出节点。

`risk_capped` 场景会在沪深300收盘跌破MA20时把主题暴露降为现金，用来检验“先保回撤、再争取收益”的闸门效果。它不是当前账户配置，也不生成买入金额或订单。

如果某个历史方案达到10万元收益，但最大回撤明显超过账户纪律，就不能直接复制；历史收益也不能预测下一个交易日。

`.github/workflows/portfolio-sleeve-backtest-weekly.yml` 每周五北京时间17:45自动更新公开回测快照，不发送交易指令、不连接券商。

## Why This Comes Before Automation

The project should prove that action cards improve review quality and risk discipline before adding stronger automation. A strategy that cannot survive paper-trade audit should not be connected to execution.
