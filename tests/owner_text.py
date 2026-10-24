"""What the owner reads, for tests (CONVENTIONS §10, §11).

Catalog texts carry a Markdown subset that the outbox renders as Telegram HTML (`shared/outbox/domain.render`):
bold titles and labels, code, bullets, and blank lines between sections. That layout is checked byte for byte by the
goldens (`tests/golden/`). A feature's own tests check the words and their order:

- `shown(text)`: a queued text (a catalog entry, a service's reply) as the owner reads it, without its layout:
  markers stripped and blank lines dropped.
- `read(request)`: the same for a text the fake Messenger got, which went as HTML when it had markup.
- `html_problems(text)`: why Telegram would answer "can't parse entities" to `text` sent with `parse_mode="HTML"`;
  an empty list means it parses. It knows the tags the outbox writes (`b`, `code`, `pre`, `a`) and Telegram's
  nesting rule (Bot API "Formatting options"): bold may hold a link, but `code` sits only on its own or directly
  inside `pre`, and nothing nests in itself. Text may hold no raw `<`, `>` or `&`.
"""

import html
import re
from html.parser import HTMLParser
from typing import Final

from tests.fakes.messenger import Request
from yordamchi.shared.outbox.domain import plain_text

ALLOWED: Final = frozenset({"b", "code", "pre", "a"})
_TAG: Final = re.compile(r"<[^>]*>")


def _unlaid(text: str) -> str:
    return "\n".join(line for line in text.split("\n") if line.strip())


def shown(text: str) -> str:
    return _unlaid(plain_text(text))


def read(request: Request) -> str:
    text = str(request.payload.get("text", ""))
    if request.payload.get("parse_mode") == "HTML":
        text = html.unescape(_TAG.sub("", text))
    return _unlaid(text)


class _Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.open: list[str] = []
        self.problems: list[str] = []
        self.visible = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED:
            self.problems.append(f"unsupported tag <{tag}>")
        if tag in self.open:
            self.problems.append(f"<{tag}> inside <{tag}>")
        if tag == "code" and self.open and self.open[-1] != "pre":
            self.problems.append(f"<code> inside <{self.open[-1]}>")
        if tag in {"pre", "b", "a"} and "code" in self.open:
            self.problems.append(f"<{tag}> inside <code>")
        names = dict(attrs)
        if tag == "a" and not (names.keys() == {"href"} and (names["href"] or "").startswith("https://")):
            self.problems.append(f"<a> with {attrs}")
        if tag == "code" and attrs and not (self.open[-1:] == ["pre"] and [name for name, _ in attrs] == ["class"]):
            self.problems.append(f"<code> with {attrs}")
        if tag not in {"a", "code"} and attrs:
            self.problems.append(f"<{tag}> with {attrs}")
        self.open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.open or self.open[-1] != tag:
            self.problems.append(f"</{tag}> does not close {self.open[-1:] or 'anything'}")
            return
        self.open.pop()

    def handle_data(self, data: str) -> None:
        for raw in "<>&":
            if raw in data:
                self.problems.append(f"raw {raw!r} in text")
        self.visible += len(data.strip())

    def handle_entityref(self, name: str) -> None:
        if name not in {"lt", "gt", "amp", "quot"}:
            self.problems.append(f"unknown entity &{name};")
        self.visible += 1

    def handle_charref(self, name: str) -> None:
        self.visible += 1


def html_problems(text: str) -> list[str]:
    checker = _Checker()
    checker.feed(text)
    checker.close()
    if checker.open:
        checker.problems.append(f"unclosed {checker.open}")
    if checker.visible == 0:
        checker.problems.append("no visible text")
    return checker.problems
