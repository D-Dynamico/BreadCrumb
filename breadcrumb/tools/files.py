"""Files tool: list downloaded files and read PDF text with page and line locators.

Text comes from the PDF's text layer (pdfplumber, D25). A file with no text layer,
such as a scanned image, is reported as unreadable: the worker must say so, never
guess at what it says (D16).
"""

from __future__ import annotations

from pathlib import Path

import pdfplumber

MAX_CHARS = 12_000


class FilesError(Exception):
    """Shown to the model as the outcome of the action."""


class FilesTool:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def names(self) -> tuple[str, ...]:
        if not self.folder.exists():
            return ()
        return tuple(sorted(p.name for p in self.folder.glob("*") if p.is_file()))

    def list(self) -> str:
        files = (
            sorted(p.name for p in self.folder.glob("*") if p.is_file())
            if self.folder.exists()
            else []
        )
        return "\n".join(files) if files else "No files downloaded yet."

    def read(self, name: str) -> str:
        path = (self.folder / name).resolve()
        if path.parent != self.folder.resolve() or not path.is_file():
            raise FilesError(f"no downloaded file named {name!r}. Files: {self.list()}")
        if path.suffix.lower() != ".pdf":
            return path.read_text(encoding="utf-8", errors="replace")[:MAX_CHARS]
        lines: list[str] = []
        with pdfplumber.open(path) as pdf:
            for page_no, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if line.strip():
                        lines.append(f"p{page_no} L{line_no}: {line}")
        if not lines:
            return (
                f"{name} has no text layer (it is probably a scanned image), so its "
                "contents cannot be read. Do not guess what it says."
            )
        text = "\n".join(lines)
        return f"FILE: {name}\n{text[:MAX_CHARS]}"
