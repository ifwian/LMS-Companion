"""
e-GURO (CCC LMS) checker.

Logs into the portal, reads the dashboard summary cards (DUE TODAY, ASSIGNED,
MISSED, UNREAD), compares them against the last run, and emails you a
notification if anything changed.

Config comes from environment variables (see .github/workflows/check-lms.yml
and README.md for how these get set as GitHub Secrets):

  LMS_USERNAME        - your portal username
  LMS_PASSWORD        - your portal password
  GMAIL_ADDRESS        - gmail address to send FROM
  GMAIL_APP_PASSWORD   - gmail app password (not your normal password)
  NOTIFY_EMAIL         - where to send the notification (can be same as GMAIL_ADDRESS,
                          or a carrier email-to-SMS gateway address)
"""

import os
import sys
import json
import smtplib
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://lms.ccc.edu.ph/"
LOGIN_POST_URL = "https://lms.ccc.edu.ph/app/login.php?formSubmitted=true"
COURSE_FILTER_URL = "https://lms.ccc.edu.ph/app/course_filter.php"
MAIN_STUDENT_URL = "https://lms.ccc.edu.ph/app/main_student.php"
STATE_FILE = "state.json"

# The dashboard tabs (ASSIGNED / DUE TODAY / MISSED / UNREAD)
FILTER_TEXTS = ["ASSIGNED", "DUE_TODAY", "MISSED", "UNREAD"]

# The category icons shown on the to-do page. Some of these are guesses based
# on the legend labels (Assessment, Activity/Quiz, Lesson, Questionnaire,
# Submit Answer, File Lesson, Link) - a wrong guess just returns no data for
# that combination, it won't error out.
TYPE_TEXTS = [
    "LESSON",
    "ACTIVITY_QUIZ",
    "ASSESSMENT",
    "QUESTIONNAIRE",
    "SUBMIT_ANSWER",
    "FILE_LESSON",
    "LINK",
]

# The portal expects a JSON blob describing the browser/OS in the "agents"
# field. Kept in sync with the fake User-Agent below.
AGENTS_VALUE = json.dumps(
    {
        "device": "Chrome",
        "version": "153.0.0.0",
        "layout": "Blink",
        "os": {"architecture": 64, "family": "Windows", "version": "10"},
        "description": "Chrome 153.0.0.0 on Windows 10 64-bit",
    }
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    )
}


import time


def request_with_retry(session: requests.Session, method: str, url: str, attempts: int = 4, **kwargs):
    """Retry a request with backoff, since the portal can get slow/flaky
    when many students are logging in at once. Waits 3s, 8s, 20s between tries.
    """
    delays = [3, 8, 20]
    last_error = None
    for attempt in range(attempts):
        try:
            resp = session.request(method, url, timeout=45, **kwargs)
            # Treat 5xx server errors as retryable too, not just connection errors
            if resp.status_code >= 500:
                raise requests.exceptions.HTTPError(
                    f"Server returned {resp.status_code}"
                )
            return resp
        except (requests.exceptions.RequestException,) as exc:
            last_error = exc
            if attempt < attempts - 1:
                delay = delays[min(attempt, len(delays) - 1)]
                print(
                    f"[debug] Request to {url} failed ({exc}), "
                    f"retrying in {delay}s (attempt {attempt + 1}/{attempts})"
                )
                time.sleep(delay)
    raise RuntimeError(
        f"Gave up on {url} after {attempts} attempts. Last error: {last_error}"
    )


class LmsLoginError(Exception):
    """Base class for anything that can go wrong logging into the portal."""


class InvalidCredentialsError(LmsLoginError):
    """The portal's own Login Attempts counter went up - this really is a
    wrong username/password, not a request-shape problem. Don't retry."""


class PortalStructureError(LmsLoginError):
    """Login was rejected but Login Attempts did NOT go up - this looks like
    a request-shape problem (stale token, header mismatch), not a real
    credential rejection. Worth retrying once with a completely fresh token.
    """


class PortalUnavailableError(LmsLoginError):
    """The portal didn't respond usefully at all (timeout, 5xx, network
    error) even after retries. Different situation from a bad password -
    a caller should show 'try again later', not 'check your password'."""


import re


def _extract_login_attempts(soup: BeautifulSoup):
    """Returns the integer from 'Login Attempts: N' if present, else None."""
    text_node = soup.find(string=lambda t: t and "Login Attempts" in t)
    if not text_node:
        return None
    match = re.search(r"Login Attempts:\s*(\d+)", text_node)
    return int(match.group(1)) if match else None


