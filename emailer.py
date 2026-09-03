"""邮件发送模块"""
import smtplib
import time
import re
import html as html_lib
from email.utils import formatdate, make_msgid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def html_to_plain_text(html_content: str) -> str:
    """Build a useful plain-text fallback for HTML-only mail clients."""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_content)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|tr|table|section|main|li)>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", "\t", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or "本邮件包含 HTML 格式报告；如果正文显示异常，请在邮箱中切换为 HTML 显示。"


class EmailNotifier:
    """邮件通知类"""
    
    def __init__(self, sender_email: str, sender_password: str, smtp_server: str = "smtp.qq.com", smtp_port: int = 587):
        """
        初始化邮件通知
        
        :param sender_email: 发件邮箱
        :param sender_password: 邮箱授权码（不是真实密码，是QQ邮箱生成的16位授权码）
        :param smtp_server: SMTP服务器地址
        :param smtp_port: SMTP服务器端口
        """
        self.sender_email = sender_email
        self.sender_password = sender_password
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port

    def _send_message_with_retries(self, msg: MIMEMultipart, attempts: int = 3) -> bool:
        ports = [self.smtp_port]
        fallback_port = 465 if self.smtp_port != 465 else 587
        ports.append(fallback_port)

        last_error = None
        for attempt in range(1, attempts + 1):
            for port in ports:
                try:
                    if port == 465:
                        with smtplib.SMTP_SSL(self.smtp_server, port, timeout=30) as server:
                            server.login(self.sender_email, self.sender_password)
                            server.send_message(msg)
                    else:
                        with smtplib.SMTP(self.smtp_server, port, timeout=30) as server:
                            server.ehlo()
                            server.starttls()
                            server.ehlo()
                            server.login(self.sender_email, self.sender_password)
                            server.send_message(msg)
                    logger.info("SMTP email accepted by %s:%s", self.smtp_server, port)
                    return True
                except smtplib.SMTPAuthenticationError:
                    raise
                except (smtplib.SMTPException, OSError) as exc:
                    last_error = exc
                    logger.warning("SMTP send attempt %s via port %s failed: %s", attempt, port, exc)
            time.sleep(2 * attempt)

        if last_error:
            raise last_error
        return False
    
    def send_alert(self, recipient_email: str, subject: str, message: str) -> bool:
        """
        发送邮件提醒
        
        :param recipient_email: 收件人邮箱
        :param subject: 邮件主题
        :param message: 邮件内容
        :return: 是否发送成功
        """
        try:
            # 创建邮件
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.sender_email
            msg["To"] = recipient_email
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(domain=self.sender_email.split("@")[-1])
            
            # 构建HTML内容
            html_content = f"""
            <html>
              <body style="font-family: Arial, sans-serif;">
                <h2 style="color: #2c3e50;">银行利率套利提醒</h2>
                <div style="background-color: #ecf0f1; padding: 15px; border-radius: 5px;">
                  <pre style="font-size: 14px; line-height: 1.8; color: #2c3e50;">{message}</pre>
                </div>
                <hr>
                <p style="color: #7f8c8d; font-size: 12px;">
                  此邮件由银行利率监控系统自动发送，请勿直接回复。
                </p>
              </body>
            </html>
            """
            
            # 添加纯文本和HTML版本
            text_part = MIMEText(message, "plain")
            html_part = MIMEText(html_content, "html")
            
            msg.attach(text_part)
            msg.attach(html_part)
            
            self._send_message_with_retries(msg)
            
            logger.info(f"邮件已成功发送到 {recipient_email}")
            return True
            
        except smtplib.SMTPAuthenticationError:
            logger.error("邮箱认证失败，请检查邮箱地址和授权码")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"SMTP错误: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"发送邮件失败: {str(e)}")
            return False
    
    def send_html_alert(self, recipient_email: str, subject: str, html_content: str) -> bool:
        """
        发送 HTML 格式邮件（V2.0 新增）
        
        :param recipient_email: 收件人邮箱
        :param subject: 邮件主题
        :param html_content: HTML 邮件内容
        :return: 是否发送成功
        """
        try:
            # 创建邮件
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.sender_email
            msg["To"] = recipient_email
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(domain=self.sender_email.split("@")[-1])

            text_part = MIMEText(html_to_plain_text(html_content), "plain", "utf-8")
            html_part = MIMEText(html_content, "html", "utf-8")
            msg.attach(text_part)
            msg.attach(html_part)
            
            self._send_message_with_retries(msg)
            
            logger.info(f"✅ HTML 邮件已成功发送到 {recipient_email}")
            return True
            
        except smtplib.SMTPAuthenticationError:
            logger.error("❌ 邮箱认证失败，请检查邮箱地址和授权码")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"❌ SMTP错误: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"❌ 发送邮件失败: {str(e)}")
            return False
    
    def send_status_report(self, recipient_email: str, report_data: dict) -> bool:
        """
        发送定期状态报告
        
        :param recipient_email: 收件人邮箱
        :param report_data: 报告数据字典
        :return: 是否发送成功
        """
        message = f"""招商银行 vs 微众银行 利率监控报告

【招商银行 7天大额存单转让率】
{report_data.get('cmb_rate', 'N/A'):.3%}

【微众银行 7天理财率】
{report_data.get('webank_rate', 'N/A'):.3%}

【利差差值】
{report_data.get('rate_diff', 0):.1f} 个基点

【监控说明】
- 监控周期: 每8分钟
- 触发条件: 利差 > 15个基点 且 支持实时到账
- 当前状态: {'✓ 发现机会' if report_data.get('should_alert') else '✗ 暂无机会'}

"""
        
        subject = "银行利率监控 - 定期报告"
        return self.send_alert(recipient_email, subject, message)
