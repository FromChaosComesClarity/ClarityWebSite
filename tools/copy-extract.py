#!/usr/bin/env python3
"""Pull every piece of visible copy out of the site into one editable text file, and put it
back again afterwards.

    python3 tools/copy-extract.py extract  > website-copy.txt
    python3 tools/copy-extract.py apply    < website-copy.txt

⚠️ The point of this is the ROUND TRIP. Extract, change nothing, apply, and the HTML must come
back byte-for-byte identical. That is checked by `verify`, and it is what makes it safe to
hand the file to someone and take their rewrite back.

⚠️ Replacements are applied by character offset, right to left, so the HTML is never
re-serialised. A parser that rebuilt the document would reformat every page it touched and bury
the actual edits in noise.

Inline markup survives as light markers, so the file stays readable:

    **bold**   *italic*   `code`   [text](href)

Anything else in a string (an entity, a <br>, a nested span) is left exactly as it is.
"""

import glob
import html
import os
import re
import sys
from html.parser import HTMLParser

# Text containers. The first group always holds copy; the second only counts when it has
# direct text of its own and no container from the first group inside it.
ALWAYS = {"h1", "h2", "h3", "h4", "h5", "p", "li", "figcaption", "button", "summary"}
MAYBE = {"div", "span", "a", "td", "th", "strong", "em"}
SKIP_INSIDE = {"script", "style", "svg", "code", "pre"}

INLINE_OUT = [
    (re.compile(r"<strong>(.*?)</strong>", re.S), r"**\1**"),
    # ⚠️ <b> and <i> are deliberately NOT converted. The markers are one-way: ** only ever
    # comes back as <strong> and * as <em>, so turning a <b> into ** silently rewrote it as
    # <strong> the moment anyone edited the block it sat in. The site uses <b> about fifty
    # times, so that was a landmine on nearly every page. Left as raw tags they survive the
    # round trip untouched, which is the whole promise of this file.
    (re.compile(r"<em>(.*?)</em>", re.S), r"*\1*"),
    (re.compile(r"<code>(.*?)</code>", re.S), r"`\1`"),
    # ⚠️ Only a bare <a href>. A link carrying a class or a target must keep its raw tag,
    # the marker form has nowhere to put the other attributes, and dropping them silently
    # rewrote the site's own brand link into a plain one.
    (re.compile(r'<a href="([^"]*)">(.*?)</a>', re.S), r"[\2](\1)"),
]
INLINE_IN = [
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.S), r"<em>\1</em>"),
    (re.compile(r"`(.+?)`", re.S), r"<code>\1</code>"),
    (re.compile(r"\[(.+?)\]\(([^)]*)\)", re.S), r'<a href="\2">\1</a>'),
]


def attr_escape(text):
    """Escape for a double-quoted attribute, and ⚠️ NOT the apostrophe.

    html.escape(quote=True) turns ' into &#x27;, which is valid but is not what the pages
    contain: an apostrophe inside a double-quoted attribute needs no escaping, and rewriting
    every "doesn't" in the site's descriptions is a diff nobody asked for."""
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def to_markers(fragment):
    out = fragment
    for pat, rep in INLINE_OUT:
        out = pat.sub(rep, out)
    return " ".join(out.split())


def from_markers(text):
    out = text
    for pat, rep in INLINE_IN:
        out = pat.sub(rep, out)
    return out


class Collector(HTMLParser):
    """Records the byte span of each text container's inner content."""

    def __init__(self, src):
        super().__init__(convert_charrefs=False)
        self.src = src
        self.lines = [0]
        for line in src.split("\n"):
            self.lines.append(self.lines[-1] + len(line) + 1)
        self.stack = []
        self.spans = []          # (start, end, tag)
        self.skip_depth = 0

    def _pos(self):
        line, col = self.getpos()
        return self.lines[line - 1] + col

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_INSIDE:
            self.skip_depth += 1
        if self.skip_depth:
            return
        if tag in ALWAYS or tag in MAYBE:
            end_of_tag = self.src.index(">", self._pos()) + 1
            self.stack.append((tag, end_of_tag))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in SKIP_INSIDE:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                _, start = self.stack.pop(i)
                self.spans.append((start, self._pos(), tag))
                del self.stack[i:]
                break