def _attempt_login_once(session: requests.Session, username: str, password: str):
    """One full login attempt: fetch a fresh token, submit, inspect the
    result. Raises InvalidCredentialsError or PortalStructureError on
    failure, returns the dashboard soup on success.
    """
    try:
        resp = request_with_retry(session, "GET", BASE_URL, headers=HEADERS)
        resp.raise_for_status()
    except RuntimeError as exc:
        raise PortalUnavailableError(str(exc)) from exc

    soup = BeautifulSoup(resp.text, "html.parser")
    pre_attempts = _extract_login_attempts(soup)

    token_input = soup.find("input", {"name": "token_login_form"})
    if not token_input or not token_input.get("value"):
        raise PortalStructureError(
            "Could not find token_login_form on the homepage. "
            "The portal's login page structure may have changed."
        )
    token = token_input["value"]

    payload = {
        "username": username,
        "password": password,
        "submit": "login",
        "token_login_form": token,
        "agents": AGENTS_VALUE,
    }
    post_headers = {
        **HEADERS,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://lms.ccc.edu.ph",
        "Referer": "https://lms.ccc.edu.ph/index.php",
    }
    try:
        login_resp = request_with_retry(
            session, "POST", LOGIN_POST_URL, data=payload, headers=post_headers
        )
        login_resp.raise_for_status()
    except RuntimeError as exc:
        raise PortalUnavailableError(str(exc)) from exc

    dash_soup = BeautifulSoup(login_resp.text, "html.parser")

    print(f"[debug] POST status code: {login_resp.status_code}")
    print(f"[debug] Final URL after redirects: {login_resp.url}")

    if dash_soup.find("input", {"name": "password"}):
        post_attempts = _extract_login_attempts(dash_soup)
        print(
            f"[debug] Login Attempts before: {pre_attempts}, after: {post_attempts}"
        )
        for el in dash_soup.select(".alert, .error, .text-danger, [class*='alert']"):
            text = el.get_text(strip=True)
            if text:
                print(f"[debug] Possible error message on page: {text}")

        if (
            pre_attempts is not None
            and post_attempts is not None
            and post_attempts > pre_attempts
        ):
            raise InvalidCredentialsError(
                "The portal's Login Attempts counter increased - this looks "
                "like a genuine wrong username/password, not a request bug."
            )

        snippet = dash_soup.get_text(" ", strip=True)[:300]
        print(f"[debug] Page text snippet: {snippet}")
        raise PortalStructureError(
            "Login was rejected but Login Attempts did not increase - "
            "likely a stale token or request-shape issue, not a real "
            "credential problem."
        )

    return dash_soup


def log_in(session: requests.Session, username: str, password: str) -> BeautifulSoup:
    """Log in, with one automatic retry if the first failure looks like a
    structural/stale-token issue rather than a genuine wrong password.

    Raises InvalidCredentialsError, PortalStructureError, or
    PortalUnavailableError - callers can catch these specifically to show
    the right message (this matters a lot once this is behind a real API,
    per Section 28 of the project plan: never show a raw stack trace).
    """
    try:
        return _attempt_login_once(session, username, password)
    except PortalStructureError as first_error:
        print(
            "[debug] First login attempt looked like a structural issue "
            "(not a real credential rejection) - retrying once with a "
            "completely fresh token."
        )
        try:
            return _attempt_login_once(session, username, password)
        except PortalStructureError:
            # Two structural failures in a row is now suspicious enough to
            # surface as-is rather than silently retrying forever.
            raise first_error


def fetch_items(session: requests.Session, filter_text: str, type_text: str) -> list:
    """Hit the AJAX endpoint behind one to-do tab/category combo."""
    ajax_headers = {
        **HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": MAIN_STUDENT_URL,
    }
    resp = request_with_retry(
        session,
        "GET",
        COURSE_FILTER_URL,
        params={"filter_text": filter_text, "type_text": type_text},
        headers=ajax_headers,
    )
    resp.raise_for_status()
    try:
        payload = resp.json()
    except ValueError:
        print(
            f"[debug] Non-JSON response for {filter_text}/{type_text}: "
            f"{resp.text[:200]!r}"
        )
        return []
    return payload.get("data", []) or []


def gather_all_items(session: requests.Session) -> dict:
    """Query every filter/type combo and merge results by item id.

    Each item remembers which filter categories (ASSIGNED/DUE_TODAY/MISSED/
    UNREAD) it currently shows up under, since the same item can appear in
    more than one tab.
    """
    items: dict = {}
    for filt in FILTER_TEXTS:
        for typ in TYPE_TEXTS:
            for raw in fetch_items(session, filt, typ):
                item_id = raw.get("class_exam_id")
                if not item_id:
                    continue
                entry = items.setdefault(
                    item_id,
                    {
                        "title": raw.get("title"),
                        "mark_type": raw.get("mark_type"),
                        "from_date": raw.get("from_date"),
                        "to_date": raw.get("to_date"),
                        "filters": set(),
                    },
                )
                entry["filters"].add(filt)
    return items


