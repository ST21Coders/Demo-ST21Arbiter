"""Render the Adaptive Card payload as static HTML for visual preview.

Not pixel-perfect — Teams' renderer is closed source — but conveys the layout,
color band, sections, and text content so the user can eyeball the card before
posting to a webhook.

Reads:  reports/teams-message-banner-<date>.json
Writes: reports/teams-preview-<date>.html
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "reports"

COLOR_MAP = {
    "good": "#107C10",       # green
    "warning": "#D29200",    # amber
    "attention": "#A4262C",  # red
    "accent": "#0078D4",     # blue
    "default": "#323130",    # neutral
}

SIZE_MAP = {
    "Small": "12px",
    "Default": "14px",
    "Medium": "16px",
    "Large": "20px",
    "ExtraLarge": "26px",
}

SPACING_MAP = {
    "None": "0",
    "Small": "4px",
    "Default": "8px",
    "Medium": "16px",
    "Large": "24px",
    "ExtraLarge": "32px",
}


def _md_to_html(text: str) -> str:
    """Adaptive-Card-flavored markdown → HTML. Supports bold, italic, code, line breaks."""
    out = escape(text)
    # Code spans first so other rules don't eat them.
    out = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", out)
    # Bold + italic.
    out = re.sub(r"\*\*([^*]+)\*\*", lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = re.sub(r"\*([^*]+)\*", lambda m: f"<em>{m.group(1)}</em>", out)
    # Newlines.
    out = out.replace("\n", "<br>")
    return out


def render_textblock(block: dict[str, Any]) -> str:
    color = COLOR_MAP.get(block.get("color", "default"), "#323130")
    weight = "700" if block.get("weight") == "Bolder" else "400"
    size = SIZE_MAP.get(block.get("size", "Default"), "14px")
    margin = SPACING_MAP.get(block.get("spacing", "Default"), "8px")
    separator = block.get("separator", False)
    sep_html = '<hr style="border:none;border-top:1px solid #edebe9;margin:16px 0 0;">' if separator else ""
    text = _md_to_html(block.get("text", ""))
    return (
        f'{sep_html}<div style="margin-top:{margin};color:{color};font-weight:{weight};'
        f'font-size:{size};line-height:1.5;word-wrap:break-word;">{text}</div>'
    )


def render_factset(block: dict[str, Any]) -> str:
    rows = "".join(
        f'<tr><th style="text-align:left;padding:4px 16px 4px 0;color:#605e5c;'
        f'font-weight:400;vertical-align:top;white-space:nowrap;">{escape(f["title"])}</th>'
        f'<td style="padding:4px 0;font-weight:600;vertical-align:top;">{_md_to_html(f["value"])}</td></tr>'
        for f in block.get("facts", [])
    )
    return f'<table style="border-collapse:collapse;margin:12px 0;width:100%;">{rows}</table>'


def render_container(block: dict[str, Any]) -> str:
    inner = "".join(render_block(b) for b in block.get("items", []))
    margin = SPACING_MAP.get(block.get("spacing", "Default"), "8px")
    return f'<div style="margin-top:{margin};padding:8px 12px;background:#faf9f8;border-radius:4px;">{inner}</div>'


def render_block(block: dict[str, Any]) -> str:
    t = block.get("type")
    if t == "TextBlock":
        return render_textblock(block)
    if t == "FactSet":
        return render_factset(block)
    if t == "Container":
        return render_container(block)
    return f'<div style="color:#a4262c;">[unsupported: {escape(t or "?")}]</div>'


def render_card(payload: dict[str, Any]) -> str:
    card = payload["attachments"][0]["content"]
    body_blocks = card.get("body", [])
    body_html = "".join(render_block(b) for b in body_blocks)

    # Pick a banner color from the second TextBlock (status line) — it carries the color attribute.
    band_color = "#D29200"
    for b in body_blocks[:3]:
        if b.get("type") == "TextBlock" and b.get("color") in COLOR_MAP and b.get("color") != "default":
            band_color = COLOR_MAP[b["color"]]
            break

    return f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Teams card preview</title>
<style>
  body {{ font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; background:#f3f2f1; margin:0; padding:32px; color:#252423; }}
  .card {{ max-width: 920px; margin:0 auto; background:white; border-radius:6px;
           box-shadow:0 1px 3px rgba(0,0,0,0.1); overflow:hidden; }}
  .bar {{ height:6px; background:{band_color}; }}
  .body {{ padding:24px 28px; }}
  code {{ background:#f3f2f1; padding:2px 5px; border-radius:3px; font-size:12.5px;
          font-family:'SF Mono', Consolas, monospace; }}
  strong {{ color:#201f1e; }}
  hr {{ border:none; border-top:1px solid #edebe9; }}
  .meta {{ max-width:920px; margin:16px auto 0; color:#605e5c; font-size:12px; text-align:center; }}
</style></head>
<body>
  <div class='card'>
    <div class='bar'></div>
    <div class='body'>{body_html}</div>
  </div>
  <div class='meta'>
    Preview of the Adaptive Card that would be posted to <code>TEAMS_WEBHOOK_URL</code>.<br>
    Approximation of Teams' renderer — actual styling will differ slightly.
  </div>
</body></html>
"""


def main() -> int:
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        src = REPORTS_DIR / f"teams-message-banner-{today}.json"
    if not src.exists():
        sys.exit(f"missing {src}")
    payload = json.loads(src.read_text())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = REPORTS_DIR / f"teams-preview-{today}.html"
    out.write_text(render_card(payload))
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
