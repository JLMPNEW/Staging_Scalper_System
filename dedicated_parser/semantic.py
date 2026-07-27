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
    # No html.unescape here: the HTML parser already decodes entities
    # (convert_charrefs=True), so unescaping again turns escaped-entity
    # literals ("&amp;lt;" meant to display as "&lt;") into "<". The plain-text
    # fallback path unescapes explicitly before calling this.
    return " ".join(value.replace("\xa0", " ").split())


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


def _clamped_span(raw: str | None) -> int:
    try:
        return max(1, min(int(str(raw or "1")), 100))
    except ValueError:
        return 1


def _merge_table_headers(
    existing: tuple[str, ...],
    incoming: tuple[str, ...],
) -> tuple[str, ...]:
    if not existing:
        return incoming
    if len(existing) != len(incoming):
        # Multi-row headers frequently differ in expanded width (e.g. the date
        # row omits the label stub). Discarding the earlier row loses the date
        # fragments entirely; pad to a common width and merge positionally.
        width = max(len(existing), len(incoming))
        existing = existing + ("",) * (width - len(existing))
        incoming = incoming + ("",) * (width - len(incoming))
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
    preamble_text: str = ""

    @property
    def search_text(self) -> str:
        parts = [
            *self.section_path,
            self.preamble_text,
            *self.header_cells,
            self.text,
        ]
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


class _TableState:
    __slots__ = (
        "table_id",
        "row_index",
        "row_cells",
        "row_cell_colspans",
        "row_cell_rowspans",
        "row_all_header",
        "row_has_cells",
        "cell_parts",
        "cell_colspan",
        "cell_rowspan",
        "table_headers",
        "pending_rowspans",
        "preamble_text",
    )

    def __init__(self, table_id: int, *, preamble_text: str = "") -> None:
        self.table_id = table_id
        self.row_index = 0
        self.row_cells: list[str] = []
        self.row_cell_colspans: list[int] = []
        self.row_cell_rowspans: list[int] = []
        self.row_all_header = True
        self.row_has_cells = False
        self.cell_parts: list[str] | None = None
        self.cell_colspan = 1
        self.cell_rowspan = 1
        self.table_headers: tuple[str, ...] = ()
        # expanded column position -> [remaining_rows, carried_value]
        self.pending_rowspans: dict[int, list] = {}
        self.preamble_text = preamble_text