def containers(path):
    """Every span worth editing, outermost first, with nothing nested inside another."""
    src = open(path, encoding="utf-8").read()
    c = Collector(src)
    c.feed(src)
    spans = sorted(c.spans, key=lambda s: (s[0], -(s[1])))

    chosen = []
    for start, end, tag in spans:
        inner = src[start:end]
        # A container that holds another container is scaffolding, not copy.
        if re.search(r"<(?:%s)\b" % "|".join(sorted(ALWAYS)), inner):
            continue
        # ⚠️ A div holding a nav, a link or another span is layout, not a sentence. Taking it
        # collapsed the whole header onto one line and rewrote the links inside it.
        if tag in MAYBE and re.search(r"<(?!/?(?:strong|b|em|i|code|br)\b)[a-z]", inner, re.I):
            continue
        # Must actually say something once its own markup is stripped. ⚠️ The filter lives
        # HERE and not in extract(), because both sides number the blocks from this same list
        # Filtering on one side only would shift every key on the other.
        plain = re.sub(r"<[^>]+>", "", inner).strip()
        if not plain:
            continue
        # A lone separator glyph is decoration, not copy.
        if len(plain) < 2 and not plain.isalnum():
            continue
        # And must not sit inside a span already taken.
        if any(start >= s and end <= e for s, e, _ in chosen):
            continue
        chosen.append((start, end, tag))
    return src, sorted(chosen)


META = [
    ("title", re.compile(r"<title>(.*?)</title>", re.S)),
    ("meta:description", re.compile(r'<meta name="description" content="(.*?)">', re.S)),
    ("og:title", re.compile(r'<meta property="og:title" content="(.*?)">', re.S)),
    ("og:description", re.compile(r'<meta property="og:description" content="(.*?)">', re.S)),
]


def extract():
    print(HEADER)
    for path in sorted(glob.glob("*.html")):
        src = open(path, encoding="utf-8").read()
        print("\n" + "=" * 78)
        print("PAGE: %s" % path)
        print("=" * 78)

        for name, pat in META:
            m = pat.search(src)
            if m:
                print("\n### %s | %s" % (path, name))
                print(html.unescape(m.group(1)).strip())

        src, spans = containers(path)
        for n, (start, end, tag) in enumerate(spans, 1):
            body = to_markers(src[start:end])
            if not body:
                continue
            print("\n### %s | %03d | %s" % (path, n, tag))
            print(body)


def parse_stdin(stream):
    """The edited file, back into {(page, key): text}."""
    out, key, buf = {}, None, []
    for raw in stream.read().split("\n"):
        if raw.startswith("### "):
            if key:
                out[key] = "\n".join(buf).strip()
            parts = [p.strip() for p in raw[4:].split("|")]
            key = (parts[0], parts[1]) if len(parts) >= 2 else None
            buf = []
        elif key is not None:
            if raw.startswith("=" * 10) or raw.startswith("PAGE: "):
                out[key] = "\n".join(buf).strip()
                key, buf = None, []
            else:
                buf.append(raw)
    if key:
        out[key] = "\n".join(buf).strip()
    return out


def apply_edits(edited):
    changed_files = 0
    for path in sorted(glob.glob("*.html")):
        src = open(path, encoding="utf-8").read()
        original = src

        # ⚠️ BODY FIRST, META SECOND, and never the other way round. containers() computes
        # offsets against the file ON DISK; replacing the <title> first shifts everything
        # after it, so every body replacement then lands a few characters early. That is
        # what ate a closing </a> and left a doubled entity behind it. The meta patterns
        # re-search the string, so running them last costs nothing.
        _, spans = containers(path)
        # ⚠️ Right to left: every replacement changes the offsets of everything after it.
        for n, (start, end, tag) in reversed(list(enumerate(spans, 1))):
            k = (path, "%03d" % n)
            if k not in edited:
                continue
            # ⚠️ Only touch what actually changed. Rewriting an untouched paragraph would
            # reflow it onto one line and bury the real edits in whitespace noise.
            if to_markers(src[start:end]) == " ".join(edited[k].split()):
                continue
            src = src[:start] + from_markers(edited[k]) + src[end:]

        for name, pat in META:
            k = (path, name)
            if k not in edited:
                continue
            m = pat.search(src)
            if not m:
                continue
            # ⚠️ The title is unescaped on the way out, so it has to be escaped on the way
            # back in, otherwise "Ports &amp; Mods" returns as raw "&" and the page carries
            # an unescaped ampersand.
            new_text = attr_escape(edited[k]) if name != "title" else html.escape(edited[k], quote=False)
            if new_text != m.group(1):
                src = src[: m.start(1)] + new_text + src[m.end(1):]

        if src != original:
            open(path, "w", encoding="utf-8").write(src)
            changed_files += 1
            print("  updated %s" % path, file=sys.stderr)
    print("  %d file(s) changed" % changed_files, file=sys.stderr)


