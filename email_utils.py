"""
Mejlutskick via SMTP (inbyggt i Python, inga externa paket).
Konfigureras via miljövariabler (se .env.example). Om SMTP inte är
konfigurerat loggas mejlet bara till konsolen istället för att skickas,
så resten av systemet fungerar även innan mejlkontot är på plats.
"""
import os
import smtplib
import ssl
from email.mime.text import MIMEText

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)
ADMIN_NOTIFY_EMAIL = os.environ.get("ADMIN_NOTIFY_EMAIL", "")


def _is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and FROM_EMAIL)


def send_email(to_email: str, subject: str, body: str) -> bool:
    """Skickar ett textmejl. Returnerar True om det skickades (eller
    loggades, i icke-konfigurerat läge), False om ett fel uppstod."""
    if not to_email:
        return False

    if not _is_configured():
        print("=" * 60)
        print("[E-POST EJ KONFIGURERAD - visar bara innehållet]")
        print(f"Till: {to_email}")
        print(f"Ämne: {subject}")
        print(body)
        print("=" * 60)
        return True

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(FROM_EMAIL, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[E-POST-FEL] Kunde inte skicka till {to_email}: {e}")
        return False


def notify_admin_ny_bokning(objekt_namn, forening_namn, start_datum, slut_datum, booking_id):
    if not ADMIN_NOTIFY_EMAIL:
        print("[VARNING] ADMIN_NOTIFY_EMAIL är inte satt - ingen adminnotis skickad.")
        return
    subject = f"Ny bokning väntar på godkännande: {objekt_namn}"
    body = (
        f"En ny bokning har kommit in och väntar på ditt godkännande.\n\n"
        f"Förening: {forening_namn}\n"
        f"Objekt: {objekt_namn}\n"
        f"Datum: {start_datum} till {slut_datum}\n"
        f"Bokningsnummer: {booking_id}\n\n"
        f"Logga in i adminpanelen för att godkänna eller neka bokningen."
    )
    send_email(ADMIN_NOTIFY_EMAIL, subject, body)


def notify_forening_bekraftad(forening_email, objekt_namn, start_datum, slut_datum):
    subject = f"Din bokning är bekräftad: {objekt_namn}"
    body = (
        f"Din bokning har godkänts och är nu bekräftad.\n\n"
        f"Objekt: {objekt_namn}\n"
        f"Datum: {start_datum} till {slut_datum}\n\n"
        f"Vid frågor, kontakta oss."
    )
    send_email(forening_email, subject, body)


def notify_forening_nekad(forening_email, objekt_namn, start_datum, slut_datum, anledning=""):
    subject = f"Din bokning kunde inte godkännas: {objekt_namn}"
    body = (
        f"Tyvärr kunde följande bokning inte godkännas:\n\n"
        f"Objekt: {objekt_namn}\n"
        f"Datum: {start_datum} till {slut_datum}\n"
    )
    if anledning:
        body += f"\nAnledning: {anledning}\n"
    body += "\nKontakta oss om ni har frågor eller vill boka andra datum."
    send_email(forening_email, subject, body)
