# 组合方法论与阅读清单

更新时间：2026-08-22（Asia/Shanghai）

## 先说结论

这套研究不是“找到一只必涨黑马”的方法，也不应被描述成收益承诺。它更准确地属于：

1. 主题股票池上的趋势/动量条件；
2. 深埋伏、中段波段、浅埋伏三种持有节奏；
3. 市场 MA20 风险闸门；
4. 组合层的风险预算与混合配置；
5. 成本、回撤、样本外验证和交易纪律。

只有在独立基准、交易成本、样本外和风险调整指标都稳定改善后，才有资格进一步讨论“Alpha”。目前报告更像是主题暴露 + 趋势信号 + 组合构建的研究，不足以证明纯 Alpha。

## 混合组合怎么计算

混合组合不是把四个累计收益率简单平均，而是：

```text
组合当日收益 = Σ（基础场景权重 × 基础场景当日净收益）
组合净值 = 前一日净值 ×（1 + 组合当日收益）
```

未分配的权重视为现金，现金收益按 0 计。配置写在
`watchlists/portfolio_sleeve_backtest.json` 的 `blend_scenarios` 中，脚本输出在
`reports/portfolio_sleeve_backtest_latest.json` 的 `blended_portfolios`。

## 当前公开研究结果

样本：2021-08-23 至 2026-08-20，共 1,209 个共同交易日；初始资金仅用于把百分比换算成研究假设，不是当前买入金额建议。信号在收盘计算，下一交易日开始计入，权重变动按单边 0.15% 成本估算。

| 组合 | 2026上半年 | 近三年 | 近三年最大回撤 | 近五年 | 近五年最大回撤 |
|---|---:|---:|---:|---:|---:|
| 防守 | +21.45% | +77.23% | -17.63% | +96.78% | -20.47% |
| 均衡 | +31.82% | +117.29% | -23.12% | +143.46% | -27.46% |
| 激进研究 | +39.58% | +146.71% | -26.50% | +174.03% | -32.78% |
| 风险闸门 | +10.78% | +25.49% | -16.81% | +27.81% | -22.48% |
| 防守50% + 激进50% | +30.40% | +110.29% | -22.14% | +134.27% | -26.73% |
| 防守/均衡/激进等权 | +30.87% | +112.61% | -22.47% | +137.31% | -26.97% |
| 四方案等权（含风险闸门） | +25.85% | +87.95% | -18.93% | +106.37% | -25.61% |
| 风险优先（闸门50/防守25/均衡15/激进10） | +19.47% | +60.70% | -16.33% | +72.56% | -23.49% |

2026 年初至 2026-08-20 的收益、回撤、波动率和 Sharpe 也在 JSON 报告中。结果说明“混合”能改善部分波动和回撤，但主题股票之间相关性很高，等权并不会自动满足账户 -12%、单主题 -8%、单股 -6% 的纪律。尤其是四方案等权和风险优先组合的近三年回撤仍高于账户纪律。

## 这套方法论的正确验证顺序

1. **先做基准**：510300 买入持有、等权主题篮子、现金。
2. **再做信号**：MA20/MA60、相对 510300 的 5/20/60 日强弱、成交过滤、不追高。
3. **再做组合**：深/中/浅节奏与风险闸门混合。
4. **再做风险**：组合最大回撤、滚动波动、压力期、连续亏损、换手和成本。
5. **最后做样本外**：固定规则后锁定参数，按时间滚动验证；不可在看完结果后反复挑参数。
6. **再补基本面**：订单、库存、毛利率、现金流、应收、供给政策等必须按当时可获得的披露日期进入，不能把事后信息倒灌到历史信号。

后续应加入：除权/停牌/涨跌停成交约束、真实滑点、幸存者偏差修正、产业数据 PIT（point-in-time）时间戳、走样本外的参数敏感性、PBO/Deflated Sharpe 或类似的多重测试校正。

## 公平测试的当前版本

`tools/fair_universe_backtest.py` 已增加固定审计池、控制样本、退市/失败样本、510300基准和滚动样本外窗口。账户/主题/单股回撤限制的第一版采用“硬锁”：一旦触发，当前审计周期不自动恢复风险仓位。这是为了测试纪律是否真的能限制回撤，不是推荐的实盘释放规则。

这个设计会出现一个重要现象：硬锁可能在早期触发后长期留在现金，收益显著落后基准。那不是程序错误，而是在说明“止损线 + 永不复活”过于僵硬；下一步若要接近实务，应另测带冷却期、基准趋势恢复和重新评估的软闸门，并把释放规则作为独立参数验证。

截至 2026-08-20 的固定审计池结果：

