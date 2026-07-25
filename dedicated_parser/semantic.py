from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser


_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "footer",
        "header",
        "li",
        "main",
        "p",
        "section",
    }
)
_HEADING_TAGS = {f"h{level}": level for level in range(1, 7)}
_SKIP_TAGS = frozenset({"script", "style", "noscript"})


def normalize_space(value: str) -> str:
    return " ".join(html.unescape(value).replace("\xa0", " ").split())


def _looks_like_table_header(cells: tuple[str, ...]) -> bool:
    text = " | ".join(cells).lower()
    metric_terms = re.search(
        r"\b(?:orders?|bookings?|backlog|performance\s+obligations?)\b",
        text,
    )
    header_terms = re.search(
        r"\b(?:in\s+(?:thousands|millions|billions)|"
        r"as\s+of|consolidated|segment|three\s+months?\s+ended|"
        r"six\s+months?\s+ended|nine\s+months?\s+ended|"
        r"year\s+ended|fiscal\s+year)\b",
        text,
    )
    monetary_values = re.search(
        r"[$\u20ac\u00a3]|\d{1,3}(?:,\d{3})+|\d+\.\d+",
        text,
    )
    return (
        header_terms is not None and metric_terms is None
    ) or (
        metric_terms is not None
        and monetary_values is None
        and any(
            token in text
            for token in (
                "total",
                "current",
                "prior",
                "under",
                "consolidated",
            )
        )
    )


def _merge_table_headers(
    existing: tuple[str, ...],
    incoming: tuple[str, ...],
) -> tuple[str, ...]:
    if not existing or len(existing) != len(incoming):
        return incoming
    merged: list[str] = []
    for existing_value, incoming_value in zip(existing, incoming):
        values: list[str] = []
        for value in (existing_value, incoming_value):
            if value and value not in values:
                values.append(value)
        merged.append(" | ".join(values))
    return tuple(merged)


@dataclass(frozen=True)
class SemanticBlock:
    index: int
    kind: str
    text: str
    section_path: tuple[str, ...] = ()
    table_id: int | None = None
    row_index: int | None = None
    cells: tuple[str, ...] = ()
    header_cells: tuple[str, ...] = ()

    @property
    def search_text(self) -> str:
        parts = [*self.section_path, *self.header_cells, self.text]
        output: list[str] = []
        for part in parts:
            normalized = normalize_space(part)
            if normalized and normalized not in output:
                output.append(normalized)
        return " | ".join(output)


@dataclass(frozen=True)
class SemanticDocument:
    source_document: str
    blocks: tuple[SemanticBlock, ...]
    warning: str = ""

    @property
    def table_rows(self) -> tuple[SemanticBlock, ...]:
        return tuple(block for block in self.blocks if block.kind == "table_row")


