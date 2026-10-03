"""Gmail IMAP 取验证码（stdlib only）。"""
from __future__ import annotations

import email
import imaplib
import re
import time
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Any, Optional

OTP_BODY_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


class GmailOtpError(Exception):
    pass


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
    text = _decode_mime_header(header_val).lower()
    return want_l in text


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
) -> str:
    pwd = normalize_app_password(app_password)
    if not inbox_address or not pwd:
        raise GmailOtpError("Gmail 收件箱或应用专用密码未配置")
    since = _imap_since_date(since_ts)
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        mail.login(inbox_address, pwd)
        mail.select("INBOX")
        typ, data = mail.search(None, f"(SINCE {since})")
        if typ != "OK" or not data or not data[0]:
            return ""
        ids = data[0].split()
        for num in reversed(ids[-40:]):
            typ, msg_data = mail.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data:
                continue
            raw = msg_data[0]
            if isinstance(raw, tuple):
                raw = raw[1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = email.message_from_bytes(bytes(raw))
            if since_ts is not None:
                mdt = _message_datetime(msg)
                if mdt is not None and mdt.timestamp() < float(since_ts) - 30:
                    continue
            if not _from_allowed(msg, from_allowlist):
                continue
            if not _subject_ok(msg, subject_contains):
                continue
            if not _to_recipient_ok(msg, to_address):
                continue
            code = extract_otp_from_text(_collect_text_parts(msg))
            if code:
                return code
        return ""
    finally:
        try:
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
) -> str:
    allow = [str(x).strip() for x in (from_allowlist or []) if str(x).strip()]
    interval = max(1.0, int(poll_interval_ms or 3000) / 1000.0)
    deadline = time.time() + max(5.0, int(max_wait_ms or 90_000) / 1000.0)
    if deadline_ts:
        deadline = min(deadline, float(deadline_ts))
    last_err: Optional[Exception] = None
    while time.time() < deadline:
        if should_stop and should_stop():
            raise GmailOtpError("任务已取消或已超过用例时间")
        try:
            code = _fetch_once(
                inbox_address=inbox_address,
                app_password=app_password,
                to_address=to_address,
                since_ts=since_ts,
                from_allowlist=allow,
                subject_contains=subject_contains,
            )
            if code:
                return code
        except imaplib.IMAP4.error as exc:
            last_err = exc
            raise GmailOtpError(f"Gmail IMAP 登录失败，请检查收件箱地址与应用专用密码：{exc}") from exc
        except OSError as exc:
            last_err = exc
            raise GmailOtpError(f"无法连接 Gmail IMAP：{exc}") from exc
        slept = 0.0
        while slept < interval and time.time() < deadline:
            if should_stop and should_stop():
                raise GmailOtpError("任务已取消或已超过用例时间")
            step = min(0.5, interval - slept)
            time.sleep(step)
            slept += step
    if should_stop and should_stop():
        raise GmailOtpError("任务已取消或已超过用例时间")
    if last_err:
        raise GmailOtpError("等待验证码邮件超时")
    raise GmailOtpError("等待验证码邮件超时（未在邮件正文中匹配到 4–8 位数字）")