def check_markers():
    """Prove the marker conversion itself is lossless, block by block.

    ⚠️ This exists because the round-trip check below CANNOT catch a bad conversion.
    apply_edits() skips any block whose text is unchanged, so extracting and re-applying
    without editing never runs from_markers() at all, and a <b> that comes back as
    <strong> therefore passed a clean `verify` for as long as nobody edited that block.

    So: take every block's real markup, push it out to markers and straight back, and
    require the result to match. Whitespace is normalised on both sides because the
    markers do not carry line breaks, and a rewritten block legitimately reflows onto one
    line, and that is not what we are hunting here. Tags, entities and attributes are.
    """
    problems = []
    for path in sorted(glob.glob("*.html")):
        src = open(path, encoding="utf-8").read()
        _, spans = containers(path)
        for n, (start, end, tag) in enumerate(spans, 1):
            orig = src[start:end]
            rebuilt = from_markers(to_markers(orig))
            if " ".join(orig.split()) != " ".join(rebuilt.split()):
                problems.append((path, "%03d" % n, tag, orig.strip()[:70], rebuilt.strip()[:70]))
    return problems


def verify():
    """Extract, apply the extraction unchanged, and prove nothing moved."""
    import io, subprocess, shutil, tempfile
    before = {p: open(p, encoding="utf-8").read() for p in sorted(glob.glob("*.html"))}
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    extract()
    sys.stdout = old
    apply_edits(parse_stdin(io.StringIO(buf.getvalue())))
    bad = [p for p, s in before.items() if open(p, encoding="utf-8").read() != s]
    if bad:
        print("ROUND TRIP CHANGED: %s" % ", ".join(bad), file=sys.stderr)
        for p in bad:
            open(p, "w", encoding="utf-8").write(before[p])
            print("  restored %s" % p, file=sys.stderr)
        return 1
    problems = check_markers()
    if problems:
        print("MARKER CONVERSION IS LOSSY in %d block(s):" % len(problems), file=sys.stderr)
        for path, n, tag, was, now in problems[:12]:
            print("  %s %s <%s>" % (path, n, tag), file=sys.stderr)
            print("      was: %s" % was, file=sys.stderr)
            print("      now: %s" % now, file=sys.stderr)
        if len(problems) > 12:
            print("  ... and %d more" % (len(problems) - 12), file=sys.stderr)
        return 1

    print("round trip is lossless across %d pages" % len(before), file=sys.stderr)
    print("marker conversion is lossless across every block", file=sys.stderr)
    return 0


HEADER = """CLARITY: ALL WEBSITE COPY
=================================

Every word on the site, in one file. Rewrite anything below in your own voice and hand it
back; it goes straight into the pages.

HOW TO EDIT
-----------
  * Change the text UNDER a "###" line. Never change the "###" line itself. That is the
    address the text goes back to.
  * A block can be as many lines as you like. It ends at the next "###".
  * To delete a piece of copy, leave the block empty.

FORMATTING YOU CAN USE
----------------------
  **bold**            *italic*            `code`            [link text](page.html)

Anything else, a <br> or an emoji, just leave as you find it.
    No em dashes, please: a comma, a colon or a full stop instead.

WHAT THE LABELS MEAN
--------------------
  title             the browser tab and Google's headline
  meta:description  the sentence under it in search results
  og:title / og:description   what appears when the link is pasted into chat
  001, 002, ...     a piece of copy on the page, in the order you read it
  h1 h2 h3          headings, largest first
  p                 a paragraph      li  a bullet      figcaption  a caption under a picture
  div span a        short labels, tags and link text
"""

if __name__ == "__main__":
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    cmd = sys.argv[1] if len(sys.argv) > 1 else "extract"
    if cmd == "extract":
        extract()
    elif cmd == "apply":
        apply_edits(parse_stdin(sys.stdin))
    elif cmd == "verify":
        sys.exit(verify())
    else:
        print(__doc__)
        sys.exit(2)
