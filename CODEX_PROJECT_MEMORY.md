# Project Memory

## Purpose

Niki Smart Tools is a local-first A-share/ETF research and decision-discipline workspace. It must never connect to a broker or place orders.

## Important Paths

- Main radar: `monitor.py`
- Dashboard generator: `tools/local_dashboard.py`
- Local market route: `tools/a_stock_market_data.py` and `tools/a_stock_radar_snapshot.py`
- Private runtime data: `data/` and `portfolio.local.json` (ignored by Git)

## Current Safety Rules

- New entries require: complete broad-market scan, fresh valid local broker snapshot, and fresh local A-share route snapshot.
- Existing holdings can still be reviewed while the new-entry gate is blocked.
- The market route is Tencent quote -> TDX/mootdx daily bars -> Tencent qfq daily bars -> AKShare fallback.
- `requirements-a-stock.txt` pins the optional full route (`mootdx`, `akshare`, `pandas`, `stockstats`). The local `.venv-a-stock` has `mootdx==0.11.7` installed; radar and workbench launchers prefer it unless `A_STOCK_PYTHON` explicitly overrides it.
- The dashboard refresh action independently selects the same `A_STOCK_PYTHON` / `.venv-a-stock` route, so an older dashboard process cannot silently fall back to a Python environment without `mootdx`.
- `data/broker_account_snapshots.json` is historical and may be malformed; use `data/broker_account_snapshots.local.json` for current manual snapshots.
- `data/trade_journal.local.csv` is an optional ignored local ledger of user-confirmed fills. The dashboard reconciles its latest entry against the latest broker snapshot; it is never sent to GitHub or cloud email.
- The local dashboard is named "Niki 投资决策工作台". Its default order is account snapshot -> holding risk -> market observation -> post-close research.
- The dashboard must visibly downgrade stale broker snapshots; never treat an old screenshot as a current executable position.
- The local-only `risk_budget` policy is rendered before holdings and candidates. It calculates a single-trial loss budget, trial-capital limit, cumulative trial-capital limit, and daily/monthly stop lines. Profit targets never open a trade; a stale broker snapshot or closed market gate sets the available trial amount to zero.
- Candidate research now passes through `data/research_evidence.local.json`: original sources/time, supply-demand thesis, counter-evidence, trigger/invalidation, and separate data/logic checks are required before a card can be submitted for human review. `data/trade_attributions.local.csv` records market, selection, entry, sizing, exit, or discipline attribution for every locally confirmed fill.
- Options are research/simulation only and do not appear in the daily dashboard flow. Xingyao is local optional research; iFind is off by default. Neither belongs in the default refresh path or GitHub Actions.
- GitHub Actions only creates a public A-share market-snapshot artifact. It must not receive broker snapshots, Xingyao credentials, or private account data.
- `watchlists/theme_watchlist.json` is a public, replaceable module configuration. `Theme Watch Daily` sends a separate QQ theme-research email after A-share close, using only public data and GitHub Secrets. Its public `reports/theme_watch_*.json*` snapshots contain no account or recipient data.
- `.github/workflows/email-preview.yml` sends the public radar email on weekdays at 10:45 Beijing (`--intraday`) and 15:25 Beijing (post-close); manual dispatch retains an `intraday` / `postclose` mode selector. It receives only SMTP secrets and never broker/account data.
- `watchlists/whole_market_watchlist.json` and `tools/whole_market_watch_report.py` implement the four-layer whole-market funnel: market gate -> sector breadth -> 3 deep research modules -> small stock set. `.github/workflows/whole-market-watch-daily.yml` sends a separate public QQ email after the A-share close and persists only public snapshots. It never emits buy amounts, target prices, fixed positions, or orders.

## Local Commands

- `./run_a_stock_radar.ps1`
- `./run_investment_workbench.ps1`
- `./run_monitor_local.ps1`
- `python tools/local_dashboard.py`

## Validation

- Use `python -m py_compile monitor.py tools/a_stock_market_data.py tools/a_stock_radar_snapshot.py tools/local_dashboard.py`.
- Run `python tools/pre_publish_check.py --include-untracked` before committing.
