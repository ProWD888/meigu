#!/usr/bin/env python3
"""Generate the daily US stock market closing report by calling an LLM.

This script reads the structured market data JSON produced by
``fetch_market_data.py`` and the prompt template at
``prompts/daily_report_prompt.md``, splices them together, sends them to an LLM
provider (OpenAI or Anthropic), and writes the resulting Markdown report to
``reports/<report_date>.md``.

Provider selection
------------------

The provider is chosen via the ``LLM_PROVIDER`` environment variable
(``openai``, ``anthropic``, or ``gemini``). The corresponding credentials must
be present:

* ``LLM_PROVIDER=openai``    ⇒ ``OPENAI_API_KEY`` (and optional
  ``OPENAI_MODEL``, ``OPENAI_BASE_URL``).
* ``LLM_PROVIDER=anthropic`` ⇒ ``ANTHROPIC_API_KEY`` (and optional
  ``ANTHROPIC_MODEL``).
* ``LLM_PROVIDER=gemini``    ⇒ ``GEMINI_API_KEY`` (and optional
  ``GEMINI_MODEL``).

Usage
-----
::

    python scripts/generate_report.py \
        --data data/latest.json \
        --prompt prompts/daily_report_prompt.md \
        --output reports/2026-05-22.md

If ``--output`` is omitted, the file path is derived from the
``report_date`` field inside the data JSON.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("meigu.generate")

DEFAULT_OPENAI_MODEL = "gpt-4o"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"
PLACEHOLDER = "{{MARKET_DATA_JSON}}"


# --------------------------------------------------------------------------- #
# Prompt assembly
# --------------------------------------------------------------------------- #


def build_prompt(template_text: str, market_data: dict) -> str:
    """Inject the JSON payload into the prompt template.

    The template contains the literal placeholder ``{{MARKET_DATA_JSON}}``.
    We embed the JSON inside a fenced code block to avoid breaking Markdown
    formatting in the prompt.
    """
    if PLACEHOLDER not in template_text:
        raise ValueError(
            f"Prompt template missing placeholder {PLACEHOLDER!r}. "
            "Cannot inject market data."
        )
    payload_json = json.dumps(
        market_data, ensure_ascii=False, indent=2, default=str
    )
    fenced = f"```json\n{payload_json}\n```"
    return template_text.replace(PLACEHOLDER, fenced)


# --------------------------------------------------------------------------- #
# Provider clients
# --------------------------------------------------------------------------- #


def call_openai(prompt: str, model: str, base_url: Optional[str], api_key: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "openai package not installed. Run `pip install -r requirements.txt`."
        ) from exc

    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)

    logger.info("Calling OpenAI model=%s", model)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a senior US equity market analyst. "
                    "Always follow the user's report structure exactly, write "
                    "in 中文, and never fabricate numbers. If a data field "
                    "is null/missing, write '暂无可靠数据'."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    if not response.choices:
        raise RuntimeError("OpenAI response had no choices")
    content = response.choices[0].message.content or ""
    if not content.strip():
        raise RuntimeError("OpenAI returned empty content")
    return content


def call_anthropic(prompt: str, model: str, api_key: str) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package not installed. Run `pip install -r requirements.txt`."
        ) from exc

    client = anthropic.Anthropic(api_key=api_key)
    logger.info("Calling Anthropic model=%s", model)
    message = client.messages.create(
        model=model,
        max_tokens=8192,
        temperature=0.2,
        system=(
            "You are a senior US equity market analyst. Always follow the "
            "user's report structure exactly, write in 中文, and never "
            "fabricate numbers. If a data field is null/missing, write "
            "'暂无可靠数据'."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    parts = []
    for block in message.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    content = "".join(parts).strip()
    if not content:
        raise RuntimeError("Anthropic returned empty content")
    return content


def call_gemini(prompt: str, model: str, api_key: str) -> str:
    """Call Google Gemini using the official google-genai SDK."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError(
            "google-genai package not installed. "
            "Run `pip install -r requirements.txt`."
        ) from exc

    client = genai.Client(api_key=api_key)
    logger.info("Calling Gemini model=%s", model)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=8192,
            system_instruction=(
                "You are a senior US equity market analyst. Always follow "
                "the user's report structure exactly, write in 中文, and "
                "never fabricate numbers. If a data field is null/missing, "
                "write '暂无可靠数据'."
            ),
        ),
    )
    # `response.text` is a convenience accessor that joins all text parts.
    content = (getattr(response, "text", "") or "").strip()
    if not content:
        # Fallback: walk candidates manually if `.text` is unavailable.
        parts: list = []
        for cand in getattr(response, "candidates", []) or []:
            for part in getattr(getattr(cand, "content", None), "parts", []) or []:
                text = getattr(part, "text", None)
                if text:
                    parts.append(text)
        content = "".join(parts).strip()
    if not content:
        raise RuntimeError("Gemini returned empty content")
    return content


