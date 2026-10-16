"""Gmail IMAP 取验证码（stdlib only）。"""
from __future__ import annotations

import email
import imaplib
import errno
import socket
import ssl
import re
import time
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime, getaddresses
from typing import Any, Optional

OTP_BODY_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


class GmailOtpError(Exception):
    def __init__(self, code, *, diagnostics=None):
        super().__init__(code)
        self.diagnostics = dict(diagnostics or {})


def _transport_error(exc, phase):
    if isinstance(exc, ssl.SSLCertVerificationError):
        return 'otp_imap_tls_certificate_failed', False
    if isinstance(exc, ssl.SSLError):
        return 'otp_imap_tls_failed', False
    if isinstance(exc, socket.gaierror):
        return 'otp_imap_dns_failed', exc.errno == socket.EAI_AGAIN
    if isinstance(exc, TimeoutError):
        return ('otp_imap_connect_timeout' if phase == 'connect' else 'otp_imap_read_timeout'), True
    if isinstance(exc, imaplib.IMAP4.abort):
        return 'otp_imap_connection_lost', True
    if isinstance(exc, imaplib.IMAP4.error):
        return ('otp_imap_auth_failed' if phase == 'login' else 'otp_imap_protocol_failed'), False
    if isinstance(exc, ConnectionRefusedError):
        return 'otp_imap_connection_refused', True
    if isinstance(exc, ConnectionError):
        return 'otp_imap_connection_lost', True
    if isinstance(exc, OSError) and exc.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
        return 'otp_imap_network_unreachable', True
    return 'otp_imap_connection_failed', False


def normalize_app_password(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "").strip())


def extract_otp_from_text(text: str) -> str:
    body = str(text or "")
    if not body.strip():
        return ""
    for match in OTP_BODY_RE.finditer(body):
        code = match.group(1)
        if code:
            return code
    return ""


