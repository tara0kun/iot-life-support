#!/usr/bin/env python3
"""Convert FAMILY_GUIDE.md to PDF with Japanese-friendly styling."""
from pathlib import Path
from markdown_pdf import MarkdownPdf, Section

ROOT = Path(__file__).resolve().parent.parent
md_path = ROOT / "FAMILY_GUIDE.md"
pdf_path = ROOT / "FAMILY_GUIDE.pdf"

css = """
body { font-family: 'Noto Sans CJK JP', 'Noto Sans JP', 'Hiragino Sans', 'Yu Gothic', sans-serif;
       font-size: 11pt; line-height: 1.6; color: #222; }
h1 { font-size: 22pt; border-bottom: 3px solid #4a90e2; padding-bottom: 8px; margin-top: 24px; }
h2 { font-size: 16pt; border-bottom: 2px solid #ddd; padding-bottom: 4px; margin-top: 20px; color: #2c5aa0; }
h3 { font-size: 13pt; color: #2c5aa0; margin-top: 16px; }
h4 { font-size: 12pt; color: #555; margin-top: 12px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 10pt; }
th, td { border: 1px solid #bbb; padding: 6px 10px; text-align: left; vertical-align: top; }
th { background-color: #eef4fb; font-weight: bold; }
code { background: #f4f4f4; padding: 1px 4px; border-radius: 3px; font-size: 10pt; }
pre { background: #f4f4f4; padding: 8px; border-radius: 4px; font-size: 10pt; overflow-x: auto; }
blockquote { border-left: 4px solid #4a90e2; margin: 8px 0; padding: 4px 12px;
             background: #f3f8fd; color: #333; }
hr { border: none; border-top: 1px solid #ccc; margin: 16px 0; }
ul, ol { margin: 4px 0; padding-left: 24px; }
li { margin: 2px 0; }
strong { color: #b03030; }
a { color: #2c5aa0; }
"""

import re

text = md_path.read_text(encoding="utf-8")
# Strip manual TOC anchor links: [label](#anchor) -> label
text = re.sub(r"\[([^\]]+)\]\(#[^)]+\)", r"\1", text)

pdf = MarkdownPdf(toc_level=2, optimize=True)
pdf.meta["title"] = "家族用マニュアル ── おばあちゃん見守りシステム"
pdf.meta["author"] = "IoT生活サポートシステム"
pdf.add_section(Section(text, toc=True), user_css=css)
pdf.save(str(pdf_path))

print(f"Wrote: {pdf_path} ({pdf_path.stat().st_size:,} bytes)")
