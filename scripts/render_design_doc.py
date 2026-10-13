"""Render docs/design/v1-system-design.md into a standalone HTML page with Mermaid diagrams.

Usage: python scripts/render_design_doc.py <output.html>
Requires: pip install markdown
"""

import html
import re
import sys
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "design" / "v1-system-design.md"

MERMAID_RE = re.compile(r"```mermaid\n(.*?)```", re.S)


def main(out: Path) -> None:
    text = SRC.read_text(encoding="utf-8")

    # Pull Mermaid blocks out before Markdown conversion so they are not turned into <code>.
    diagrams: list[str] = []

    def stash(m: re.Match) -> str:
        diagrams.append(m.group(1))
        return f"\n\nMERMAIDBLOCK{len(diagrams) - 1}\n\n"

    text = MERMAID_RE.sub(stash, text)
    # Drop the hand-written TOC (the page builds its own sidebar).
    text = re.sub(r"## 0\. Table of contents.*?\n---\n", "", text, flags=re.S)

    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists"])
    body = md.convert(text)

    for i, src in enumerate(diagrams):
        block = (
            '<figure class="diagram"><div class="diagram-scroll">'
            f'<pre class="mermaid">{html.escape(src)}</pre></div></figure>'
        )
        body = body.replace(f"<p>MERMAIDBLOCK{i}</p>", block)

    # Wrap tables so they scroll on narrow screens.
    body = body.replace("<table>", '<div class="table-scroll"><table>').replace("</table>", "</table></div>")

    toc_items = [t for t in md.toc_tokens[0]["children"]] if md.toc_tokens else []
    toc = "\n".join(f'<li><a href="#{t["id"]}">{html.escape(t["name"])}</a></li>' for t in toc_items)

    template = (ROOT / "scripts" / "design_doc_template.html").read_text(encoding="utf-8")
    out.write_text(template.replace("{{TOC}}", toc).replace("{{BODY}}", body), encoding="utf-8")
    print(f"wrote {out} ({len(diagrams)} diagrams)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
