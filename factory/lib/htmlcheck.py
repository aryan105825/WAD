# @exports: check_page(html: str) -> list[tuple[str, bool, str]]   # (check_name, ok, detail); names: viewport-meta, title, input-labels, no-fixed-width, html-lang, landmark
# @imports: none (stdlib html.parser, re)
# @env: none
# @schema: fixed-width rule: any width > 480px (inline style, width attribute, or <style> rule outside min-width media queries) on container elements; max-width/min-width are ignored
# @schema: labelled control = aria-label | aria-labelledby | wrapped in <label> | id referenced by <label for>; hidden/submit/button/reset/image inputs exempt
"""Static UX checks on a served HTML page, for acceptance checks on UI items."""
import re
from html.parser import HTMLParser

MAX_FIXED_PX = 480
CONTAINERS = {"body", "main", "div", "section", "article", "form", "table", "header", "footer", "nav", "aside"}
EXEMPT_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}
CONTROL_TAGS = {"input", "select", "textarea"}

DECL = re.compile(r"(?<![-\w])width\s*:\s*(\d+(?:\.\d+)?)px", re.I)
WIDTH_ATTR = re.compile(r"^\s*(\d+)(?:px)?\s*$", re.I)
COMMENT = re.compile(r"/\*.*?\*/", re.S)
MEDIA_MIN = re.compile(r"@media[^{}]*min-width[^{}]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", re.I)
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
SKIP_SELECTOR = re.compile(r"\b(img|svg|canvas|video|picture|td|th|input|button|select|textarea)\b", re.I)


class _Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.has_html = False
        self.html_lang = ""
        self.has_viewport = False
        self.title_text = ""
        self.in_title = False
        self.in_style = False
        self.style_blocks: list[str] = []
        self.label_depth = 0
        self.label_for: set[str] = set()
        self.controls: list[dict] = []
        self.inline_widths: list[tuple[str, float]] = []
        self.landmark = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "html":
            self.has_html = True
            self.html_lang = a.get("lang", "").strip()
        elif tag == "meta" and a.get("name", "").lower() == "viewport":
            if "width=device-width" in a.get("content", "").replace(" ", "").lower():
                self.has_viewport = True
        elif tag == "title":
            self.in_title = True
        elif tag == "style":
            self.in_style = True
            self.style_blocks.append("")
        elif tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.label_for.add(a["for"])
        elif tag in CONTROL_TAGS:
            if not (tag == "input" and a.get("type", "text").lower() in EXEMPT_INPUT_TYPES):
                self.controls.append(
                    {
                        "tag": tag,
                        "id": a.get("id", ""),
                        "name": a.get("name", ""),
                        "labelled": bool(
                            a.get("aria-label", "").strip()
                            or a.get("aria-labelledby", "").strip()
                            or self.label_depth > 0
                        ),
                    }
                )
        if tag in ("main", "nav") or a.get("role", "").lower() in ("main", "navigation"):
            self.landmark = True
        if tag in CONTAINERS:
            for m in DECL.finditer(a.get("style", "")):
                self.inline_widths.append((tag, float(m.group(1))))
            wm = WIDTH_ATTR.match(a.get("width", ""))
            if wm:
                self.inline_widths.append((tag, float(wm.group(1))))

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "style":
            self.in_style = False
        elif tag == "label" and self.label_depth > 0:
            self.label_depth -= 1

    def handle_data(self, data):
        if self.in_title:
            self.title_text += data
        if self.in_style and self.style_blocks:
            self.style_blocks[-1] += data


def _block_widths(css: str):
    css = MEDIA_MIN.sub("", COMMENT.sub("", css))
    for m in RULE.finditer(css):
        selector, body = m.group(1).strip(), m.group(2)
        if SKIP_SELECTOR.search(selector):
            continue
        for d in DECL.finditer(body):
            yield selector, float(d.group(1))


def check_page(html: str) -> list[tuple[str, bool, str]]:
    s = _Scan()
    s.feed(html)
    s.close()
    results: list[tuple[str, bool, str]] = []

    results.append(
        (
            "viewport-meta",
            s.has_viewport,
            "ok"
            if s.has_viewport
            else '<meta name="viewport" content="width=device-width, initial-scale=1"> is missing; add it to <head>',
        )
    )

    title = s.title_text.strip()
    results.append(("title", bool(title), "ok" if title else "<title> is missing or empty; add a descriptive title"))

    unlabelled = [
        f"<{c['tag']}{' id=' + c['id'] if c['id'] else ''}{' name=' + c['name'] if c['name'] else ''}>"
        for c in s.controls
        if not c["labelled"] and not (c["id"] and c["id"] in s.label_for)
    ]
    results.append(
        (
            "input-labels",
            not unlabelled,
            "ok" if not unlabelled else "controls without a label/aria-label: " + ", ".join(unlabelled),
        )
    )

    offenders = [f"<{t}> width {w:g}px" for t, w in s.inline_widths if w > MAX_FIXED_PX]
    for css in s.style_blocks:
        offenders += [f"{sel} width {w:g}px" for sel, w in _block_widths(css) if w > MAX_FIXED_PX]
    results.append(
        (
            "no-fixed-width",
            not offenders,
            "ok"
            if not offenders
            else f"fixed widths over {MAX_FIXED_PX}px break small screens; use max-width or %: " + "; ".join(offenders),
        )
    )

    lang_ok = s.has_html and bool(s.html_lang)
    results.append(("html-lang", lang_ok, "ok" if lang_ok else '<html lang="..."> is missing; add a language attribute'))

    results.append(
        (
            "landmark",
            s.landmark,
            "ok" if s.landmark else "no <main> or <nav> landmark found; wrap primary content in <main>",
        )
    )
    return results
