---
name: market-decision-workbench
description: Turn user-provided A-share, Hong Kong, ETF, macro, earnings, and portfolio inputs into disciplined market-state assessments, watchlists, rebalance plans, and intraday action cards. Use when the user asks about 大盘, 调仓, 仓位, 买什么, 盘中动作卡, 市场风格, 观察池, or whether to buy, hold, reduce, wait, or use cash management.
---

# Market Decision Workbench

Use this as a research and discipline layer, not as investment advice or automated trading. Treat screenshots, group-chat claims, and copied market commentary as unverified inputs until corroborated.

## Required Workflow

1. Separate the request into market state, current holdings, candidate assets, cash, and time horizon. Ask only for information that materially changes the result.
2. Assess the market before discussing a new risk position. Use at least: index trend, breadth, sector dispersion, liquidity/turnover, and event risk.
3. Decide the regime: `risk-on`, `transition`, `bottoming/uncertain`, or `risk-off`. Do not infer a regime from one strong sector or one intraday rebound.
4. Map candidates to one risk bucket. Treat correlated holdings as one exposure rather than independent diversification.
5. Test a candidate with the evidence ladder: business/industry evidence, valuation or expected payoff, price-volume confirmation, and portfolio risk capacity.
6. Output a decision card with a default of `wait/no add` when evidence is incomplete, the market is unconfirmed, or the account is already exposed.
7. Record what would invalidate the thesis and the next review trigger. Never present a target price or a chart pattern as certainty.

## Decision Rules

- Use market environment as a gate. Breadth, an index close above its relevant trend reference, and stable leadership are stronger together than separately.
- Distinguish a tactical rebound from a confirmed trend. Require persistence across sessions; a single late-session rally is insufficient.
- Prefer relative strength only after a pullback or orderly consolidation. Do not convert a strong intraday move into an automatic chase signal.
- Separate long-horizon research from tactical execution. A good business can still be a poor trade at an expensive valuation or in a weak market.
- Use expected value, not only win rate: identify upside case, downside case, probability uncertainty, and sizing. Large-upside ideas belong in small exploratory allocations until evidence improves.
- Protect cash when the regime is unclear. Cash management products may be appropriate for unused funds but are not a substitute for an investment thesis.
- Do not convert bank cash into brokerage cash solely to chase a moving market.

## Evidence Standards

- Prefer official filings, earnings releases, company disclosures, exchange data, and reproducible price/volume data.
- Label information from screenshots, chat groups, blogs, or influencer posts as `unverified commentary`.
- Treat pattern labels such as "washing", "main-force intent", and "high win rate" as hypotheses. Do not use them as stand-alone triggers.
- Account for T+1, ETF tracking differences, fees, liquidity, gaps, and the inability to sell newly bought A-share positions the same day.

## Output Modes

For a market or rebalance request, use:

```text
市场状态：...
结论：加风险 / 维持 / 降风险 / 等待
依据：最多 3 条可验证证据
账户影响：风险桶、已有暴露、现金用途
动作：...
失效与复核：...
```

For an intraday question, use:

```text
动作：买 / 不买 / 持有 / 减 / 清 / 观察
置信度：低 / 中 / 高
触发：须满足的盘面或数据条件
失效：趋势、量价、主题或账户风险条件
仓位：只给上限与分批原则，不鼓励追涨
下次检查：时间、收盘或下一交易日
```

Read `references/framework.md` for the distilled research models and anti-patterns.

