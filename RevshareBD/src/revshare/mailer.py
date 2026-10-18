from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from revshare.secrets import load_secrets


class MailError(RuntimeError):
    pass


def smtp_config() -> dict[str, str]:
    load_secrets()
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": os.getenv("SMTP_PORT", "587").strip(),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", "").strip(),
        "sender": os.getenv("SMTP_FROM", "").strip() or os.getenv("SMTP_USER", "").strip(),
        "sender_name": os.getenv("SMTP_FROM_NAME", "Altegio Integration Hub").strip(),
        "reply_to": os.getenv("SMTP_REPLY_TO", "").strip(),
        "mode": os.getenv("SMTP_MODE", "starttls").strip().lower(),
    }


def mail_ready() -> bool:
    config = smtp_config()
    return bool(config["host"] and config["sender"])


def send_email(
    to_address: str, subject: str, body: str, html: str | None = None
) -> None:
    config = smtp_config()
    if not config["host"] or not config["sender"]:
        raise MailError("smtp_not_configured")
    message = EmailMessage()
    message["From"] = (
        formataddr((config["sender_name"], config["sender"]))
        if config["sender_name"]
        else config["sender"]
    )
    message["To"] = to_address
    message["Subject"] = subject
    if config["reply_to"]:
        message["Reply-To"] = config["reply_to"]
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    try:
        port = int(config["port"] or "587")
    except ValueError:
        port = 587
    try:
        if config["mode"] == "ssl":
            with smtplib.SMTP_SSL(config["host"], port, timeout=30) as server:
                if config["user"]:
                    server.login(config["user"], config["password"])
                server.send_message(message)
            return
        with smtplib.SMTP(config["host"], port, timeout=30) as server:
            if config["mode"] != "plain":
                server.starttls()
            if config["user"]:
                server.login(config["user"], config["password"])
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        raise MailError(str(error)) from error
