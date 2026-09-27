#!/usr/bin/env python3
"""Validate local report assets and assemble the GitHub Pages artifact."""

from __future__ import annotations

import json
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
OUTPUT = ROOT / "dist"
CUSTOM_DOMAIN = "mir3map.iamcheyan.com"


class LocalReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids.add(element_id)
        for name in ("href", "src"):
            value = values.get(name)
            if value:
                self.references.append(value)


def validate_links() -> None:
    index = SITE / "index.html"
    parser = LocalReferences()
    parser.feed(index.read_text(encoding="utf-8"))
    if parser.duplicate_ids:
        raise ValueError(f"duplicate HTML ids: {', '.join(sorted(parser.duplicate_ids))}")

    root = SITE.resolve()
    for reference in parser.references:
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            continue
        if url.fragment and not url.path and url.fragment not in parser.ids:
            raise ValueError(f"missing in-page target: #{url.fragment}")
        if not url.path:
            continue
        target = (SITE / unquote(url.path)).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"HTML reference escapes the site root: {reference}")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            raise ValueError(f"HTML reference does not exist: {reference}")


def build() -> None:
    required = (
        SITE / "index.html",
        SITE / "assets" / "report.css",
        SITE / "assets" / "report.js",
        SITE / "data" / "audit.json",
        SITE / "data" / "comparisons.csv",
        SITE / "data" / "issues.json",
        SITE / "CNAME",
    )
    missing = [path.relative_to(SITE).as_posix() for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing static site inputs: {', '.join(missing)}")
    if (SITE / "CNAME").read_text(encoding="utf-8").strip() != CUSTOM_DOMAIN:
        raise ValueError("site/CNAME does not match the configured Pages custom domain")
    validate_links()

    # dist/ is this script's generated Pages artifact, never source data.
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    shutil.copytree(SITE, OUTPUT)
    files = sum(1 for path in OUTPUT.rglob("*") if path.is_file())
    print(json.dumps({"valid": True, "output": "dist", "files": files}, sort_keys=True))


if __name__ == "__main__":
    build()
