#!/usr/bin/env python3
"""Post the freshly generated daily report to a Telegram chat or channel.

Designed to be the final step of the daily-report pipeline. The script is a
no-op when ``TELEGRAM_BOT_TOKEN`` or ``TELEGRAM_CHAT_ID`` is unset, so
configuring Telegram is fully optional.

What it sends
-------------
1. A short header text message (date + GitHub blob URL + first lines of the
   "今日一句话总结" section if it can be located) — small enough to fit
   inside Telegram's 4096-character message limit.
2. The full Markdown file as a Telegram document attachment, so the user can
   read or archive it offline. Telegram allows up to 50 MB for documents and
   our daily reports are ~15-30 KB, so size is never a concern.

Usage
-----
::

    python scripts/post_to_telegram.py \
        --report reports/2026-05-22.md \
        --report-date 2026-05-22

Required environment variables
------------------------------
* ``TELEGRAM_BOT_TOKEN`` — bot API token from @BotFather
* ``TELEGRAM_CHAT_ID``   — numeric chat id (positive for a user/private chat,
  negative for a group or channel; for channels you can also pass an
  ``@channelusername`` string)

Optional environment variables
------------------------------
* ``GITHUB_REPOSITORY`` — auto-populated by GitHub Actions, e.g.
  ``ProWD888/meigu``. Used to build the public blob URL.
* ``REPORT_BRANCH``     — the branch where the report was committed. Defaults
  to ``feat/daily-report-automation`` to match the current default branch.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger("meigu.telegram")

# Telegram limits.
TELEGRAM_MESSAGE_LIMIT = 4096           # characters per sendMessage
TELEGRAM_API_BASE = "https://api.telegram.org"
HEADER_MAX_CHARS = 3000                 # safety margin under 4096


# --------------------------------------------------------------------------- #
# Report parsing
# --------------------------------------------------------------------------- #


def extract_summary(report_text: str) -> Optional[str]:
    """Pull out the body of the "0. 今日一句话总结" section, if present.

    The prompt template emits this as a fixed heading. We tolerate small
    variations in whitespace / numbering and stop at the next ``## `` heading.
    Returns ``None`` if the heading is not found.
    """
    pattern = re.compile(
        r"^##\s*0\.?\s*今日一句话总结\s*\n(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(report_text)
    if not match:
        return None
    body = match.group(1).strip()
    return body or None


def build_header(
    *,
    report_date: str,
    report_text: str,
    github_url: Optional[str],
) -> str:
    """Compose the inline preview message sent before the document."""
    lines = [f"📊 美股收盘日报｜{report_date}"]

    summary = extract_summary(report_text)
    if summary:
        lines.append("")
        lines.append("【一句话总结】")
        lines.append(summary)

    if github_url:
        lines.append("")
        lines.append(f"📎 完整报告: {github_url}")

    text = "\n".join(lines).strip()

    # Hard-truncate to keep within Telegram's per-message limit. The full
    # report is always also sent as a document, so truncation here only
    # affects the preview.
    if len(text) > HEADER_MAX_CHARS:
        text = text[: HEADER_MAX_CHARS - 4].rstrip() + "\n..."
    return text


# --------------------------------------------------------------------------- #
# Telegram API
# --------------------------------------------------------------------------- #


def send_message(token: str, chat_id: str, text: str) -> dict:
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            # Plain text — no parse_mode — so Chinese punctuation, brackets,
            # underscores etc. do not need to be escaped.
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    return _check_response(response, "sendMessage")


def send_document(
    token: str,
    chat_id: str,
    file_path: Path,
    caption: Optional[str] = None,
) -> dict:
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendDocument"
    with file_path.open("rb") as fh:
        files = {
            "document": (
                file_path.name,
                fh,
                "text/markdown; charset=utf-8",
            ),
        }
        data: dict = {"chat_id": chat_id}
        if caption:
            # Telegram caption limit is 1024; keep ours much shorter.
            data["caption"] = caption[:1000]
        response = requests.post(url, data=data, files=files, timeout=60)
    return _check_response(response, "sendDocument")


def _check_response(response: requests.Response, label: str) -> dict:
    """Raise a descriptive error on non-OK Telegram responses."""
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:500]}
    if not response.ok or not payload.get("ok", False):
        raise RuntimeError(
            f"Telegram {label} failed (status={response.status_code}): {payload}"
        )
    return payload


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def resolve_github_url(
    *,
    repo: Optional[str],
    branch: Optional[str],
    report_date: str,
) -> Optional[str]:
    if not repo:
        return None
    branch = branch or "feat/daily-report-automation"
    return f"https://github.com/{repo}/blob/{branch}/reports/{report_date}.md"


def discover_credentials() -> Tuple[Optional[str], Optional[str]]:
    return (
        os.getenv("TELEGRAM_BOT_TOKEN"),
        os.getenv("TELEGRAM_CHAT_ID"),
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Post daily report to Telegram")
    parser.add_argument("--report", "-r", required=True,
                        help="Path to the generated Markdown report")
    parser.add_argument("--report-date", "-d", required=True,
                        help="ISO date for the report (e.g. 2026-05-22)")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY"),
                        help="owner/name slug, e.g. ProWD888/meigu "
                             "(default: $GITHUB_REPOSITORY)")
    parser.add_argument("--branch", default=os.getenv("REPORT_BRANCH"),
                        help="Branch where the report lives "
                             "(default: feat/daily-report-automation)")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token, chat_id = discover_credentials()
    if not token or not chat_id:
        logger.info(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set; "
            "skipping Telegram post."
        )
        return 0

    report_path = Path(args.report)
    if not report_path.exists():
        logger.error("Report file not found: %s", report_path)
        return 2

    report_text = report_path.read_text(encoding="utf-8")
    github_url = resolve_github_url(
        repo=args.repo, branch=args.branch, report_date=args.report_date,
    )
    header = build_header(
        report_date=args.report_date,
        report_text=report_text,
        github_url=github_url,
    )

    logger.info("Sending header preview (%d chars)...", len(header))
    send_message(token, chat_id, header)

    caption = f"美股收盘日报 {args.report_date}"
    logger.info("Sending document %s...", report_path.name)
    send_document(token, chat_id, report_path, caption=caption)

    logger.info("Telegram post complete.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