def to_serializable(items: dict) -> dict:
    return {
        str(item_id): {
            "title": v["title"],
            "mark_type": v["mark_type"],
            "to_date": v["to_date"],
            "filters": sorted(v["filters"]),
        }
        for item_id, v in items.items()
    }


def load_previous_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def diff_states(previous: dict, current: dict):
    """Return (new_items, urgent_items) as lists of (id, info) tuples.

    new_items: ids that weren't seen last run at all.
    urgent_items: ids currently under DUE_TODAY or MISSED (whether new or not,
    so you keep getting reminded until it's resolved).
    """
    prev_ids = set(previous.keys())
    new_items = [
        (i, v) for i, v in current.items() if i not in prev_ids
    ]
    urgent_items = [
        (i, v)
        for i, v in current.items()
        if "DUE_TODAY" in v["filters"] or "MISSED" in v["filters"]
    ]
    return new_items, urgent_items


def format_date(raw: str) -> str:
    """Turn '2026-09-15 23:59:00' into 'Sep 15, 2026 · 11:59 PM'."""
    if not raw:
        return "No date given"
    try:
        from datetime import datetime
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%b %d, %Y \u00b7 %I:%M %p").replace(" 0", " ")
    except ValueError:
        return raw


STATUS_COLORS = {
    "MISSED": "#dc2626",
    "DUE TODAY": "#ea580c",
    "ASSIGNED": "#2563eb",
}


def item_status(info: dict) -> str:
    if "MISSED" in info["filters"]:
        return "MISSED"
    if "DUE_TODAY" in info["filters"]:
        return "DUE TODAY"
    return "ASSIGNED"


STATUS_EMOJI = {
    "MISSED": "\U0001F534",       # red circle
    "DUE TODAY": "\U0001F7E0",    # orange circle
    "ASSIGNED": "\U0001F535",     # blue circle
}


def format_item_line(item_id: str, info: dict) -> str:
    status = item_status(info)
    return f"- [{status}] {info['title']} ({info['mark_type']}) - due {format_date(info['to_date'])}"


def render_item_row_html(info: dict) -> str:
    status = item_status(info)
    color = STATUS_COLORS[status]
    return f"""
    <tr>
      <td style="padding:14px 16px;border-bottom:1px solid #eef0f2;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="vertical-align:top;">
              <span style="display:inline-block;background:{color};color:#ffffff;
                font-size:11px;font-weight:700;letter-spacing:.03em;padding:3px 8px;
                border-radius:4px;font-family:Arial,sans-serif;">{status}</span>
              <div style="font-family:Arial,sans-serif;font-size:15px;font-weight:600;
                color:#1a1a1a;margin-top:8px;">{info['title']}</div>
              <div style="font-family:Arial,sans-serif;font-size:13px;color:#6b7280;
                margin-top:2px;">{info['mark_type'].replace('_', ' ').title()} &middot; Due {format_date(info['to_date'])}</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
    """


def render_section_html(title: str, rows_html: str) -> str:
    return f"""
    <tr>
      <td style="padding:24px 24px 8px 24px;font-family:Arial,sans-serif;
        font-size:13px;font-weight:700;letter-spacing:.04em;color:#374151;
        text-transform:uppercase;">{title}</td>
    </tr>
    <tr>
      <td style="padding:0 8px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
          style="background:#ffffff;border:1px solid #eef0f2;border-radius:8px;overflow:hidden;">
          {rows_html}
        </table>
      </td>
    </tr>
    """