# --------------------------------------------------------------------------- #
# Output handling
# --------------------------------------------------------------------------- #


def resolve_output_path(
    explicit: Optional[str], market_data: dict
) -> Path:
    if explicit:
        return Path(explicit)
    report_date = market_data.get("report_date")
    if not report_date:
        # Fall back to today's UTC date.
        report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Path("reports") / f"{report_date}.md"


def prepend_metadata(content: str, market_data: dict, provider: str, model: str) -> str:
    """Add a small YAML-style header so reports are easy to audit later."""
    report_date = market_data.get("report_date") or "unknown"
    generated_utc = market_data.get("generated_at_utc") or datetime.now(timezone.utc).isoformat()
    header = (
        "<!--\n"
        f"report_date: {report_date}\n"
        f"data_generated_at_utc: {generated_utc}\n"
        f"llm_provider: {provider}\n"
        f"llm_model: {model}\n"
        f"report_generated_at_utc: {datetime.now(timezone.utc).isoformat()}\n"
        "-->\n\n"
    )
    return header + content.lstrip()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate daily report via LLM")
    parser.add_argument("--data", "-d", default="data/latest.json",
                        help="Path to the market data JSON")
    parser.add_argument("--prompt", "-p", default="prompts/daily_report_prompt.md",
                        help="Path to the prompt template")
    parser.add_argument("--output", "-o", default=None,
                        help="Output Markdown path (default: reports/<report_date>.md)")
    parser.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "openai"),
                        choices=["openai", "anthropic", "gemini"],
                        help="LLM provider to use")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    data_path = Path(args.data)
    prompt_path = Path(args.prompt)
    if not data_path.exists():
        logger.error("Market data file not found: %s", data_path)
        return 2
    if not prompt_path.exists():
        logger.error("Prompt template not found: %s", prompt_path)
        return 2

    with data_path.open("r", encoding="utf-8") as fh:
        market_data = json.load(fh)
    template_text = prompt_path.read_text(encoding="utf-8")

    full_prompt = build_prompt(template_text, market_data)

    # NOTE: we deliberately use ``os.getenv("FOO") or DEFAULT`` rather than
    # ``os.getenv("FOO", DEFAULT)``. GitHub Actions renders an unset secret as
    # the empty string instead of leaving the env var unset, which would make
    # the second form return ``""`` and silently bypass the default.
    if args.provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            logger.error("OPENAI_API_KEY is not set")
            return 3
        model = os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        base_url = os.getenv("OPENAI_BASE_URL") or None
        try:
            content = call_openai(full_prompt, model, base_url, api_key)
        except Exception:
            logger.exception("OpenAI call failed")
            return 4
    elif args.provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            logger.error("ANTHROPIC_API_KEY is not set")
            return 3
        model = os.getenv("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL
        try:
            content = call_anthropic(full_prompt, model, api_key)
        except Exception:
            logger.exception("Anthropic call failed")
            return 4
    else:  # gemini
        # Accept GEMINI_API_KEY (preferred) or the older GOOGLE_API_KEY name.
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            logger.error("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
            return 3
        model = os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        try:
            content = call_gemini(full_prompt, model, api_key)
        except Exception:
            logger.exception("Gemini call failed")
            return 4

    final_text = prepend_metadata(content, market_data, args.provider, model)

    out_path = resolve_output_path(args.output, market_data)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(final_text, encoding="utf-8")
    logger.info("Wrote report to %s (%d chars)", out_path, len(final_text))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
