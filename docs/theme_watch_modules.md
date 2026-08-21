# 可替换主题观察模块

`watchlists/theme_watchlist.json` 是公开、可替换的研究配置。它不含账户、收件人、仓位、目标价或交易指令；fork 本仓库后可自行替换模块、A股代码、美股验证标的和产业核验项。

## 默认模块

- `ai_optical_interconnect`：中际旭创、新易盛、澜起科技，以及海外AI资本开支验证。
- `minor_metals`：厦门钨业为核心，钨、锡、锑、稀土用代表股交叉验证。
- `ai_pcb_components`：PCB、覆铜板和高速材料，用于确认AI硬件是否扩散。

## 每日输出

主题日报只生成研究状态：

- 单股：相对沪深300的5/10/20日强弱、20日趋势、近5日相对成交、单日过热标识。
- 模块：趋势确认家数、相对强势家数、是否出现板块扩散，以及需补充的产业证据。
- 美股：最近一个美股交易日的趋势快照，仅作产业验证，不能等同于A股买卖信号。
- GitHub组件：跟踪配置中的开源项目最近版本/推送时间，作为数据工具维护提醒。

日报中的“可人工复核”只表示价格趋势与模块扩散达到预设观察阈值，下一步必须补核配置列出的产业/财务证据；它不是买入指令。单日急涨、数据缺失、市场基准转弱或反证出现时，一律显示“等待”。

## GitHub Actions 与 QQ 邮箱

工作流 `.github/workflows/theme-watch-daily.yml` 现在只保留手动触发。它仍会跳过A股休市日，发送独立主题邮件，并更新仅含公开行情的 `reports/theme_watch_latest.json` 与 `reports/theme_watch_history.jsonl`。

`Theme Price Study Weekly` 在每周五北京时间17:35发送单独的价格条件研究邮件。它用前复权日线测试“MA20趋势 + 20日相对沪深300 + 量能确认 + 非单日过热”条件，并以次日开盘模拟进入、持有20个交易日、扣除0.3%完整进出成本。该研究只覆盖价格和成交，不能把产业证据、财报披露时点或真实流动性回测出来。

在仓库 `Settings -> Secrets and variables -> Actions` 配置：

| Secret | 用途 |
| --- | --- |
| `SENDER_EMAIL` | QQ发件邮箱 |
| `SENDER_PASSWORD` | QQ邮箱SMTP授权码，不是登录密码 |
| `SMTP_SERVER` | 一般为 `smtp.qq.com` |
| `SMTP_PORT` | 建议 `465` |
| `THEME_WATCH_RECIPIENT_EMAIL` | 主题日报专用收件人；不填写时兼容旧的 `RECIPIENT_EMAIL` |

日报不读取券商、个人持仓或本地 `.local.json` 文件。公开历史只保存模块信号与公共行情摘要。

## 手工运行

```powershell
python tools/theme_watch_report.py --dry-run
python tools/theme_watch_report.py --output reports/theme_watch_latest.json --history reports/theme_watch_history.jsonl
python tools/theme_watch_report.py --email
python tools/theme_signal_backtest.py --dry-run
python tools/theme_signal_backtest.py --email
```

在修改规则或执行任何人工交易前，应先重新回测并做样本外验证。历史规律不能保证未来，相关性不等于因果。
