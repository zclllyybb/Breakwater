from __future__ import annotations

import re
from pathlib import Path
from string import Template


PROMPT_FILE = Path(__file__).resolve().parents[1] / "prompts" / "breakwater.md"
SECTION_RE = re.compile(r"^<!-- prompt: (?P<name>[a-zA-Z0-9_-]+) -->\n(?P<body>.*?)^<!-- /prompt -->", re.M | re.S)


class PromptLibrary:
    def __init__(self, path: Path = PROMPT_FILE, variables: dict[str, object] | None = None):
        self.path = path
        self.variables = {key: str(value) for key, value in (variables or {}).items() if value is not None}
        self._mtime_ns = 0
        self._sections = self._load()

    def render(self, name: str, **values: object) -> str:
        self._reload_if_changed()
        try:
            template = self._sections[name]
        except KeyError as exc:
            raise KeyError(f"prompt section not found: {name}") from exc
        merged = dict(self.variables)
        merged.update({key: str(value) for key, value in values.items() if value is not None})
        return Template(template).safe_substitute(merged).strip()

    def _reload_if_changed(self) -> None:
        mtime_ns = self.path.stat().st_mtime_ns
        if mtime_ns != self._mtime_ns:
            self._sections = self._load()

    def _load(self) -> dict[str, str]:
        text = self.path.read_text(encoding="utf-8")
        sections = {match.group("name"): match.group("body").strip() for match in SECTION_RE.finditer(text)}
        if not sections:
            raise RuntimeError(f"no prompt sections found in {self.path}")
        self._mtime_ns = self.path.stat().st_mtime_ns
        return sections
