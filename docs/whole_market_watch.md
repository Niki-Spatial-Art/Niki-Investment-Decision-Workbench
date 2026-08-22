# 全市场漏斗观察与QQ日报

`watchlists/whole_market_watchlist.json` 是公开、可替换的全市场观察配置。它把研究拆成四层：

1. 市场层：沪深300、涨跌家数、成交和数据覆盖，决定是否处于“等待”或“观察”环境。
2. 板块层：全行业按平均涨跌、上涨比例和成交额做初筛，只保留少量重点研究行业。
3. 深研层：默认保留AI光互连、小金属、PCB/元件三条线，补看产业、财报、供需和美股验证；梅森规则只作为公开研究标签，用来标记“顺大势逆小势 / 等待回踩 / 不追”，不直接生成仓位或买卖金额。
4. 个股层：每条主线只保留少量代表股，记录5/10/20日相对沪深300、MA20、量能、单日过热和梅森观察标签。

从 2026-08-22 起，日报额外输出一张“每日动作卡”，表头固定为：

`日期｜板块｜股票｜产业催化｜价格/成交证据｜反证条件｜结论（观察/复核）｜梅森标签`

动作卡只输出观察/复核/等待，不直接生成自动买卖指令；可用 `tools/export_daily_action_cards.py` 把最新日报或历史日报导出成 Markdown/CSV，方便你把昨天和今天并排看。

报告的“重点研究”不是买入清单。至少需要价格趋势、产业证据、财务/供需和板块广度中的三项同步，才进入人工复核；任一关键证据缺失就显示观察或等待。覆盖不足、接口失败或A股休市时，报告必须明确写出缺口并跳过邮件。

## 手工运行

```powershell
python tools/whole_market_watch_report.py --dry-run
python tools/whole_market_watch_report.py
python tools/whole_market_watch_report.py --email
```

## GitHub Actions 与 QQ 邮箱

`.github/workflows/whole-market-watch-daily.yml` 使用北京时间工作日17:25（UTC 09:25）运行。它只读取公开行情和GitHub API，不读取 `portfolio.local.json`、账户截图、券商数据或本地密钥。

`.github/workflows/theme-watch-daily.yml` 和 `.github/workflows/email-preview.yml` 现在只保留手动触发，不再固定定时推送，避免与主日报重复。

邮件可使用专用 secret `WHOLE_MARKET_RECIPIENT_EMAIL`；未配置时兼容 `THEME_WATCH_RECIPIENT_EMAIL` 或旧的 `RECIPIENT_EMAIL`。发件邮箱、SMTP授权码和服务器配置仍沿用 `SENDER_EMAIL`、`SENDER_PASSWORD`、`SMTP_SERVER`、`SMTP_PORT`。

历史回测仅说明过去的条件事件，不代表未来收益；相关性不等于因果。本模块不连接券商、不自动下单、不提供目标价、固定仓位、买入金额或收益承诺。
