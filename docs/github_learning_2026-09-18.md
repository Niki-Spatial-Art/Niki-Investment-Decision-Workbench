# GitHub开源学习与取舍 · 2026-09-18

通过GitHub REST API实际读取9个上游的仓库信息、最新正式Release及README。推送时间不等于正式版本发布时间，维护者发布说明中的测试数字也不是本轮重跑结果。调研阶段未安装新框架；后续按用户授权安装项目必要依赖并更新现有数据技能，未复制第三方实现代码。

| 项目 | 查询到的最新Release | 最近推送（UTC） | 对工作台的价值与决定 |
|---|---|---|---|
| [AKShare](https://github.com/akfamily/akshare) | release-v1.18.96，9月17日 | 9月17日03:40 | 已按用户授权从固定1.18.64更新至1.18.96并安装到专用环境；导入和依赖检查通过，东财历史接口在本机遇到代理连接失败，保留多源降级 |
| [mootdx](https://github.com/mootdx/mootdx) | v0.11.7，2024年5月5日 | 2024年7月16日 | 与项目固定版本一致；上游较久未推送，保留多源降级，不作为唯一数据保障 |
| [a-stock-data](https://github.com/simonlin1212/a-stock-data) | v3.8.0，9月5日 | 9月16日23:35 | 新增官方指数成分/权重、深交所交易日历、官方两融与北交所行情备份；最值得下一步研究交易日历、分页完整性和缺失值检查 |
| [yfinance](https://github.com/ranaroussi/yfinance) | 1.7.0，8月26日 | 9月17日11:23 | 更新包含元信息惰性加载、代理及拆股价格修复；本工作台部分海外行情直接使用Yahoo chart，不应假称升级此包即可修复全部海外链路 |
| [vn.py](https://github.com/vnpy/vnpy) | 4.4.0，5月14日 | 9月13日06:36 | 统一通知、推送间隔和合并消息值得借鉴；本次采用通知事件状态思路，不引入交易网关 |
| [RQAlpha](https://github.com/ricequant/rqalpha) | release/6.4.0，9月18日 | 9月18日08:56 | 当天Release新增市场配置、不同ETF佣金设置，并修复代码转换后的资金/成本连续性；适合后续A股成本与账户仿真 |
| [Backtesting.py](https://github.com/kernc/backtesting.py) | `/releases/latest`返回404，不能断言没有版本 | 8月5日12:39 | 实际读取源码确认默认市场单按下一根开盘成交；本次借鉴成交时点，不引入其AGPL代码 |
| [QuantStats](https://github.com/ranaroussi/quantstats) | v0.0.81，1月13日 | 7月20日14:12 | 净值序列、基准日期匹配、盈亏与回撤的统一展示适合未来真实账本；缺完整日净值时不强算年化或夏普 |
| [Qlib](https://github.com/microsoft/qlib) | v0.9.7，2025年8月15日 | 9月17日16:40 | 学习时间切分、实验记录和数据健康检查。当前账本与执行验证优先，暂不引入模型训练平台 |

## 已核查的具体参考

- [a-stock-data v3.8.0发布说明](https://github.com/simonlin1212/a-stock-data/releases/tag/v3.8.0)：区分官方公布日期、缺失与零值；当前指数成分不能代替历史时点成分。本轮采用了明确区分零值和缺失的原则；交易日历适配尚未接入。
- [Backtesting.py源码](https://github.com/kernc/backtesting.py/blob/master/backtesting/backtesting.py)：读取到 `trade_on_close=False` 以及默认下一根开盘成交说明；查询内容SHA为`d356b21140e55b649104c01e0fe9afaa71dd2e1b`。
- [RQAlpha撮合配置源码](https://github.com/ricequant/rqalpha/blob/master/rqalpha/mod/rqalpha_mod_sys_simulation/mod.py)：查询内容SHA为`d6f25d1f585cf01449fd32690789b582770a0d52`。尤其注意该文件对日频`next_bar`会转为`CURRENT_BAR_CLOSE`并警告，不能仅凭参数名称就以为实现了日线次日开盘。本次没有直接套用该默认行为。
- [vn.py 4.4.0](https://github.com/vnpy/vnpy/releases/tag/4.4.0)：通知合并与间隔控制对应我们重复提醒的问题；本次自主实现公共事件指纹与状态持久化。
- [QuantStats v0.0.81](https://github.com/ranaroussi/quantstats/releases/tag/v0.0.81)：报告明确标注复利、基准日期匹配与参数；对应我们不能混用持仓参考收益与账户净值的问题。

## 取舍

优先学数据契约、成交约束、状态记录与验证方法。暂不增加强化学习、自动交易、多套重叠指标库，也不把上游更新次数当投资优势。RQAlpha API许可识别为`NOASSERTION`，正式依赖前应阅读其许可证；Backtesting.py识别为AGPL-3.0。本轮只读取与借鉴方法。

原始证据与README内容保存在本地`codex-work/exports/github_learning_latest.json`；含采集时间、HTTP状态、版本、许可证和README内容SHA。用`tools/github_research_audit.py`可重复查询，失败明确记录，404不伪装成新版本。

## 安装结果

用户明确授权后，创建被Git忽略的`.venv-a-stock`（Python 3.12），安装现有基础依赖及AKShare 1.18.96、mootdx 0.11.7、pandas 2.3.3、stockstats 0.6.8。`pip check`通过，AKShare及mootdx Quotes可正常导入。安装清单保存在本地`codex-work/exports/dependency_install_20260918.json`。

本机已有`a-stock-data`技能由3.4.0更新到上游v3.8.0，原版本在本地`codex-work/backups/`保留；上游提供的26项离线测试通过，1项可选联网测试未运行。新版技能在下一轮对话可用。

仅安装当前执行链需要的软件。RQAlpha、Qlib、vn.py、QuantStats暂未接入，安装它们不能替代账户流水和样本外验证。