class _SemanticHTMLParser(HTMLParser):
    def __init__(self, *, source_document: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_document = source_document
        self.blocks: list[SemanticBlock] = []
        self._skip_depth = 0
        self._table_depth = 0
        self._table_id = 0
        self._row_index = 0
        self._row_cells: list[str] = []
        self._row_cell_colspans: list[int] = []
        self._row_has_header = False
        self._cell_parts: list[str] | None = None
        self._cell_colspan = 1
        self._cell_is_header = False
        self._table_headers: tuple[str, ...] = ()
        self._text_parts: list[str] = []
        self._heading_level = 0
        self._heading_parts: list[str] = []
        self._headings: dict[int, str] = {}

    def _section_path(self) -> tuple[str, ...]:
        return tuple(
            self._headings[level]
            for level in sorted(self._headings)
            if self._headings[level]
        )

    def _append_block(
        self,
        *,
        kind: str,
        text: str,
        table_id: int | None = None,
        row_index: int | None = None,
        cells: tuple[str, ...] = (),
        header_cells: tuple[str, ...] = (),
    ) -> None:
        normalized = normalize_space(text)
        if not normalized:
            return
        if (
            kind == "paragraph"
            and self.blocks
            and self.blocks[-1].kind == kind
            and self.blocks[-1].text == normalized
            and self.blocks[-1].section_path == self._section_path()
        ):
            return
        self.blocks.append(
            SemanticBlock(
                index=len(self.blocks),
                kind=kind,
                text=normalized,
                section_path=self._section_path(),
                table_id=table_id,
                row_index=row_index,
                cells=cells,
                header_cells=header_cells,
            )
        )

    def _flush_text(self) -> None:
        if self._text_parts:
            self._append_block(
                kind="paragraph",
                text=" ".join(self._text_parts),
            )
            self._text_parts.clear()

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _HEADING_TAGS:
            self._flush_text()
            self._heading_level = _HEADING_TAGS[tag]
            self._heading_parts = []
            return
        if tag == "table":
            self._flush_text()
            self._table_depth += 1
            if self._table_depth == 1:
                self._table_id += 1
                self._row_index = 0
                self._table_headers = ()
            return
        if self._table_depth:
            if tag == "tr":
                self._row_cells = []
                self._row_cell_colspans = []
                self._row_has_header = False
            elif tag in {"td", "th"}:
                self._cell_parts = []
                attributes = {
                    name.lower(): value
                    for name, value in attrs
                    if value is not None
                }
                try:
                    self._cell_colspan = max(
                        1,
                        min(int(attributes.get("colspan", "1")), 100),
                    )
                except ValueError:
                    self._cell_colspan = 1
                self._cell_is_header = tag == "th"
                self._row_has_header = self._row_has_header or tag == "th"
            elif tag == "br" and self._cell_parts is not None:
                self._cell_parts.append(" ")
            return
        if tag in _BLOCK_TAGS:
            self._flush_text()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in _HEADING_TAGS and self._heading_level:
            heading = normalize_space(" ".join(self._heading_parts))
            level = self._heading_level
            self._heading_level = 0
            self._heading_parts = []
            if heading:
                self._headings = {
                    existing_level: existing_heading
                    for existing_level, existing_heading in self._headings.items()
                    if existing_level < level
                }
                self._headings[level] = heading
                self._append_block(kind="heading", text=heading)
            return
        if self._table_depth:
            if tag in {"td", "th"} and self._cell_parts is not None:
                value = normalize_space(" ".join(self._cell_parts))
                self._row_cells.append(value)
                self._row_cell_colspans.append(self._cell_colspan)
                self._cell_parts = None
                self._cell_colspan = 1
                self._cell_is_header = False
            elif tag == "tr":
                raw_cells = tuple(self._row_cells)
                if any(raw_cells):
                    is_header = self._row_has_header or (
                        _looks_like_table_header(raw_cells)
                    )
                    expanded_cells: list[str] = []
                    for value, colspan in zip(
                        self._row_cells,
                        self._row_cell_colspans,
                    ):
                        expanded_cells.append(value)
                        expanded_cells.extend(
                            value if is_header else ""
                            for _ in range(colspan - 1)
                        )
                    cells = tuple(expanded_cells)
                    if is_header:
                        self._table_headers = _merge_table_headers(
                            self._table_headers,
                            cells,
                        )
                    self._append_block(
                        kind="table_row",
                        text=" | ".join(cells),
                        table_id=self._table_id,
                        row_index=self._row_index,
                        cells=cells,
                        header_cells=(
                            ()
                            if is_header
                            else self._table_headers
                        ),
                    )
                    self._row_index += 1
                self._row_cells = []
                self._row_cell_colspans = []
                self._row_has_header = False
            elif tag == "table":
                self._table_depth -= 1
            return
        if tag in _BLOCK_TAGS:
            self._flush_text()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._heading_level:
            self._heading_parts.append(data)
        elif self._table_depth and self._cell_parts is not None:
            self._cell_parts.append(data)
        elif not self._table_depth:
            self._text_parts.append(data)

    def close(self) -> None:
        super().close()
        self._flush_text()


def parse_semantic_document(
    document_text: str,
    *,
    source_document: str,
) -> SemanticDocument:
    parser = _SemanticHTMLParser(source_document=source_document)
    warning = ""
    try:
        parser.feed(document_text)
        parser.close()
    except (AssertionError, ValueError) as exc:
        warning = f"{type(exc).__name__}:{exc}"
    if parser.blocks:
        return SemanticDocument(
            source_document=source_document,
            blocks=tuple(parser.blocks),
            warning=warning,
        )
    plain_blocks = [
        normalize_space(block)
        for block in re.split(r"(?:\r?\n){2,}", document_text)
        if normalize_space(block)
    ]
    return SemanticDocument(
        source_document=source_document,
        blocks=tuple(
            SemanticBlock(index=index, kind="paragraph", text=text)
            for index, text in enumerate(plain_blocks)
        ),
        warning=warning,
    )
