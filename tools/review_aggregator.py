#!/usr/bin/env python3
"""
复盘聚合器：生成日/周/月/季复盘，并复用 emailer 发送邮件。

职责：
- 日复盘：已有 reviews/daily/YYYY-MM-DD_trade_review.md，本脚本负责美股隔夜数据补充 + 归档校验。
- 周复盘：聚合本周 daily 复盘 + whole_market_watch 数据，计算胜率/纪律违反/盈亏。
- 月复盘：聚合本月 weekly 复盘，对比资金计划目标，产出策略优化项。
- 季复盘：聚合本季 monthly 复盘，做胜率/执行率/回报率总账。
- 年复盘：聚合本年 quarterly 复盘，做年度策略、纪律和账户总账。

设计原则：
- 只读 reports/ 和 reviews/daily/，生成 reviews/{weekly,monthly,quarterly,yearly}/。
- 不连接券商、不自动交易、不承诺收益。
- 邮件发送复用 emailer.EmailNotifier，由 workflow 传入 secrets 环境变量。

用法：
  python tools/review_aggregator.py --period weekly   [--email]
  python tools/review_aggregator.py --period monthly  [--email]
  python tools/review_aggregator.py --period quarterly [--email]
  python tools/review_aggregator.py --period yearly   [--email]
  python tools/review_aggregator.py --period daily --us-stock   # 仅补美股隔夜数据
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python 3.9+ ships zoneinfo; keep fallback safe.
    ZoneInfo = None

# 允许直接运行和作为模块导入
ROOT = Path(__file__).resolve().parent.parent
REVIEWS = ROOT / "reviews"
REPORTS = ROOT / "reports"
DAILY = REVIEWS / "daily"
WEEKLY = REVIEWS / "weekly"
MONTHLY = REVIEWS / "monthly"
QUARTERLY = REVIEWS / "quarterly"
YEARLY = REVIEWS / "yearly"
BEIJING_TZ = ZoneInfo("Asia/Shanghai") if ZoneInfo else dt.timezone(dt.timedelta(hours=8))


def now_beijing() -> dt.datetime:
    return dt.datetime.now(BEIJING_TZ)


def today_beijing() -> dt.date:
    return now_beijing().date()


def generated_line() -> str:
    return f"> 生成时间：{now_beijing():%Y-%m-%d %H:%M:%S} 北京时间\n"


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------

def load_json(path: Path):
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_daily_reviews():
    """返回按日期排序的每日复盘文件列表 [(date, path), ...]"""
    if not DAILY.exists():
        return []
    out = []
    for p in DAILY.glob("*.md"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})_trade_review\.md", p.name)
        if m:
            out.append((dt.date.fromisoformat(m.group(1)), p))
    out.sort(key=lambda x: x[0])
    return out


def load_whole_market_latest():
    return load_json(REPORTS / "whole_market_watch_latest.json")


def load_theme_watch_latest():
    return load_json(REPORTS / "theme_watch_latest.json")


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 美股隔夜数据（免费接口，只拉指数）
# ---------------------------------------------------------------------------

def fetch_us_indices():
    """
    用腾讯 qt.gtimg.cn 拉美股三大指数隔夜收盘。
    代码：usDJI(道指) usIXIC(纳指) usINX(标普500)。
    失败时降级返回 None，不伪造数据。
    """
    import urllib.request

    codes = "usDJI,usIXIC,usINX"
    url = f"https://qt.gtimg.cn/q={codes}"
    names = {"usDJI": "道琼斯", "usIXIC": "纳斯达克", "usINX": "标普500"}
    result = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk", "ignore")
    except Exception:
        return None
    for line in raw.strip().split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        code = line.split("=")[0].replace("v_", "").strip()
        payload = line.split("=", 1)[1].strip().strip('"')
        fields = payload.split("~")
        if len(fields) < 32 or code not in names:
            continue
        try:
            name = fields[1] or names[code]
            price = float(fields[3])
            change = float(fields[31]) if fields[31] else 0.0
            pct = float(fields[32]) if fields[32] else 0.0
        except (ValueError, IndexError):
            continue
        result.append({
            "code": code,
            "name": name,
            "price": price,
            "change": change,
            "pct": pct,
        })
    return result if result else None


def us_indices_markdown():
    data = fetch_us_indices()
    if not data:
        return "_美股隔夜数据获取失败，已降级跳过（不伪造）。_\n"
    lines = ["| 指数 | 收盘 | 涨跌 | 涨跌幅 |", "|---|---:|---:|---:|"]
    for d in data:
        sign = "+" if d["change"] >= 0 else ""
        lines.append(
            f"| {d['name']} | {d['price']:.2f} | {sign}{d['change']:.2f} | {sign}{d['pct']:.2f}% |"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 提取每日复盘的量化指标（用于周/月聚合）
# ---------------------------------------------------------------------------

def extract_daily_metrics(text: str):
    """从每日复盘 md 里提取关键指标，尽力而为，失败返回 None。"""
    metrics = {}

    def find(pattern):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else None

    metrics["conclusion"] = find(r"##\s*一句话结论\s*\n+(.+?)(?:\n|$)")
    if not metrics["conclusion"]:
        metrics["conclusion"] = find(r"一句话结论\s*\n+(.+?)(?:\n|$)")

    # 当日盈亏
    pnl = find(r"当日盈亏[:：]\s*约?\s*([+-]?[\d,\.]+)")
    if pnl:
        try:
            metrics["daily_pnl"] = float(pnl.replace(",", ""))
        except ValueError:
            metrics["daily_pnl"] = None

    # 总资产
    assets = find(r"总资产[:：]\s*约?\s*([\d,\.]+)")
    if assets:
        try:
            metrics["total_assets"] = float(assets.replace(",", ""))
        except ValueError:
            metrics["total_assets"] = None

    return metrics


# ---------------------------------------------------------------------------
# 周复盘
# ---------------------------------------------------------------------------

def build_weekly(today: dt.date):
    # 本周一
    monday = today - dt.timedelta(days=today.weekday())
    sunday = monday + dt.timedelta(days=6)

    daily_list = list_daily_reviews()
    week_items = [(d, p) for d, p in daily_list if monday <= d <= sunday]

    lines = []
    lines.append(f"# 周复盘 {monday} ~ {sunday}\n")
    lines.append(generated_line())

    # 美股隔夜
    lines.append("## 一、美股周度概览（最近一次隔夜收盘）\n")
    lines.append(us_indices_markdown())

    # 本周每日复盘概览
    lines.append("## 二、本周每日复盘概览\n")
    if not week_items:
        lines.append("_本周暂无每日复盘记录。_\n")
    else:
        lines.append("| 日期 | 一句话结论 | 当日盈亏 |")
        lines.append("|---|---|---:|")
        pnl_sum = 0.0
        pnl_count = 0
        for d, p in week_items:
            text = read_text(p)
            m = extract_daily_metrics(text)
            concl = (m.get("conclusion") or "").replace("|", "/")[:60]
            pnl = m.get("daily_pnl")
            if pnl is not None:
                pnl_sum += pnl
                pnl_count += 1
                pnl_str = f"{pnl:+,.2f}"
            else:
                pnl_str = "—"
            lines.append(f"| {d} | {concl} | {pnl_str} |")
        lines.append("")
        if pnl_count:
            lines.append(f"- 本周可统计的当日盈亏合计：**{pnl_sum:+,.2f} 元**（{pnl_count} 个交易日）\n")

    # 全市场快照
    lines.append("## 三、全市场快照（最新）\n")
    wm = load_whole_market_latest()
    if wm:
        lines.append(f"- 生成时间：{wm.get('generated_at', 'N/A')}")
        gate = wm.get("market_gate") or wm.get("gate") or {}
        if isinstance(gate, dict):
            for k, v in gate.items():
                lines.append(f"- 市场闸门.{k}：{v}")
        breadth = wm.get("breadth") or {}
        if isinstance(breadth, dict):
            for k, v in breadth.items():
                lines.append(f"- 广度.{k}：{v}")
        lines.append("")
    else:
        lines.append("_无全市场快照数据。_\n")

    # 纪律与规则回顾（占位，需人工填写关键项）
    lines.append("## 四、本周纪律执行与教训\n")
    lines.append("_（待人工补充：本周规则违反项、追高/止损执行情况、去弱留强结果）_\n")

    # 下周规则
    lines.append("## 五、下周规则（禁止项与观察项）\n")
    lines.append("- 涨停/高开>5% 不追；乖离>8% 不碰；s信号止损；禁止补偿交易。")
    lines.append("- 单笔 2.5%、单票 25%、同向 50% 上限。")
    lines.append("- 盈利超 5% 且乖离超 3% 出 1/2 减半仓。")
    lines.append("- _（待人工补充：下周具体观察标的与触发价）_\n")

    return "\n".join(lines)


def build_monthly(today: dt.date):
    month_start = today.replace(day=1)
    lines = []
    lines.append(f"# 月复盘 {month_start:%Y-%m}\n")
    lines.append(generated_line())

    lines.append("## 一、本月目标 vs 实际\n")
    lines.append("| 项目 | 目标 | 实际 | 状态 |")
    lines.append("|---|---|---|---|")
    lines.append("| 月度收益 | 见资金计划 | _待统计_ | ⏳ |")
    lines.append("| 胜率 | 目标 50%+ | _待统计_ | ⏳ |")
    lines.append("| 纪律违反 | 0 | _待统计_ | ⏳ |")
    lines.append("")

    # 聚合本月每周复盘
    lines.append("## 二、本月周复盘索引\n")
    weekly_list = sorted(WEEKLY.glob("*.md"))
    month_prefix = month_start.strftime("%Y-%m")
    matched = [p for p in weekly_list if p.name.startswith(month_prefix)]
    if matched:
        for p in matched:
            lines.append(f"- [{p.name}](weekly/{p.name})")
    else:
        lines.append("_本月暂无周复盘。_\n")

    lines.append("## 三、策略优化项（持续改进）\n")
    lines.append("_（待人工补充：本月最有效的一类信号、最该砍掉的一类操作）_\n")

    lines.append("## 四、下月计划\n")
    lines.append("- 对照资金计划的分批入场节奏，确认是否进入下一档。")
    lines.append("- _（待人工补充）_\n")

    return "\n".join(lines)


def build_quarterly(today: dt.date):
    q = (today.month - 1) // 3 + 1
    q_start_month = (q - 1) * 3 + 1
    q_start = today.replace(month=q_start_month, day=1)
    lines = []
    lines.append(f"# 季复盘 {today.year} Q{q}\n")
    lines.append(generated_line())

    lines.append("## 一、季度总账\n")
    lines.append("| 指标 | 数值 | 说明 |")
    lines.append("|---|---|---|")
    lines.append("| 季度回报率 | _待统计_ | 基于交割单 FIFO 配对 |")
    lines.append("| 季度胜率 | _待统计_ | 盈利回合 / 总回合 |")
    lines.append("| 盈亏比 | _待统计_ | 平均盈利 / 平均亏损 |")
    lines.append("| 最大回撤 | _待统计_ | 需完整交割单 |")
    lines.append("| 执行率 | _待统计_ | 按计划执行 / 总交易 |")
    lines.append("")

    lines.append("## 二、季度月度复盘索引\n")
    monthly_list = sorted(MONTHLY.glob("*.md"))
    matched = [
        p for p in monthly_list
        if p.name.startswith(f"{today.year}-{q_start_month:02d}")
        or p.name.startswith(f"{today.year}-{q_start_month + 1:02d}")
        or p.name.startswith(f"{today.year}-{q_start_month + 2:02d}")
    ]
    if matched:
        for p in matched:
            lines.append(f"- [{p.name}](monthly/{p.name})")
    else:
        lines.append("_本季暂无月复盘。_\n")

    lines.append("## 三、策略迭代结论（胜率/执行率改进）\n")
    lines.append("_（待人工补充：本季相比上季，胜率与执行率的变化，及原因）_\n")

    lines.append("## 四、下季度计划\n")
    lines.append("- 对照年底收益目标与资金计划，评估是否调整仓位档位。")
    lines.append("- _（待人工补充）_\n")

    return "\n".join(lines)


def build_yearly(today: dt.date):
    lines = []
    lines.append(f"# 年复盘 {today.year}\n")
    lines.append(generated_line())

    lines.append("## 一、年度总账\n")
    lines.append("| 指标 | 数值 | 说明 |")
    lines.append("|---|---|---|")
    lines.append("| 年度回报率 | _待统计_ | 需完整交割单或账户净值序列 |")
    lines.append("| 年度胜率 | _待统计_ | 盈利回合 / 总回合 |")
    lines.append("| 盈亏比 | _待统计_ | 平均盈利 / 平均亏损 |")
    lines.append("| 最大回撤 | _待统计_ | 需完整资金曲线 |")
    lines.append("| 纪律执行率 | _待统计_ | 按计划执行 / 总交易 |")
    lines.append("")

    lines.append("## 二、年度季度复盘索引\n")
    quarterly_list = sorted(QUARTERLY.glob("*.md"))
    matched = [p for p in quarterly_list if p.name.startswith(f"{today.year}-Q")]
    if matched:
        for p in matched:
            lines.append(f"- [{p.name}](quarterly/{p.name})")
    else:
        lines.append("_本年暂无季度复盘。_\n")

    lines.append("## 三、年度策略结论\n")
    lines.append("_（待人工补充：全年最有效策略、最应删除动作、主要回撤来源、下一年资金与纪律规则）_\n")

    lines.append("## 四、下一年度计划\n")
    lines.append("- 先完成完整交割单/成交查询整理，再更新年度胜率、盈亏比、最大回撤。")
    lines.append("- 不用收益目标倒推出手次数；继续以市场闸门、主题证据、账户风险预算决定动作。")
    lines.append("- _（待人工补充）_\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 写入文件
# ---------------------------------------------------------------------------

def write_review(period: str, content: str, today: dt.date):
    if period == "weekly":
        monday = today - dt.timedelta(days=today.weekday())
        name = f"{monday:%Y-%m-%d}_weekly_review.md"
        target = WEEKLY / name
    elif period == "monthly":
        name = f"{today:%Y-%m}_monthly_review.md"
        target = MONTHLY / name
    elif period == "quarterly":
        q = (today.month - 1) // 3 + 1
        name = f"{today.year}-Q{q}_quarterly_review.md"
        target = QUARTERLY / name
    elif period == "yearly":
        name = f"{today.year}_yearly_review.md"
        target = YEARLY / name
    else:
        raise ValueError(f"unsupported period: {period}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# 邮件发送
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str):
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.utils import formatdate, make_msgid
    import smtplib

    sender = (os.environ.get("SENDER_EMAIL") or "").strip()
    password = (os.environ.get("SENDER_PASSWORD") or "").strip()
    recipient = (os.environ.get("RECIPIENT_EMAIL") or "").strip()
    smtp_server = (os.environ.get("SMTP_SERVER") or "smtp.qq.com").strip()
    smtp_port = int((os.environ.get("SMTP_PORT") or "465").strip())

    if not (sender and password and recipient):
        print("[skip-email] 缺少邮件环境变量，跳过发送")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])

    html = (
        "<html><body style=\"font-family: Arial, 'Microsoft YaHei', sans-serif;\">"
        f"<pre style=\"font-size:14px;line-height:1.7;white-space:pre-wrap;\">{body}</pre>"
        "<hr><p style=\"color:#888;font-size:12px;\">本邮件由投资复盘系统自动发送，请勿直接回复。</p>"
        "</body></html>"
    )
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    ports = [smtp_port, 465 if smtp_port != 465 else 587]
    last_err = None
    for port in ports:
        try:
            if port == 465:
                with smtplib.SMTP_SSL(smtp_server, port, timeout=30) as s:
                    s.login(sender, password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(smtp_server, port, timeout=30) as s:
                    s.ehlo(); s.starttls(); s.ehlo()
                    s.login(sender, password)
                    s.send_message(msg)
            print(f"[email-ok] 已发送到 {recipient} via {smtp_server}:{port}")
            return True
        except Exception as e:
            last_err = e
    print(f"[email-fail] {last_err}")
    return False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="复盘聚合器")
    parser.add_argument("--period", required=True,
                        choices=["daily", "weekly", "monthly", "quarterly", "yearly"])
    parser.add_argument("--email", action="store_true", help="发送邮件")
    parser.add_argument("--us-stock", action="store_true", help="(daily) 仅输出美股隔夜概览")
    args = parser.parse_args()

    today = today_beijing()

    if args.period == "daily":
        if args.us_stock:
            print(us_indices_markdown())
            return
        # 日复盘主体由人工/其他脚本生成，这里只校验目录
        daily_list = list_daily_reviews()
        print(f"[daily] 现有每日复盘 {len(daily_list)} 篇")
        for d, p in daily_list[-3:]:
            print(f"  - {d} -> {p.name}")
        return

    if args.period == "weekly":
        content = build_weekly(today)
        subject = f"投资周复盘 {today:%Y-%m-%d} 周"
    elif args.period == "monthly":
        content = build_monthly(today)
        subject = f"投资月复盘 {today:%Y-%m}"
    elif args.period == "quarterly":
        content = build_quarterly(today)
        q = (today.month - 1) // 3 + 1
        subject = f"投资季复盘 {today.year} Q{q}"
    else:
        content = build_yearly(today)
        subject = f"投资年复盘 {today.year}"

    target = write_review(args.period, content, today)
    print(f"[wrote] {target}")

    if args.email:
        send_email(subject, content)

    # 打印供 workflow 日志查看
    print("\n" + content)


if __name__ == "__main__":
    main()