def _decode_mime_header(value: str) -> str:
    parts: list[str] = []
    for frag, enc in decode_header(value or ""):
        if isinstance(frag, bytes):
            parts.append(frag.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(str(frag))
    return "".join(parts).strip()


def _message_datetime(msg: email.message.Message) -> Optional[datetime]:
    raw = msg.get("Date") or ""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _addresses_match_header(header_val: str, want: str) -> bool:
    want_l = str(want or "").strip().lower()
    if not want_l:
        return True
    return want_l in {address.strip().lower() for _, address in getaddresses([_decode_mime_header(header_val)])}


def _from_allowed(msg: email.message.Message, allowlist: list[str]) -> bool:
    if not allowlist:
        return True
    from_h = _decode_mime_header(msg.get("From") or "").lower()
    return any(str(a).strip().lower() in from_h for a in allowlist if str(a).strip())


def _subject_ok(msg: email.message.Message, needle: str) -> bool:
    sub = _decode_mime_header(msg.get("Subject") or "")
    n = str(needle or "").strip()
    if not n:
        return True
    return n in sub


def _to_recipient_ok(msg: email.message.Message, to_address: str) -> bool:
    want = str(to_address or "").strip()
    if not want:
        return True
    for key in ("To", "Delivered-To", "X-Original-To", "Envelope-To"):
        if _addresses_match_header(msg.get(key) or "", want):
            return True
    return False


def _collect_text_parts(msg: email.message.Message) -> str:
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = str(part.get_content_type() or "").lower()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                chunks.append(payload.decode(charset, errors="replace"))
            except LookupError:
                chunks.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                chunks.append(payload.decode(charset, errors="replace"))
            except LookupError:
                chunks.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _imap_since_date(since_ts: Optional[float]) -> str:
    if since_ts is None:
        since_ts = time.time() - 3600
    dt = datetime.fromtimestamp(float(since_ts) - 30, tz=timezone.utc)
    return dt.strftime("%d-%b-%Y")


def _fetch_once(
    *,
    inbox_address: str,
    app_password: str,
    to_address: str,
    since_ts: Optional[float],
    from_allowlist: list[str],
    subject_contains: str,
    check=None, remaining=None, stats=None,
) -> str:
    pwd = normalize_app_password(app_password)
    if not inbox_address or not pwd:
        raise GmailOtpError("Gmail 收件箱或应用专用密码未配置")
    since = _imap_since_date(since_ts)
    stats = stats if stats is not None else {}
    def guard():
        if check:
            check()
        timeout = max(0.001, min(5.0, remaining() if remaining else 5.0))
        if mail.sock is not None:
            mail.sock.settimeout(timeout)
    if check:
        check()
    stats['phase'] = 'connect'
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993,
                            timeout=max(0.001, min(5.0, remaining() if remaining else 5.0)))
    try:
        guard()
        stats['phase'] = 'login'
        mail.login(inbox_address, pwd)
        guard()
        stats['phase'] = 'select'
        mail.select("INBOX", readonly=True)
        guard()
        stats['phase'] = 'search'
        typ, data = mail.search(None, f"(SINCE {since})")
        if typ != "OK" or not data or not data[0]:
            return ""
        ids = data[0].split()
        for num in reversed(ids[-40:]):
            guard()
            stats['phase'] = 'fetch'
            typ, msg_data = mail.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data:
                continue
            raw = msg_data[0]
            if isinstance(raw, tuple):
                raw = raw[1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            guard()
            stats["messages"] = stats.get("messages", 0) + 1
            msg = email.message_from_bytes(bytes(raw))
            if since_ts is not None:
                mdt = _message_datetime(msg)
                if mdt is None or mdt.timestamp() < float(since_ts) - 30 or mdt.timestamp() > time.time() + 30:
                    continue
            if not _from_allowed(msg, from_allowlist):
                continue
            if not _subject_ok(msg, subject_contains):
                continue
            if not _to_recipient_ok(msg, to_address):
                continue
            stats["matched"] = stats.get("matched", 0) + 1
            code = extract_otp_from_text(_collect_text_parts(msg))
            if code:
                return code
        return ""
    finally:
        try:
            if mail.sock is not None:
                mail.sock.settimeout(0.1)
            mail.logout()
        except Exception:
            pass


def fetch_otp_via_imap(
    *,
    inbox_address: str,
    app_password: str,
    to_address: str = "",
    since_ts: Optional[float] = None,
    from_allowlist: Optional[list[str]] = None,
    subject_contains: str = "",
    poll_interval_ms: int = 3000,
    max_wait_ms: int = 90_000,
    should_stop=None,
    deadline_ts: Optional[float] = None,
    diagnostics: Optional[dict] = None,
) -> str:
    allow = [str(x).strip() for x in (from_allowlist or []) if str(x).strip()]
    interval = max(1.0, int(poll_interval_ms or 3000) / 1000.0)
    deadline = time.time() + max(0.0, int(max_wait_ms) / 1000.0)
    if deadline_ts:
        deadline = min(deadline, float(deadline_ts))
    monotonic_end = time.monotonic() + max(0.0, deadline - time.time())
    def remaining():
        return min(deadline - time.time(), monotonic_end - time.monotonic())
    def check():
        if (should_stop and should_stop()) or remaining() <= 0:
            raise GmailOtpError("otp_wait_stopped")
    stats = {}
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.update(connection_attempts=0, reconnects=0)
    transport_failures = 0
    last_error_code = ""
    while remaining() > 0:
        if should_stop and should_stop():
            raise GmailOtpError("任务已取消或已超过用例时间")
        delay = interval
        try:
            diagnostics['connection_attempts'] += 1
            code = _fetch_once(
                inbox_address=inbox_address,
                app_password=app_password,
                to_address=to_address,
                since_ts=since_ts,
                from_allowlist=allow,
                subject_contains=subject_contains, check=check, remaining=remaining, stats=stats,
            )
            check()
            if code:
                return code
            last_error_code = ""
        except (imaplib.IMAP4.error, OSError) as exc:
            error_code, retryable = _transport_error(exc, stats.get('phase', 'connect'))
            diagnostics.update(error_code=error_code, phase=stats.get('phase', 'connect'))
            if should_stop and should_stop():
                raise GmailOtpError('otp_wait_stopped', diagnostics=diagnostics) from None
            transport_failures += 1
            last_error_code = error_code
            # At most two reconnections over the entire request; success does not
            # reset this budget. No email sends and no parent-operation replay.
            if not retryable or transport_failures > 2 or remaining() <= 0:
                raise GmailOtpError(error_code, diagnostics=diagnostics) from None
            diagnostics['reconnects'] += 1
            delay = min(2.0 ** (transport_failures - 1), max(0.0, remaining()))
        slept = 0.0
        while slept < delay and remaining() > 0:
            if should_stop and should_stop():
                raise GmailOtpError("任务已取消或已超过用例时间")
            step = max(0.0, min(0.5, delay - slept, remaining()))
            time.sleep(step)
            slept += step
    if should_stop and should_stop():
        raise GmailOtpError("任务已取消或已超过用例时间")
    if last_error_code:
        raise GmailOtpError(last_error_code, diagnostics=diagnostics)
    if stats.get('matched'):
        raise GmailOtpError('otp_mail_code_not_found')
    if stats.get('messages'):
        raise GmailOtpError('otp_mail_filter_no_match')
    raise GmailOtpError('otp_mail_not_received')