class _SemanticHTMLParser(HTMLParser):
    def __init__(self, *, source_document: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_document = source_document
        self.blocks: list[SemanticBlock] = []
        self._skip_depth = 0
        self._table_id = 0
        # One state per nested table: inner tables get their own id/headers/
        # rows, and the enclosing cell resumes accumulating afterwards, so
        # layout-nested tables no longer corrupt either level.
        self._table_states: list[_TableState] = []
        self._text_parts: list[str] = []
        self._heading_level = 0
        self._heading_parts: list[str] = []
        self._headings: dict[int, str] = {}

    @property
    def _table(self) -> _TableState | None:
        return self._table_states[-1] if self._table_states else None

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
        preamble_text: str = "",
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
                preamble_text=preamble_text,
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
        if tag in _HEADING_TAGS and not self._table_states:
            # Heading tags inside table cells must not hijack cell content or
            # mutate section_path mid-table.
            self._flush_text()
            self._heading_level = _HEADING_TAGS[tag]
            self._heading_parts = []
            return
        if tag == "table":
            self._flush_text()
            self._table_id += 1
            preamble_text = ""
            if not self._table_states and self.blocks:
                previous = self.blocks[-1]
                if (
                    previous.kind == "paragraph"
                    and previous.section_path == self._section_path()
                ):
                    preamble_text = previous.text
            self._table_states.append(
                _TableState(
                    self._table_id,
                    preamble_text=preamble_text,
                )
            )
            return
        state = self._table
        if state is not None:
            if tag == "tr":
                state.row_cells = []
                state.row_cell_colspans = []
                state.row_cell_rowspans = []
                state.row_all_header = True
                state.row_has_cells = False
            elif tag in {"td", "th"}:
                state.cell_parts = []
                attributes = {
                    name.lower(): value
                    for name, value in attrs
                    if value is not None
                }
                state.cell_colspan = _clamped_span(attributes.get("colspan", "1"))
                state.cell_rowspan = _clamped_span(attributes.get("rowspan", "1"))
                state.row_has_cells = True
                if tag == "td":
                    # A row is a header row only when EVERY cell is <th>;
                    # <th scope="row"> label cells beside <td> data are data rows.
                    state.row_all_header = False
            elif tag == "br" and state.cell_parts is not None:
                state.cell_parts.append(" ")
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
        state = self._table
        if state is not None:
            if tag in {"td", "th"} and state.cell_parts is not None:
                value = normalize_space(" ".join(state.cell_parts))
                state.row_cells.append(value)
                state.row_cell_colspans.append(state.cell_colspan)
                state.row_cell_rowspans.append(state.cell_rowspan)
                state.cell_parts = None
                state.cell_colspan = 1
                state.cell_rowspan = 1
            elif tag == "tr":
                self._emit_table_row(state)
            elif tag == "table":
                self._emit_table_row(state)  # flush a row truncated mid-table
                self._table_states.pop()
            return
        if tag in _BLOCK_TAGS:
            self._flush_text()

    def _emit_table_row(self, state: _TableState) -> None:
        if state.cell_parts is not None:
            # Row/document truncated mid-cell: keep what we have.
            value = normalize_space(" ".join(state.cell_parts))
            state.row_cells.append(value)
            state.row_cell_colspans.append(state.cell_colspan)
            state.row_cell_rowspans.append(state.cell_rowspan)
            state.cell_parts = None
            state.cell_colspan = 1
            state.cell_rowspan = 1
        has_carryover = any(
            entry[0] > 0 for entry in state.pending_rowspans.values()
        )
        if not state.row_cells and not has_carryover:
            return
        raw_cells = tuple(state.row_cells)
        if not any(raw_cells) and not has_carryover:
            state.row_cells = []
            state.row_cell_colspans = []
            state.row_cell_rowspans = []
            state.row_all_header = True
            state.row_has_cells = False
            return
        is_header = (
            state.row_has_cells and state.row_all_header and any(raw_cells)
        ) or _looks_like_table_header(raw_cells)
        # Expand colspans, then interleave rowspan carryovers from earlier
        # rows at their recorded column positions so following rows keep
        # column alignment with the headers.
        expanded: list[tuple[str, int]] = []
        for value, colspan, rowspan in zip(
            state.row_cells,
            state.row_cell_colspans,
            state.row_cell_rowspans,
        ):
            expanded.append((value, rowspan))
            expanded.extend(
                (value if is_header else "", rowspan)
                for _ in range(colspan - 1)
            )
        merged_cells: list[str] = []
        new_pending: dict[int, list] = {}
        source_index = 0
        position = 0
        while source_index < len(expanded) or any(
            entry[0] > 0 for entry in state.pending_rowspans.values()
        ):
            carry = state.pending_rowspans.get(position)
            if carry is not None and carry[0] > 0:
                merged_cells.append(carry[1])
                carry[0] -= 1
                if carry[0] > 0:
                    new_pending[position] = carry
            elif source_index < len(expanded):
                value, rowspan = expanded[source_index]
                source_index += 1
                merged_cells.append(value)
                if rowspan > 1:
                    new_pending[position] = [rowspan - 1, value]
            else:
                break
            position += 1
            if position > 500:
                break
        state.pending_rowspans = new_pending
        cells = tuple(merged_cells)
        if not any(cells):
            state.row_cells = []
            state.row_cell_colspans = []
            state.row_cell_rowspans = []
            state.row_all_header = True
            state.row_has_cells = False
            return
        if is_header:
            state.table_headers = _merge_table_headers(
                state.table_headers,
                cells,
            )
        self._append_block(
            kind="table_row",
            text=" | ".join(cells),
            table_id=state.table_id,
            row_index=state.row_index,
            cells=cells,
            header_cells=(() if is_header else state.table_headers),
            preamble_text=state.preamble_text,
        )
        state.row_index += 1
        state.row_cells = []
        state.row_cell_colspans = []
        state.row_cell_rowspans = []
        state.row_all_header = True
        state.row_has_cells = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        state = self._table
        if self._heading_level and state is None:
            self._heading_parts.append(data)
        elif state is not None and state.cell_parts is not None:
            state.cell_parts.append(data)
        elif state is None:
            self._text_parts.append(data)

    def close(self) -> None:
        super().close()
        # Truncated documents: flush any open row/cell in every open table so
        # the trailing data row is not silently lost, then flush prose.
        while self._table_states:
            self._emit_table_row(self._table_states[-1])
            self._table_states.pop()
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
        normalize_space(html.unescape(block))
        for block in re.split(r"(?:\r?\n){2,}", document_text)
        if normalize_space(html.unescape(block))
    ]
    return SemanticDocument(
        source_document=source_document,
        blocks=tuple(
            SemanticBlock(index=index, kind="paragraph", text=text)
            for index, text in enumerate(plain_blocks)
        ),
        warning=warning,
    )