| 版本 | 累计收益 | 年化 | 最大回撤 | Sharpe | 50万元研究盈亏 |
|---|---:|---:|---:|---:|---:|
| 510300 买入持有 | +63.14% | +5.22% | -42.16% | 0.37 | +31.57万元 |
| 固定池、无回撤锁 | +57.13% | +4.81% | -44.98% | 0.31 | +28.58万元 |
| 固定池、账户/主题/单股硬锁 | -10.87% | -1.19% | -12.43% | -0.35 | -5.43万元 |

无锁策略在9个滚动样本外窗口中7个为正，4个跑赢510300，但全样本仍落后基准；硬锁策略因为早期触发后长期保持现金，9个窗口没有产生正收益。这说明“把回撤限制写进规则”与“保持收益能力”是两个需要分别优化的问题，不能把账户纪律直接当作收益增强器。

固定池中4只失败/退市样本已列入配置，但公开接口只返回它们退市前的部分历史（例如 600401、600890、600240、000585），因此这仍不是完整的点时全市场无幸存者偏差测试。要完成真正公平的全市场研究，需要带历史成员、上市/退市日期、停牌和公告时点的数据库。

## 推荐阅读：组合与信号

### 论文/研究

- Markowitz, H. (1952), [“Portfolio Selection”](https://www.jstor.org/stable/2975974). *The Journal of Finance*. 现代组合理论的起点：收益、协方差和有效前沿。
- Black, F., & Litterman, R. (1992), [“Global Portfolio Optimization”](https://www.tandfonline.com/doi/abs/10.2469/faj.v48.n5.28). *Financial Analysts Journal*, 48(5), 28–43. 将市场均衡与投资者观点结合，适合理解“基准 + 观点 + 约束”。
- DeMiguel, V., Garlappi, L., & Uppal, R. (2009), [“Optimal Versus Naive Diversification”](https://academic.oup.com/rfs/article-abstract/22/5/1915/1592901). *The Review of Financial Studies*. 适合检验复杂优化是否真的胜过简单分散。
- Moskowitz, T. J., Ooi, Y. H., & Pedersen, L. H. (2012), [“Time Series Momentum”](https://www.sciencedirect.com/science/article/pii/S0304405X11002613). *Journal of Financial Economics*, 104(2), 228–250. 解释趋势/动量在多资产上的持续性与反转风险。
- Moreira, A., & Muir, T. (2017), [“Volatility-Managed Portfolios”](https://www.nber.org/papers/w22208). *The Journal of Finance*, 72, 1611–1644. 说明在波动升高时降低暴露的风险管理思路，但不等于保证收益。
- Harvey, C. R., Liu, Y., & Zhu, H. (2016), [“... and the Cross-Section of Expected Returns”](https://academic.oup.com/rfs/article/29/1/5/1843824). *The Review of Financial Studies*, 29(1), 5–68. 讨论因子挖掘和多重检验，提醒不要把筛出来的历史规律直接当 Alpha。
- Bailey, D. H., Borwein, J. M., López de Prado, M., & Zhu, Q. J., [“The Probability of Backtest Overfitting”](https://www.risk.net/journal-of-computational-finance/2471206/the-probability-of-backtest-overfitting). *The Journal of Computational Finance*. 适合理解为什么反复试参数会制造漂亮但脆弱的回测。
- Bailey, D. H., & López de Prado, M. (2014), [“The Deflated Sharpe Ratio”](https://www.pm-research.com/content/iijpormgmt/40/5/94). *The Journal of Portfolio Management*. 用于对多次试验、非正态和选择偏差后的 Sharpe 做降温。

### 实务资料与论坛

- QuantConnect 的 [Portfolio Construction](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/portfolio-construction/key-concepts) / [Risk Management](https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/risk-management/key-concepts) 文档：把“信号、组合构建、风险管理、执行”拆成不同层，和本项目的分层结构很接近。
- [Quantitative Finance Stack Exchange 的 backtesting 标签](https://quant.stackexchange.com/questions/tagged/backtesting)：适合看数据泄漏、复权、交易成本、样本外和回撤计算的具体问题。
- [聚宽社区](https://www.joinquant.com/community)：适合看 A 股策略代码和组合回测案例，但帖子结果需要自行复核，不能把“胜率100%”标题当成证据。
- 米筐文档 / RQAlpha / RQOptimizer：适合学习 A 股回测、风险归因、组合优化和约束设置。
- AQR 的 Factor Investing 资料：适合把动量、质量、价值等因子与组合而不是单票联系起来。

论坛的用途是启发实现细节，不是提供买卖信号。任何示例都要回到公开原始数据、成本和样本外验证。

## 给本项目的工作定义

今后把“Alpha”拆成三层记录：

- **信号层**：价格、成交、相对强弱是否提供了可重复的条件概率；
- **组合层**：混合后是否在同一回撤预算下提高收益或 Sharpe；
- **归因层**：收益来自市场 Beta、行业/主题暴露、动量、选股，还是纯粹的样本选择。

在这三层没有分开之前，不使用“预测明天”“压中黑马”“保证赚到”之类表述。
