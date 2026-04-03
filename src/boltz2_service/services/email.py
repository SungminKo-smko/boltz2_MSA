import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import structlog

logger = structlog.get_logger()


class EmailService:
    def __init__(self, settings):
        self.enabled = settings.smtp_enabled
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username
        self.password = settings.smtp_password
        self.from_email = settings.smtp_from_email or settings.smtp_username

    def send(self, to: str, subject: str, body_html: str) -> None:
        if not self.enabled:
            return
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = self.from_email
            msg["To"] = to
            msg["Subject"] = subject
            msg.attach(MIMEText(body_html, "html"))
            with smtplib.SMTP(self.host, self.port) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg)
            logger.info("email_sent", to=to, subject=subject)
        except Exception:
            logger.exception("email_send_failed", to=to, subject=subject)

    def notify_job_status(
        self,
        to: str,
        job_id: str,
        status: str,
        stage: str | None = None,
        message: str | None = None,
    ) -> None:
        subject = f"[Boltz-2] Job {status}: {job_id[:8]}"
        body = f"""
        <h3>Boltz-2 Prediction Job Update</h3>
        <table style="border-collapse:collapse;">
          <tr><td><b>Job ID</b></td><td>{job_id}</td></tr>
          <tr><td><b>Status</b></td><td>{status}</td></tr>
          {"<tr><td><b>Stage</b></td><td>" + stage + "</td></tr>" if stage else ""}
          {"<tr><td><b>Message</b></td><td>" + message + "</td></tr>" if message else ""}
        </table>
        """
        self.send(to, subject, body)

    def notify_stage_change(
        self,
        to: str,
        job_id: str,
        stage: str,
        progress_percent: int | None = None,
    ) -> None:
        subject = f"[Boltz-2] Stage: {stage} ({progress_percent or 0}%) - {job_id[:8]}"
        body = f"""
        <h3>Boltz-2 Prediction Progress</h3>
        <table style="border-collapse:collapse;">
          <tr><td><b>Job ID</b></td><td>{job_id}</td></tr>
          <tr><td><b>Current Stage</b></td><td>{stage}</td></tr>
          <tr><td><b>Progress</b></td><td>{progress_percent or 0}%</td></tr>
        </table>
        """
        self.send(to, subject, body)
