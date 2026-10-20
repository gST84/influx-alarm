"""Mail over SMTP (STARTTLS, SSL or plain)."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid


class MailError(Exception):
    pass


def send(cfg, to, subject, text, html=None):
    if not cfg.get("smtp_host"):
        raise MailError("Geen SMTP-server ingesteld (Instellingen → Mail)")
    if not to:
        raise MailError("Geen mailadres")
    msg = EmailMessage()
    sender = cfg.get("smtp_from") or cfg.get("smtp_user")
    msg["From"] = formataddr(("Influx Alarm", sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    host, port, security = cfg["smtp_host"], int(cfg.get("smtp_port") or 587), cfg.get("smtp_security", "starttls")
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if cfg.get("smtp_user"):
                server.login(cfg["smtp_user"], cfg.get("smtp_password", ""))
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"Mail versturen mislukt: {e}") from None