def build_email_html(new_items, urgent_only) -> str:
    sections = ""
    if new_items:
        rows = "".join(render_item_row_html(v) for _, v in sorted(new_items))
        sections += render_section_html("New pending items", rows)
    if urgent_only:
        rows = "".join(render_item_row_html(v) for _, v in sorted(urgent_only))
        sections += render_section_html("Still needs attention", rows)

    return f"""
    <html>
    <head><meta charset="utf-8"></head>
    <body style="margin:0;padding:0;background:#f4f5f7;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:32px 0;">
        <tr>
          <td align="center">
            <table role="presentation" width="560" cellpadding="0" cellspacing="0"
              style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
              <tr>
                <td style="background:#111827;padding:22px 24px;">
                  <span style="font-family:Arial,sans-serif;color:#ffffff;font-size:17px;font-weight:700;">
                    e-GURO Reminder
                  </span>
                </td>
              </tr>
              {sections}
              <tr>
                <td style="padding:20px 24px 28px 24px;">
                  <a href="https://lms.ccc.edu.ph/" style="display:inline-block;background:#111827;
                    color:#ffffff;text-decoration:none;font-family:Arial,sans-serif;font-size:14px;
                    font-weight:600;padding:11px 20px;border-radius:6px;">Open e-GURO &rarr;</a>
                </td>
              </tr>
              <tr>
                <td style="padding:0 24px 20px 24px;font-family:Arial,sans-serif;font-size:11px;color:#9ca3af;">
                  Sent automatically by your personal LMS checker.
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </body>
    </html>
    """


def build_telegram_message(new_items, urgent_only) -> str:
    """Telegram supports a small HTML subset: <b>, <i>, <a>, <code>, etc."""
    lines = ["<b>e-GURO Reminder</b>", ""]

    if new_items:
        lines.append("<b>New pending items</b>")
        for _, v in sorted(new_items):
            emoji = STATUS_EMOJI[item_status(v)]
            lines.append(
                f"{emoji} <b>{v['title']}</b>\n"
                f"{v['mark_type'].replace('_', ' ').title()} \u00b7 Due {format_date(v['to_date'])}"
            )
        lines.append("")

    if urgent_only:
        lines.append("<b>Still needs attention</b>")
        for _, v in sorted(urgent_only):
            emoji = STATUS_EMOJI[item_status(v)]
            lines.append(
                f"{emoji} <b>{v['title']}</b>\n"
                f"{v['mark_type'].replace('_', ' ').title()} \u00b7 Due {format_date(v['to_date'])}"
            )
        lines.append("")

    lines.append('<a href="https://lms.ccc.edu.ph/">Open e-GURO</a>')
    return "\n".join(lines)


def send_telegram(message: str) -> None:
    """Sends via Telegram if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are set.
    Silently does nothing if they're not configured yet, so this is safe to
    call even before you've set up a bot.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        print("[debug] Telegram not configured (missing secrets) - skipping.")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"[debug] Telegram send failed: {resp.status_code} {resp.text[:200]}")
    else:
        print("Sent Telegram notification.")


def send_email(subject: str, text_body: str, html_body: str) -> None:
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    notify_email = os.environ.get("NOTIFY_EMAIL", gmail_address)

    from email.mime.multipart import MIMEMultipart
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = notify_email
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.sendmail(gmail_address, [notify_email], msg.as_string())


def main():
    username = os.environ["LMS_USERNAME"]
    password = os.environ["LMS_PASSWORD"]

    session = requests.Session()
    try:
        log_in(session, username, password)
    except InvalidCredentialsError:
        print(
            "Login failed: the portal reports this as a wrong username/password "
            "(its own attempts counter went up). Check LMS_USERNAME/LMS_PASSWORD."
        )
        sys.exit(1)
    except PortalStructureError as exc:
        print(
            f"Login failed after a retry, but it doesn't look like a wrong "
            f"password - looks like something about the portal's request "
            f"format changed. Details: {exc}"
        )
        sys.exit(1)
    except PortalUnavailableError as exc:
        print(f"Portal seems to be down or unreachable right now: {exc}")
        sys.exit(1)

    items = gather_all_items(session)
    current = to_serializable(items)
    previous = load_previous_state()

    print("Current items:", json.dumps(current, indent=2))

    new_items, urgent_items = diff_states(previous, current)
    # Avoid double-listing something that's both new AND urgent
    new_ids = {i for i, _ in new_items}
    urgent_only = [(i, v) for i, v in urgent_items if i not in new_ids]

    if new_items or urgent_items:
        lines = []
        if new_items:
            lines.append("NEW pending items:")
            lines.extend(format_item_line(i, v) for i, v in sorted(new_items))
        if urgent_only:
            if lines:
                lines.append("")
            lines.append("Still needs attention (due today / missed):")
            lines.extend(format_item_line(i, v) for i, v in sorted(urgent_only))
        lines.append("\nCheck: https://lms.ccc.edu.ph/")
        text_body = "\n".join(lines)
        html_body = build_email_html(new_items, urgent_only)
        send_email("LMS: pending items", text_body, html_body)
        print("Sent notification email.")

        telegram_message = build_telegram_message(new_items, urgent_only)
        send_telegram(telegram_message)
    else:
        print("Nothing new or urgent since last run.")

    save_state(current)


if __name__ == "__main__":
    main()
