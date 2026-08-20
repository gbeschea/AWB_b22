"""Trimitere email INTERN (alerte către echipă), prin SMTP.

OH avea o singură cale de email: `orderInvoiceSend` prin Shopify — care scrie CLIENTULUI, de pe
adresa magazinului, legat de o comandă. Pentru o alertă de stoc către depozit/achiziții aia nu e
folosibilă: n-are comandă, n-are client, iar mesajul ar ateriza în inboxul greșit.

Credențialele vin din env (SMTP_HOST/PORT/USER/PASS), puse din KB. Fără ele, `send` întoarce False
și nu aruncă — o alertă care nu se poate trimite nu are voie să dărâme tura care a generat-o.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"))


def _recipients(to: Sequence[str] | str | None) -> List[str]:
    if not to:
        return []
    raw = to if isinstance(to, (list, tuple)) else str(to).replace(";", ",").split(",")
    return [a.strip() for a in raw if a and a.strip() and "@" in a]


def send(subject: str, body_html: str, to: Sequence[str] | str,
         *, body_text: Optional[str] = None, sender: Optional[str] = None) -> bool:
    """True dacă a plecat. Fail-soft: orice problemă → False + log, niciodată excepție în sus."""
    rcpt = _recipients(to)
    if not rcpt:
        logger.info("mailer: fără destinatari — nu trimit '%s'", subject)
        return False
    if not configured():
        logger.info("mailer: SMTP neconfigurat — nu trimit '%s'", subject)
        return False

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT") or 587)
    user = os.environ["SMTP_USER"]
    pwd = os.environ.get("SMTP_PASS") or ""
    frm = sender or os.environ.get("SMTP_FROM") or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = ", ".join(rcpt)
    msg.set_content(body_text or "Mesajul acesta necesită un client de email cu HTML.")
    msg.add_alternative(body_html, subtype="html")
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                try:
                    s.starttls(context=ctx)
                    s.ehlo()
                except smtplib.SMTPNotSupportedError:
                    pass          # server fără TLS pe portul ăsta — mergem mai departe
                s.login(user, pwd)
                s.send_message(msg)
        logger.info("mailer: trimis '%s' către %d destinatari", subject, len(rcpt))
        return True
    except Exception as e:
        logger.info("mailer: trimiterea a eșuat pentru '%s': %s", subject, e)
        return False
