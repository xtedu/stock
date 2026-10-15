"""通知通道：飞书机器人 / Server酱 / 邮件，均为可选，配了才启用。"""
import base64
import hashlib
import hmac
import logging
import smtplib
import time
from email.mime.text import MIMEText

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


def _feishu_sign(timestamp: str, secret: str) -> str:
    """自定义机器人签名：以 timestamp\\n secret 为密钥对空串做 HMAC-SHA256，再 base64。"""
    digest = hmac.new(f"{timestamp}\n{secret}".encode(), b"",
                      hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def send_feishu(title: str, content: str) -> bool:
    s = get_settings()
    url = s.feishu_webhook
    if not url:
        return False
    payload = {"msg_type": "interactive",
               "card": {"header": {"title": {"tag": "plain_text", "content": title}},
                        "elements": [{"tag": "markdown", "content": content}]}}
    if s.feishu_secret:  # 机器人开了「签名校验」才需要
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = _feishu_sign(timestamp, s.feishu_secret)
    try:
        r = httpx.post(url, json=payload, timeout=10)
        data = r.json()
        ok = r.status_code == 200 and data.get("code") == 0
        if not ok:
            log.warning("feishu notify rejected: %s", r.text)
        return ok
    except Exception as e:
        log.warning("feishu notify failed: %s", e)
        return False


def send_serverchan(title: str, content: str) -> bool:
    key = get_settings().server_chan_sendkey
    if not key:
        return False
    try:
        r = httpx.post(f"https://sctapi.ftqq.com/{key}.send",
                       data={"title": title, "desp": content}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.warning("serverchan notify failed: %s", e)
        return False


def send_email(title: str, content: str) -> bool:
    s = get_settings()
    if not (s.smtp_host and s.smtp_user and s.smtp_password and s.smtp_to):
        return False
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = title, s.smtp_from or s.smtp_user, s.smtp_to
    try:
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=15) as server:
            server.login(s.smtp_user, s.smtp_password)
            server.sendmail(msg["From"], [s.smtp_to], msg.as_string())
        return True
    except Exception as e:
        log.warning("email notify failed: %s", e)
        return False


CHANNELS = {"feishu": send_feishu, "serverchan": send_serverchan,
            "email": send_email}


def notify(title: str, content: str, channels: list[str] | None = None) -> dict:
    """按指定通道推送；未指定则全部已配置通道。"""
    results = {}
    for name, fn in CHANNELS.items():
        if channels and name not in channels:
            continue
        results[name] = fn(title, content)
    return results
