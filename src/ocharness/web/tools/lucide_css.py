"""Committed recipe: regenerate src/ocharness/web/vendor/lucide-icons.css.

Fetches lucide-static SVGs at LUCIDE_STATIC_VERSION (pinned: an unpinned
`latest` refetch would silently regenerate a different-versioned sheet) and
base64-encodes each raw SVG (xmlns intact) into a mask-image data URI,
emitting offline icon classes colored via currentColor. base64 is what lets
xmlns survive: the `http://www.w3.org/2000/svg` namespace rides encoded, so
the committed CSS and the produced pages stay free of http(s) URL literals
while browsers (which require xmlns on image/svg+xml roots) still parse the
icons. No runtime fetch — the vendored CSS is the committed artifact; this
script is the dev-time recipe for it.

Usage: uv run python src/ocharness/web/tools/lucide_css.py
"""

import base64
import pathlib
import urllib.request

NAMES = [
    "chevron-down",
    "chevron-right",
    "chevron-up",
    "search",
    "check",
    "x",
    "download",
    "copy",
    "sun",
    "moon",
    "monitor",
    "info",
    "external-link",
    "table-2",
    "sigma",
]

# Classes the templates never consume today; kept on purpose: the vendor sheet
# is the icon library layer, not a per-page trim. (Gate 2 F4.)
UNCONSUMED = [
    "chevron-right",
    "chevron-up",
    "external-link",
    "info",
    "sigma",
    "table-2",
]

DEST = pathlib.Path(__file__).resolve().parent.parent / "vendor" / "lucide-icons.css"

# Bump only deliberately: the committed banner and README attribution follow this.
LUCIDE_STATIC_VERSION = "1.40.0"

ver = LUCIDE_STATIC_VERSION
rules = [
    f"/*! Lucide icons v{ver} | ISC License | https://lucide.dev */",
    "/*! Offline icon classes: mask-image base64 data URIs colored via currentColor (no remote fetch).",
    '    Usage: <i class="i-chevron-down" aria-hidden="true"></i>',
    "    Regenerate: python3 src/ocharness/web/tools/lucide_css.py",
    f"    {len(NAMES) - len(UNCONSUMED)} of {len(NAMES)} icons are consumed by the templates; the other",
    f"    {len(UNCONSUMED)} ({', '.join(UNCONSUMED)}) are unconsumed today and kept intentionally:",
    "    this sheet is the library layer. */",
]
missing = []
for name in NAMES:
    try:
        svg = urllib.request.urlopen(
            f"https://unpkg.com/lucide-static@{ver}/icons/{name}.svg"
        ).read()
    except Exception:
        missing.append(name)
        continue
    b64 = base64.b64encode(svg).decode("ascii")
    rules.append(f'.i-{name}{{--icon:url("data:image/svg+xml;base64,{b64}")}}')
DEST.write_text("\n".join(rules) + "\n", encoding="utf-8")
print("version:", ver, "| icons:", len(NAMES) - len(missing), "| missing:", missing, "->", DEST)
