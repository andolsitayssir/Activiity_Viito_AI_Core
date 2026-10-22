"""OnlyOffice Document Builder integration via the Web API.

Uses the DocBuilder REST API at http://onlyoffice-ds:80/docbuilder to create and
modify documents using the OnlyOffice Office JavaScript API.

Flow:
  1. Generate a .js script using the Office JavaScript API
  2. Host the script on our backend (/docbuilder/script/{id})
  3. POST { url: <script_url> } to OnlyOffice /docbuilder with JWT
  4. OnlyOffice downloads the script, executes it, downloads source files via URL
  5. Returns URLs to output files which we download and save locally

Supports: docx, xlsx, pptx (and their legacy formats).
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
import urllib.request
from typing import Any

import jwt as pyjwt
import requests as _requests

from server.execution.executors.file_storage import FileStorage, _full

_file_storage = FileStorage()

# ── Config ────────────────────────────────────────────────────────────────
DOC_SERVER_PORT = os.environ.get("ONLYOFFICE_DS_PORT", "8088")
DOC_SERVER_URL = os.environ.get("ONLYOFFICE_DS_URL", f"http://127.0.0.1:{DOC_SERVER_PORT}")
APP_INTERNAL_HOST = os.environ.get("ONLYOFFICE_APP_HOST", "host.docker.internal")
APP_INTERNAL_PORT = os.environ.get("ONLYOFFICE_APP_PORT", "8000")
# DocServer address reachable from inside the app container (the Document
# Server is published on the Docker HOST). The browser still uses the
# 127.0.0.1 DOC_SERVER_URL above to load the editor.
DOCSERVER_HOST_URL = os.environ.get(
    "ONLYOFFICE_DS_INTERNAL_URL", f"http://{APP_INTERNAL_HOST}:{DOC_SERVER_PORT}"
).rstrip("/")
# Secret shared with the Document Server. No default: it must come from the
# environment (see .env.example) so the value never lands in the repository.
JWT_SECRET = os.environ.get("ONLYOFFICE_JWT_SECRET", "")

# File type -> (Office JS document type constant, default extension)
_DOC_TYPES = {
    "word": ("word", "docx"),
    "cell": ("cell", "xlsx"),
    "slide": ("slide", "pptx"),
}


def _sign(payload: dict) -> str:
    if not JWT_SECRET:
        raise RuntimeError(
            "ONLYOFFICE_JWT_SECRET not configured (set it in .env, see .env.example)"
        )
    return pyjwt.encode(payload, JWT_SECRET, algorithm="HS256", headers={"typ": "JWT"})


def _file_url(path: str) -> str:
    """URL the Document Server can fetch the file from (via our download endpoint)."""
    from urllib.parse import quote
    return f"http://{APP_INTERNAL_HOST}:{APP_INTERNAL_PORT}/onlyoffice/download/{quote(path)}"


def _script_url(script_id: str) -> str:
    """URL to serve the generated .js script back to OnlyOffice."""
    return f"http://{APP_INTERNAL_HOST}:{APP_INTERNAL_PORT}/docbuilder/script/{script_id}"


def _detect_doc_type(file_path: str) -> str:
    """Detect 'word'/'cell'/'slide' from file extension."""
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    if ext in ("xlsx", "xls", "ods", "csv"):
        return "cell"
    if ext in ("pptx", "ppt", "odp"):
        return "slide"
    return "word"


# ── Office JS script generators ───────────────────────────────────────────

def _js_open_file(file_url: str) -> str:
    """Generate JS to open an existing file from URL."""
    escaped = file_url.replace("\\", "\\\\").replace('"', '\\"')
    return f'builder.OpenFile("{escaped}");\n'


def _js_create_file(doc_type: str) -> str:
    """Generate JS to create a new empty file."""
    fmt = _DOC_TYPES.get(doc_type, ("word", "docx"))[1]
    return f'builder.CreateFile("{fmt}");\n'


def _js_insert_text(text: str) -> str:
    """Generate JS to insert text at current cursor position."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oParagraph = oDocument.GetElement(0);\n'
        f'oParagraph.AddText("{escaped}");\n'
    )


def _js_select_all() -> str:
    """No-op kept for backward compatibility. Formatting functions now iterate all paragraphs internally."""
    return ''


def _js_set_bold(bold: bool = True) -> str:
    """Set bold on the entire document text range."""
    val = "true" if bold else "false"
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oText = oDocument.GetText();\n'
        'var oRange = oDocument.GetRange(0, oText.length);\n'
        f'oRange.SetBold({val});\n'
    )


def _js_set_italic(italic: bool = True) -> str:
    """Set italic on the entire document text range."""
    val = "true" if italic else "false"
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oText = oDocument.GetText();\n'
        'var oRange = oDocument.GetRange(0, oText.length);\n'
        f'oRange.SetItalic({val});\n'
    )


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert a hex color string (e.g. '0000FF' or '#FF0000') to (r, g, b)."""
    h = hex_color.lstrip("#").upper()
    if len(h) != 6:
        return (0, 0, 0)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (0, 0, 0)


def _js_set_font_color(hex_color: str) -> str:
    """Set font color (numeric RGB) on the entire document text range."""
    r, g, b = _hex_to_rgb(hex_color)
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oText = oDocument.GetText();\n'
        'var oRange = oDocument.GetRange(0, oText.length);\n'
        f'oRange.SetColor({r}, {g}, {b});\n'
    )


def _js_set_font_size(size: int) -> str:
    """Set font size on the entire document text range."""
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oText = oDocument.GetText();\n'
        'var oRange = oDocument.GetRange(0, oText.length);\n'
        f'oRange.SetFontSize({size});\n'
    )


def _js_add_heading(text: str, level: int = 1) -> str:
    """Insert a heading at the BEGINNING of the document."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    style_name = f"Heading {level}"
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oParagraph = Api.CreateParagraph();\n'
        f'oParagraph.AddText("{escaped}");\n'
        f'oParagraph.SetStyle(oDocument.GetStyle("{style_name}"));\n'
        'oDocument.AddElement(0, oParagraph);\n'
    )


def _js_move_cursor_start() -> str:
    return 'builder.MoveCursorToStart();\n'


def _js_move_cursor_end() -> str:
    return 'builder.MoveCursorToEnd();\n'


def _js_paragraph_break() -> str:
    return 'var oDocument = Api.GetDocument();\noDocument.CreateParagraph();\n'


def _js_delete_all() -> str:
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oParas = oDocument.GetAllParagraphs();\n'
        'for (var i = 0; i < oParas.length; i++) {\n'
        '  oParas[i].RemoveAllElements();\n'
        '}\n'
    )


def _js_replace_text(old: str, new: str) -> str:
    """Replace all occurrences of `old` with `new` (keeps paragraph structure)."""
    old_esc = old.replace("\\", "\\\\").replace('"', '\\"')
    new_esc = new.replace("\\", "\\\\").replace('"', '\\"')
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oParas = oDocument.GetAllParagraphs();\n'
        'for (var i = 0; i < oParas.length; i++) {\n'
        '  var oP = oParas[i];\n'
        '  var s = oP.GetText();\n'
        f'  if (s.indexOf("{old_esc}") >= 0) {{\n'
        f'    var t = s.split("{old_esc}").join("{new_esc}");\n'
        '    oP.RemoveAllElements();\n'
        f'    oP.AddText(t);\n'
        '  }\n'
        '}\n'
    )


def _js_delete_text(text_to_delete: str) -> str:
    """Delete every occurrence of the given text via the Search API."""
    escaped = text_to_delete.replace("\\", "\\\\").replace('"', '\\"')
    return (
        'var oDocument = Api.GetDocument();\n'
        f'var oRes = oDocument.Search("{escaped}");\n'
        'for (var i = 0; i < oRes.length; i++) {\n'
        '  oRes[i].Delete();\n'
        '}\n'
    )


def _js_delete_images() -> str:
    """Delete all images (drawing objects) from the document."""
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oImages = oDocument.GetAllImages();\n'
        'for (var i = oImages.length - 1; i >= 0; i--) {\n'
        '  oImages[i].Delete();\n'
        '}\n'
    )


def _js_add_paragraph_text(text: str) -> str:
    """Add a new paragraph with text at the end."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oParagraph = Api.CreateParagraph();\n'
        f'oParagraph.AddText("{escaped}");\n'
        'oDocument.Push(oParagraph);\n'
    )


def _js_add_content(text: str, at_beginning: bool = False) -> str:
    """Add a new paragraph with text, optionally at the beginning of the doc."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    insert = ('oDocument.AddElement(0, oParagraph);\n' if at_beginning else 'oDocument.Push(oParagraph);\n')
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oParagraph = Api.CreateParagraph();\n'
        f'oParagraph.AddText("{escaped}");\n'
        + insert
    )


def _js_delete_tables() -> str:
    """Remove all tables from the document."""
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oTables = oDocument.GetAllTables();\n'
        'for (var i = oTables.length - 1; i >= 0; i--) {\n'
        '  oTables[i].Delete();\n'
        '}\n'
    )


def _js_insert_image(image_url: str, width: int = 400, height: int = 300,
                     at_beginning: bool = False) -> str:
    """Insert an image from URL, at the beginning or end of the document."""
    escaped = image_url.replace("\\", "\\\\").replace('"', '\\"')
    insert = 'oDocument.AddElement(0, oParagraph);\n' if at_beginning else 'oDocument.Push(oParagraph);\n'
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oParagraph = Api.CreateParagraph();\n'
        f'var oDrawing = Api.CreateImage("{escaped}", {width * 9525}, {height * 9525});\n'
        'oDrawing.SetWrappingStyle("inline");\n'
        'oParagraph.AddDrawing(oDrawing);\n'
        + insert
    )


def _js_insert_table(rows: int, cols: int, headers: list[list[str]] | None = None,
                     at_beginning: bool = False) -> str:
    """Insert a table with optional header row (header cells bolded)."""
    lines = [
        'var oDocument = Api.GetDocument();\n',
        f'var oTable = Api.CreateTable({cols}, {rows});\n',
    ]
    if headers:
        for col_idx, header_text in enumerate(headers[0] if headers else []):
            escaped = header_text.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(
                f'var oCell = oTable.GetCell(0, {col_idx});\n'
                f'var oCellParagraph = oCell.GetContent().GetElement(0);\n'
                f'oCellParagraph.AddText("{escaped}");\n'
            )
    push = 'oDocument.AddElement(0, oTable);\n' if at_beginning else 'oDocument.Push(oTable);\n'
    lines.append(push)
    return '\n'.join(lines)


def _js_insert_table_from_data(headers: list[str], rows_data: list[list[str]]) -> str:
    """Insert a complete table with headers and data rows."""
    num_rows = 1 + len(rows_data)
    num_cols = len(headers)
    lines = [
        'var oDocument = Api.GetDocument();\n',
        f'var oTable = Api.CreateTable({num_cols}, {num_rows});\n',
    ]
    # Header row
    for col_idx, header_text in enumerate(headers):
        escaped = header_text.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(
            f'var oCell = oTable.GetCell(0, {col_idx});\n'
            f'var oCellParagraph = oCell.GetContent().GetElement(0);\n'
            f'oCellParagraph.AddText("{escaped}");\n'
            f'var oCellRun = oCellParagraph.GetElement(0);\n'
            f'oCellRun.SetBold(true);\n'
        )
    # Data rows
    for row_idx, row in enumerate(rows_data):
        for col_idx, cell_text in enumerate(row):
            escaped = cell_text.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(
                f'var oCell = oTable.GetCell({row_idx + 1}, {col_idx});\n'
                f'var oCellParagraph = oCell.GetContent().GetElement(0);\n'
                f'oCellParagraph.AddText("{escaped}");\n'
            )
    lines.append('oDocument.Push(oTable);\n')
    return '\n'.join(lines)


def _js_modify_table_from_data(headers: list[str], rows_data: list[list[str]],
                               rows_num: int = 0, cols_num: int = 0) -> str:
    """Update the first existing table: append rows/columns as needed, then fill cells.

    The table is preserved (never deleted). Current dimensions are discovered by
    probing GetCell(r, c) (GetRowsCount/GetCellsCount are unreliable in this
    DocBuilder build). Provided headers are written into row 0; provided data
    rows are APPENDED at the end of the table (after the last existing row) so a
    repeated "ajoute une ligne avec les données X" grows the table instead of
    overwriting earlier rows. Extra rows/columns (rows_num / cols_num) widen it.
    Untouched cells keep their existing content.
    """
    esc_h = [_js_escape(str(h)) for h in headers]
    clean_rows: list[list[str]] = []
    max_cols = len(headers)
    for row in rows_data or []:
        cells = [_js_escape(str(c)) for c in (row or [])]
        clean_rows.append(cells)
        if len(cells) > max_cols:
            max_cols = len(cells)
    rows_js = json.dumps(clean_rows)
    headers_js = json.dumps(esc_h)
    rows_num = max(0, int(rows_num or 0))
    cols_num = max(0, int(cols_num or 0))

    script = (
        'var oDocument = Api.GetDocument();\n'
        'var oTables = oDocument.GetAllTables();\n'
        'if (oTables.length > 0) {\n'
        '  var oTable = oTables[0];\n'
        # Discover current dimensions by probing GetCell (null/throws = past end).
        '  var nRows = 0;\n'
        '  for (var r = 0; r < 200; r++) {\n'
        '    var ok = true;\n'
        '    try { if (oTable.GetCell(r, 0) == null) ok = false; } catch (e) { ok = false; }\n'
        '    if (ok) nRows = r + 1; else break;\n'
        '  }\n'
        '  var nCols = 0;\n'
        '  for (var c = 0; c < 200; c++) {\n'
        '    var ok = true;\n'
        '    try { if (oTable.GetCell(0, c) == null) ok = false; } catch (e) { ok = false; }\n'
        '    if (ok) nCols = c + 1; else break;\n'
        '  }\n'
        # Target dimensions: append the data rows at the end + any explicit additions.
        f'  var headers = {headers_js} || [];\n'
        f'  var rows = {rows_js} || [];\n'
        f'  var addRows = {rows_num};\n'
        f'  var addCols = {cols_num};\n'
        '  var needCols = headers.length;\n'
        '  for (var i = 0; i < rows.length; i++) { if (rows[i].length > needCols) needCols = rows[i].length; }\n'
        '  var targetRows = nRows + (rows.length > addRows ? rows.length : addRows);\n'
        '  var targetCols = Math.max(needCols, nCols + addCols);\n'
        '  var startRow = nRows;\n'
        # Add extra rows (each AddRow(nCols) adds ONE row with nCols cells).
        '  while (nRows < targetRows) { try { oTable.AddRow(nCols); } catch (e) {} nRows++; }\n'
        # Add extra columns (each AddColumn(1) adds ONE empty column).
        '  while (nCols < targetCols) { try { oTable.AddColumn(1); } catch (e) {} nCols++; }\n'
        # Fill headers into row 0 (overwrite).
        '  for (var ci = 0; ci < headers.length; ci++) {\n'
        '    try { var c = oTable.GetCell(0, ci); c.GetContent().RemoveAllElements(); c.GetContent().GetElement(0).AddText(headers[ci]); } catch (e) {}\n'
        '  }\n'
        # Append the data rows at the end of the table (startRow .. startRow+len-1).
        '  for (var ri = 0; ri < rows.length; ri++) {\n'
        '    var r = startRow + ri;\n'
        '    for (var cj = 0; cj < rows[ri].length; cj++) {\n'
        '      try { var c = oTable.GetCell(r, cj); c.GetContent().RemoveAllElements(); c.GetContent().GetElement(0).AddText(rows[ri][cj]); } catch (e) {}\n'
        '    }\n'
        '  }\n'
        '}\n'
    )
    return script


def _js_escape(value: str) -> str:
    """Escape a string for embedding inside double-quoted JS string literals."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _js_col_letter(col: int) -> str:
    """1-based column index -> spreadsheet column letters (1=A, 27=AA)."""
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _js_cell_helpers() -> str:
    """Shared JS helpers for spreadsheet edits (stable A1 addressing)."""
    return (
        'function OCColLetter(c) {\n'
        '  var s = "";\n'
        '  while (c > 0) { var m = (c - 1) % 26; s = String.fromCharCode(65 + m) + s; c = Math.floor((c - 1) / 26); }\n'
        '  return s;\n'
        '}\n'
        'function OCVal(v) {\n'
        '  if (typeof v === "string" && v.trim() !== "" && isFinite(Number(v))) return Number(v);\n'
        '  return v;\n'
        '}\n'
        'function OCLastRow(oSheet, maxRows) {\n'
        '  var last = 0;\n'
        '  for (var r = 1; r <= maxRows; r++) {\n'
        '    var t = "";\n'
        '    try { t = oSheet.GetRange("A" + r).GetText(); } catch (e) { t = ""; }\n'
        '    if (String(t).length > 0) last = r; else break;\n'
        '  }\n'
        '  return last;\n'
        '}\n'
    )


def _js_cell_set_headers(headers: list[str]) -> str:
    """Overwrite the first row of a spreadsheet with the given headers."""
    return _js_cell_helpers() + (
        f'var headers = {json.dumps([_js_escape(str(h)) for h in headers])} || [];\n'
        'var oSheet = Api.GetActiveSheet();\n'
        'for (var ci = 0; ci < headers.length; ci++) {\n'
        '  try { oSheet.GetRange(OCColLetter(ci + 1) + "1").SetValue(headers[ci]); } catch (e) {}\n'
        '}\n'
    )


def _js_cell_append_rows(rows_data: list[list[str]], rows_num: int = 0, cols_num: int = 0) -> str:
    """Append a 2D block of rows right after the used area of a spreadsheet.

    Rows are discovered by scanning column A (empty cell = end) since the
    builder cell API has no GetUsedRange/GetRowIndex helpers. Numeric-looking
    strings are stored as numbers; extra requested empty rows are materialised.
    """
    clean: list[list[str]] = []
    max_cells = 0
    for row in rows_data or []:
        cells = [_js_escape(str(c)) for c in (row or [])]
        clean.append(cells)
        max_cells = max(max_cells, len(cells))
    extra_cols = max(1, int(cols_num or 0))
    target_cols = max(extra_cols, max_cells)
    return _js_cell_helpers() + (
        f'var rows = {json.dumps(clean)} || [];\n'
        f'var addRows = {max(0, int(rows_num or 0))};\n'
        f'var targetCols = {target_cols};\n'
        'var oSheet = Api.GetActiveSheet();\n'
        'var startRow = OCLastRow(oSheet, 1000);\n'
        'for (var ri = 0; ri < rows.length; ri++) {\n'
        '  var r = startRow + ri + 1;\n'
        '  for (var cj = 0; cj < rows[ri].length; cj++) {\n'
        '    try { oSheet.GetRange(OCColLetter(cj + 1) + r).SetValue(OCVal(rows[ri][cj])); } catch (e) {}\n'
        '  }\n'
        '}\n'
        'for (var ei = 0; ei < addRows; ei++) {\n'
        '  var er = startRow + rows.length + ei + 1;\n'
        '  for (var cj = 0; cj < targetCols; cj++) {\n'
        '    try { oSheet.GetRange(OCColLetter(cj + 1) + er).SetValue(""); } catch (e) {}\n'
        '  }\n'
        '}\n'
    )


def _js_cell_insert_from_data(headers: list[str], rows_data: list[list[str]]) -> str:
    """Write a complete spreadsheet starting at A1 (headers in row 1)."""
    lines = [_js_cell_helpers(), 'var oSheet = Api.GetActiveSheet();\n']
    for ci, header in enumerate(headers, 1):
        lines.append(f'oSheet.GetRange("{_js_col_letter(ci)}1").SetValue("{_js_escape(str(header))}");\n')
    for ri, row in enumerate(rows_data or [], 2):
        for cj, value in enumerate(row, 1):
            esc = _js_escape(str(value))
            lines.append(f'oSheet.GetRange("{_js_col_letter(cj)}{ri}").SetValue(OCVal({json.dumps(esc)}));\n')
    return "".join(lines)


def _js_slide_add_textbox(text: str) -> str:
    """Add a text box with the given text to the bottom of the first slide."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    return (
        'var oPresentation = Api.GetPresentation();\n'
        'var oSlide = oPresentation.GetSlideByIndex(0);\n'
        'var oShape = Api.CreateShape("rect", 11000000, 1200000, 500000, 5400000);\n'
        'var oDocContent = oShape.GetDocContent();\n'
        'oDocContent.RemoveAllElements();\n'
        'var oParagraph = oDocContent.GetElement(0);\n'
        f'oParagraph.AddText("{escaped}");\n'
        'oSlide.AddObject(oShape);\n'
    )


def _js_slide_text_rendition(headers: list[str], rows_data: list[list[str]]) -> str:
    """Render a table spec as plain lines for a slide text box."""
    lines: list[str] = []
    if headers:
        lines.append(" | ".join(str(h) for h in headers))
    for row in rows_data or []:
        lines.append(" | ".join(str(c) for c in (row or [])))
    return "\n".join(lines)


def _js_insert_chart(chart_type: str, data: list[list[str]] | None = None) -> str:
    """Insert a chart. chart_type: bar, line, pie, scatter."""
    chart_map = {
        "bar": 0, "column": 0, "histogramme": 0,
        "line": 1, "courbe": 1,
        "pie": 2, "camembert": 2,
        "scatter": 3, "nuage": 3,
    }
    ct = chart_map.get(chart_type.lower(), 0)
    
    # Default chart data if none provided
    if not data:
        data = [
            ["", "Serie 1", "Serie 2"],
            ["Jan", "10", "20"],
            ["Fev", "15", "18"],
            ["Mar", "12", "25"],
            ["Avr", "20", "15"],
        ]
    
    # Build the chart creation script
    lines = [
        'var oDocument = Api.GetDocument();\n',
        'var oParagraph = Api.CreateParagraph();\n',
    ]
    
    # Create chart data
    lines.append('var oData = [\n')
    for row in data:
        escaped_row = [str(cell).replace('"', '\\"') for cell in row]
        cells = ", ".join('"' + c + '"' for c in escaped_row)
        lines.append(f'  [{cells}],\n')
    lines.append('];\n')
    
    lines.append(f'var oChart = Api.CreateChart(oData, {ct});\n')
    lines.append('oChart.SetSize(5486400, 3200400);\n')
    lines.append('oParagraph.AddChart(oChart);\n')
    lines.append('oDocument.Push(oParagraph);\n')
    return '\n'.join(lines)


def _js_insert_page_break() -> str:
    """Insert a page break."""
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oParagraph = Api.CreateParagraph();\n'
        'oParagraph.AddPageBreak();\n'
        'oDocument.Push(oParagraph);\n'
    )


def _js_add_list_item(text: str, numbered: bool = False) -> str:
    """Add a bullet or numbered list item."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    list_type = "numbered" if numbered else "bulleted"
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oParagraph = Api.CreateParagraph();\n'
        f'oParagraph.AddText("{escaped}");\n'
        f'oParagraph.SetStyle(oDocument.GetStyle("{list_type}"));\n'
        'oDocument.Push(oParagraph);\n'
    )


def _js_set_paragraph_alignment(alignment: str) -> str:
    """Set paragraph alignment on ALL paragraphs: left, center, right, justify."""
    align_map = {
        "left": "left", "gauche": "left",
        "center": "center", "centre": "center",
        "right": "right", "droite": "right",
        "justify": "both", "justifie": "both", "justifié": "both",
    }
    align = align_map.get(alignment.lower(), "left")
    return (
        'var oDocument = Api.GetDocument();\n'
        'var oParas = oDocument.GetAllParagraphs();\n'
        'for (var i = 0; i < oParas.length; i++) {\n'
        f'  oParas[i].SetJc("{align}");\n'
        '}\n'
    )


def _js_add_header(text: str) -> str:
    """Insert text at the very top of the document (beginning)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return (
        'var oDocument = Api.GetDocument();\n'
        f'var oParagraph = Api.CreateParagraph();\n'
        f'oParagraph.AddText("{escaped}");\n'
        'oDocument.AddElement(0, oParagraph);\n'
    )


def _js_add_footer(text: str) -> str:
    """Add text to the document footer using a simpler approach."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return (
        'var oDocument = Api.GetDocument();\n'
        f'var oParagraph = Api.CreateParagraph();\n'
        f'oParagraph.AddText("{escaped}");\n'
        'oDocument.Push(oParagraph);\n'
    )


def _js_save_file(doc_type: str, output_name: str) -> str:
    """Generate JS to save the file with a specific name."""
    fmt = _DOC_TYPES.get(doc_type, ("word", "docx"))[1]
    return f'builder.SaveFile("{fmt}", "{output_name}");\n'


def _js_save() -> str:
    return 'builder.Save();\n'


def _js_close() -> str:
    return 'builder.CloseFile();\n'


# ── Natural language -> JS script ─────────────────────────────────────────

def build_edit_script(file_url: str, doc_type: str, request: str, output_name: str,
                      table: dict | None = None, modify: dict | None = None,
                      insert: dict | None = None) -> str:
    """Convert a natural-language edit request into an OnlyOffice JS script.
    
    Args:
        file_url: Absolute URL to the source file (fetchable by OnlyOffice)
        doc_type: 'word', 'cell', or 'slide'
        request: Natural language edit instruction
        output_name: Output filename for builder.SaveFile()
        table: Optional structured table spec {"headers": [...], "rows_data": [...]}
            to insert a NEW populated table.
        modify: Optional structured table spec {"headers": [...], "rows_data": [...]}
            to update the EXISTING table's provided cells in place.
        insert: Optional structured text insert {"text": str, "at_beginning": bool}
            to append/prepend raw text verbatim (no NL keyword scanning).
        
    Returns:
        Complete .js script content
    """
    req = request.lower().strip()

    # ── Spreadsheet (cell) edits ──────────────────────────────────────────
    if doc_type == "cell":
        body: list[str] = []
        if modify is not None:
            if modify.get("headers"):
                body.append(_js_cell_set_headers(modify.get("headers") or []))
            body.append(_js_cell_append_rows(
                modify.get("rows_data") or [],
                rows_num=modify.get("rows_num") or 0,
                cols_num=modify.get("cols_num") or 0,
            ))
        elif table is not None:
            body.append(_js_cell_insert_from_data(table.get("headers") or [], table.get("rows_data") or []))
        elif insert is not None and (insert.get("text") or "").strip():
            items = [(insert.get("text") or "").strip()]
            body.append(_js_cell_append_rows([items]))
        else:
            text = _extract_text_to_insert(request)
            text = re.sub(
                r"^(?:ajout(?:er|e|ons)?\s+)?(?:une\s+)?nouvelle\s+ligne\b|"
                r"^(?:ajout(?:er|e|ons)?\s+)?(?:une\s+)?ligne\b",
                "", text, flags=re.IGNORECASE,
            ).strip()
            items = [t.strip() for t in re.split(r"[,\n;]", text) if t.strip()]
            if items:
                body.append(_js_cell_append_rows([items]))
        if body:
            lines = [_js_open_file(file_url)] + body
            lines += [_js_save_file(doc_type, output_name), _js_close()]
            return "\n".join(lines)

    # ── Presentation (slide) edits ────────────────────────────────────────
    if doc_type == "slide":
        body = []
        if modify is not None:
            body.append(_js_slide_add_textbox(_js_slide_text_rendition(
                modify.get("headers") or [], modify.get("rows_data") or [])))
        elif table is not None:
            body.append(_js_slide_add_textbox(_js_slide_text_rendition(
                table.get("headers") or [], table.get("rows_data") or [])))
        elif insert is not None and (insert.get("text") or "").strip():
            body.append(_js_slide_add_textbox(insert.get("text") or ""))
        else:
            text = _extract_text_to_insert(request)
            if text:
                body.append(_js_slide_add_textbox(text))
        if body:
            lines = [_js_open_file(file_url)] + body
            lines += [_js_save_file(doc_type, output_name), _js_close()]
            return "\n".join(lines)

    lines = [_js_open_file(file_url)]

    # Structured in-place table update (modify_table): keep the existing table,
    # grow rows/columns as needed, rewrite only the cells the user provided.
    if modify is not None:
        lines.append(_js_modify_table_from_data(
            modify.get("headers") or [],
            modify.get("rows_data") or [],
            rows_num=modify.get("rows_num") or 0,
            cols_num=modify.get("cols_num") or 0,
        ))
        return _finish_script(lines, doc_type, output_name)

    # Structured table (headers + data) overrides NL-parsed table insertion.
    if table is not None:
        if request and _is_table_removal(request):
            lines.append(_js_delete_tables())
        lines.append(_js_insert_table_from_data(table.get("headers") or [], table.get("rows_data") or []))
        return _finish_script(lines, doc_type, output_name)

    # Structured verbatim text insertion (multi-domain handoff: summary, answer, ...).
    if insert is not None:
        text = (insert.get("text") or "").strip()
        if text:
            lines.append(_js_add_content(text, at_beginning=bool(insert.get("at_beginning"))))
        return _finish_script(lines, doc_type, output_name)

    # ── Remove images (photo/image deletion, e.g. "supprime la photo du document") ──
    # Only treat as image removal when a photo/image is the direct object (not quoted text).
    media_delete = re.search(
        r'(?:supprime|supprimer|delete|remove|effacer|vider)\s+'
        r'(?:la\s+|l\'\s*|l’|le\s+|une\s+|des\s+|cette\s+|toutes\s+|tous\s+|les\s+)?'
        r'(?:photo|image|picture|logo)s?',
        req,
    )
    if media_delete and not _extract_text_to_delete(request):
        lines.append(_js_delete_images())

    # ── Remove table ──
    if re.search(r'(?:supprime|supprimer|delete|remove|effacer|vider)\s+(?:le\s+|la\s+|une\s+|des\s+|toutes\s+|tous\s+|les\s+)?(?:tableau|table|grille)s?', req):
        lines.append(_js_delete_tables())

    # ── Modify table → remove existing tables and insert a replacement ──
    elif any(kw in req for kw in ["modifie le tableau", "modifie la grille", "modify the table",
                                   "change le tableau", "modifie les colonnes du tableau",
                                   "modifie les lignes du tableau"]):
        lines.append(_js_delete_tables())
        table_info = _extract_table_info(request)
        if table_info:
            rows, cols = table_info
            lines.append(_js_insert_table(rows, cols))

    # ── Title / Heading ──
    elif any(kw in req for kw in ["titre", "title", "heading", "en-tête"]):
        title = _extract_quoted_or_after(request, ["titre", "title", "heading", "en-tête"]) or "Titre"
        level = 1
        if any(kw in req for kw in ["sous-titre", "subtitle", "h2", "secondaire"]):
            level = 2
        lines.append(_js_add_heading(title, level))

    # ── Bold ──
    elif any(kw in req for kw in ["gras", "bold", "en gras", "mettre en gras"]):
        lines.append(_js_select_all())
        lines.append(_js_set_bold())

    # ── Italic ──
    elif any(kw in req for kw in ["italique", "italic", "en italique"]):
        lines.append(_js_select_all())
        lines.append(_js_set_italic())

    # ── Color ──
    elif any(kw in req for kw in ["couleur", "color", "bleu", "blue", "rouge", "red",
                                    "vert", "green", "noir", "black", "jaune", "yellow",
                                    "orange", "violet", "purple"]):
        color = _extract_color(request)
        lines.append(_js_select_all())
        lines.append(_js_set_font_color(color))

    # ── Font size ──
    elif any(kw in req for kw in ["taille", "size", "police", "font", "grand", "petit"]):
        size = _extract_number(request) or 14
        lines.append(_js_select_all())
        lines.append(_js_set_font_size(size))

    # ── Delete ──
    elif any(kw in req for kw in ["supprime", "supprimer", "delete", "remove", "effacer", "vider"]):
        # Check if specific text to delete is mentioned
        text_to_delete = _extract_text_to_delete(request)
        if text_to_delete:
            lines.append(_js_delete_text(text_to_delete))
        else:
            lines.append(_js_delete_all())

    # ── Search and replace ──
    elif any(kw in req for kw in ["remplace", "remplacer", "replace", "change", "modifie", "transforme"]):
        old, new = _extract_replace_args(request)
        if old:
            lines.append(_js_replace_text(old, new or ""))

    # ── Image insertion ──
    elif any(kw in req for kw in ["image", "photo", "logo", "image", "photo", "logo",
                                    "insère une image", "insert image", "ajoute une image"]):
        image_url = _extract_url(request)
        if image_url:
            at_beginning = any(kw in req for kw in ["au debut", "at the beginning", "en debut", "tout en haut", "le debut du document"])
            lines.append(_js_insert_image(image_url, at_beginning=at_beginning))
        else:
            text = _extract_text_to_insert(request)
            if text:
                lines.append(_js_add_paragraph_text(text))

    # ── Table insertion ──
    elif any(kw in req for kw in ["tableau", "table", "grille", "tableau"]):
        table_info = _extract_table_info(request)
        if table_info:
            rows, cols = table_info
            lines.append(_js_insert_table(rows, cols))
        else:
            text = _extract_text_to_insert(request)
            if text:
                lines.append(_js_add_paragraph_text(text))

    # ── Header ──
    elif any(kw in req for kw in ["en-tête", "header", "tête de page"]):
        text = _extract_text_to_insert(request)
        if text:
            lines.append(_js_add_header(text))

    # ── Footer ──
    elif any(kw in req for kw in ["pied de page", "footer", "bas de page"]):
        text = _extract_text_to_insert(request)
        if text:
            lines.append(_js_add_footer(text))

    # ── List item ──
    elif any(kw in req for kw in ["liste", "list", "puce", "bullet", "numéro", "numbered"]):
        text = _extract_text_to_insert(request)
        numbered = any(kw in req for kw in ["numéro", "numbered", "numéroté"])
        if text:
            items = [item.strip() for item in re.split(r"[,;]|\bet\b", text) if item.strip()]
            for item in items:
                lines.append(_js_add_list_item(item, numbered))

    # ── Page break ──
    elif any(kw in req for kw in ["saut de page", "page break", "nouvelle page", "new page"]):
        lines.append(_js_insert_page_break())

    # ── Alignment ──
    elif any(kw in req for kw in ["aligne", "align", "centré", "center", "centre", "centrer",
                                   "justifié", "justify", "droite à gauche"]):
        alignment = "center" if any(kw in req for kw in ["centré", "center", "centre", "centrer"]) else "justify"
        lines.append(_js_set_paragraph_alignment(alignment))

    # ── Add text / content ──
    elif any(kw in req for kw in ["ajoute", "add", "insère", "insert", "écris", "write",
                                    "met", "put", "rajoute", "complète", "complete"]):
        text = _extract_text_to_insert(request)
        if text:
            at_beginning = any(kw in req for kw in ["au debut", "at the beginning", "en debut", "le debut du document", "tout en haut"])
            lines.append(_js_add_content(text, at_beginning=at_beginning))

    # ── Generic fallback ──
    else:
        text = _extract_text_to_insert(request)
        if text:
            lines.append(_js_add_paragraph_text(text))

    lines.append(_js_save_file(doc_type, output_name))
    lines.append(_js_close())
    return "\n".join(lines)


def _finish_script(lines: list[str], doc_type: str, output_name: str) -> str:
    """Append save+close and join the JS lines into a complete script."""
    lines.append(_js_save_file(doc_type, output_name))
    lines.append(_js_close())
    return "\n".join(lines)


# ── Text extraction helpers ───────────────────────────────────────────────

def _extract_quoted_or_after(text: str, keywords: list[str]) -> str | None:
    """Extract quoted text or text after a keyword, stopping at formatting keywords."""
    # Formatting instruction keywords that should NOT be part of extracted content
    _STOP_WORDS = [
        "en gras", "en italique", "en souligné", "en bleu", "en rouge", "en vert",
        "en noir", "en blanc", "en jaune", "en orange", "en violet", "en rose",
        "en gras", "bold", "italic", "underlined",
        "au debut", "à la fin", "at the beginning", "at the end",
        "en gras", "en italique", "en gros", "en petit",
    ]
    match = re.search(r'["\u201c\u201d\'\u2018\u2019]([^"\u201c\u201d\'\u2018\u2019]+)["\u201c\u201d\'\u2018\u2019]', text)
    if match:
        return match.group(1)
    for kw in keywords:
        match = re.search(rf'(?:{kw})\s+(?:de\s+|of\s+|"?)([^"\']+)["\']?', text, re.IGNORECASE)
        if match:
            result = match.group(1).strip()
            # Strip formatting keywords from the result
            lower = result.lower()
            for stop in _STOP_WORDS:
                idx = lower.find(stop)
                if idx >= 0:
                    result = result[:idx].strip().rstrip(" ,;:")
                    break
            return result if result else None
    return None


def _extract_color(text: str) -> str:
    """Extract hex color from natural language."""
    color_map = {
        "rouge": "FF0000", "red": "FF0000",
        "bleu": "0000FF", "blue": "0000FF",
        "vert": "00FF00", "green": "00FF00",
        "noir": "000000", "black": "000000",
        "blanc": "FFFFFF", "white": "FFFFFF",
        "jaune": "FFFF00", "yellow": "FFFF00",
        "orange": "FFA500",
        "violet": "800080", "purple": "800080",
        "rose": "FFC0CB", "pink": "FFC0CB",
        "gris": "808080", "gray": "808080",
    }
    text_lower = text.lower()
    for name, hex_val in color_map.items():
        if name in text_lower:
            return hex_val
    return "000000"


def _extract_number(text: str) -> int | None:
    match = re.search(r'(\d+)', text)
    return int(match.group(1)) if match else None


def _extract_replace_args(text: str) -> tuple[str, str]:
    """Extract old/new text from replace instruction."""
    patterns = [
        r'(?:remplacer|replace|change|modifie)\s+["\'](.+?)["\']\s+(?:par|by|en|avec|with)\s+["\'](.+?)["\']',
        r'["\'](.+?)["\']\s+(?:par|by|en|avec|with)\s+["\'](.+?)["\']',
    ]
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            return match.group(1), match.group(2)
    return "", ""


def _extract_text_to_insert(text: str) -> str:
    """Extract the text content to insert."""
    match = re.search(r'["\u201c\u201d\'\u2018\u2019]([^"\u201c\u201d\'\u2018\u2019]+)["\u201c\u201d\'\u2018\u2019]', text)
    if match:
        return match.group(1)
    match = re.search(
        r'(?:ajoute|add|insère|insert|écris|write|met|put|rajoute|complète|complete)\s+(.+)',
        text, re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    return text.strip()


def _extract_text_to_delete(text: str) -> str | None:
    """Extract text to delete from request."""
    # Pattern: supprime "text" or delete "text"
    patterns = [
        r'(?:supprime|supprimer|delete|remove|effacer|vider)\s+["\u201c\u201d\'\u2018\u2019]([^"\u201c\u201d\'\u2018\u2019]+)["\u201c\u201d\'\u2018\u2019]',
        r'(?:supprime|supprimer|delete|remove|effacer|vider)\s+(?:le\s+)?(?:texte\s+)?["\u201c\u201d\'\u2018\u2019]([^"\u201c\u201d\'\u2018\u2019]+)["\u201c\u201d\'\u2018\u2019]',
    ]
    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            return match.group(1)
    # Unquoted after "le mot", "le texte", "la phrase", "le paragraphe"
    # e.g. "supprimer le mot bonjour", "supprime le texte 'bonjour'"
    keyed = re.search(
        r'(?:supprime|supprimer|delete|remove|effacer|vider)\s+'
        r'(?:le\s+|la\s+)?(?:mot|texte|phrase|paragraphe)\s+'
        r'["\u201c\u201d\'\u2018\u2019]?([A-Za-zÀ-ÿ0-9_\-‘’\'’\u2018\u2019]+(?:[\s-][A-Za-zÀ-ÿ0-9_\-]+)*)["\u201c\u201d\'\u2018\u2019]?',
        text,
        re.IGNORECASE,
    )
    if keyed:
        value = keyed.group(1).strip().rstrip('.')
        if value:
            return value
    return None



def _extract_url(text: str) -> str | None:
    """Extract a URL from text."""
    match = re.search(r'(https?://[^\s"\'<>]+)', text, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _is_table_removal(text: str | None) -> bool:
    """Return True when the request asks to remove/replace existing tables."""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in [
        "supprime le tableau", "supprimer le tableau", "remove the table",
        "efface le tableau", "vider le tableau", "modifie le tableau",
        "modifier le tableau", "modify the table", "change le tableau",
    ])


def _extract_table_info(text: str) -> tuple[int, int] | None:
    """Extract table dimensions (rows, cols) from text.

    Looks for patterns like:
    - "tableau 3x4" or "table 3x4"
    - "tableau 3 colonnes 4 lignes"
    - "tableau de 3 lignes et 4 colonnes"
    - "table 3 rows 4 columns"
    """
    # Pattern: NxN or NxM
    match = re.search(r'(\d+)\s*[xX×]\s*(\d+)', text)
    if match:
        rows, cols = int(match.group(1)), int(match.group(2))
        if 1 <= rows <= 50 and 1 <= cols <= 50:
            return rows, cols

    # Pattern: N lignes et M colonnes (or reversed)
    match = re.search(r'(\d+)\s*(?:lignes?|rows?)\s*(?:et|and|&)\s*(\d+)\s*(?:colonnes?|columns?)', text, re.IGNORECASE)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r'(\d+)\s*(?:colonnes?|columns?)\s*(?:et|and|&)\s*(\d+)\s*(?:lignes?|rows?)', text, re.IGNORECASE)
    if match:
        return int(match.group(2)), int(match.group(1))

    # Pattern: just a single number (NxN table)
    match = re.search(r'(?:tableau|table)\s*(?:de\s*)?(\d+)', text, re.IGNORECASE)
    if match:
        n = int(match.group(1))
        if 1 <= n <= 50:
            return n, n

    return None


# ── Main API: edit_document ───────────────────────────────────────────────

def edit_document(file_path: str, request: str, table: dict | None = None,
                  modify: dict | None = None, insert: dict | None = None) -> dict[str, Any]:
    """Edit a document via the OnlyOffice DocBuilder Web API.
    
    Args:
        file_path: Relative path under memory/files/
        request: Natural language edit instruction
        table: Optional structured table spec {"headers": [...], "rows_data": [...]}
            to insert a NEW populated table.
        modify: Optional structured table spec {"headers": [...], "rows_data": [...]}
            to update the EXISTING table's provided cells in place.
        insert: Optional structured text insert {"text": str, "at_beginning": bool}
        
    Returns:
        dict with success, message, modified, output
    """
    abs_path = _full(file_path)
    if not abs_path.exists():
        return {"success": False, "message": f"Fichier non trouvé: {file_path}", "modified": False}

    doc_type = _detect_doc_type(file_path)
    _, default_ext = _DOC_TYPES.get(doc_type, ("word", "docx"))
    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else default_ext

    # Register the script via HTTP so it's in the running server's dict
    script_id = f"viito_{uuid.uuid4().hex[:12]}"
    file_url = _file_url(file_path)
    output_name = f"output_{int(time.time())}.{ext}"
    js_code = build_edit_script(file_url, doc_type, request, output_name, table=table, modify=modify, insert=insert)

    print(f"[DOCBUILDER] Script generated ({len(js_code)} chars)", flush=True)
    print(f"[DOCBUILDER] File URL: {file_url}", flush=True)
    print(f"[DOCBUILDER] Doc type: {doc_type}, Output: {output_name}", flush=True)

    # Register via HTTP POST to the running server
    try:
        reg_resp = _requests.post(
            f"http://127.0.0.1:{APP_INTERNAL_PORT}/docbuilder/script",
            json={"script_id": script_id, "js_code": js_code},
            timeout=5,
        )
        reg_resp.raise_for_status()
        hosted_url = reg_resp.json().get("url", "")
    except Exception as exc:
        print(f"[DOCBUILDER] Script registration failed: {exc}", flush=True)
        return {"success": False, "message": f"Script registration failed: {exc}", "modified": False}

    print(f"[DOCBUILDER] Script URL: {hosted_url}", flush=True)

    # Call the DocBuilder Web API with JWT
    payload = {
        "async": False,
        "url": hosted_url,
    }
    token = _sign({"url": hosted_url})
    payload["token"] = token

    try:
        resp = _requests.post(
            f"{DOCSERVER_HOST_URL}/docbuilder",
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"[DOCBUILDER] API response: {json.dumps(result)[:500]}", flush=True)
    except Exception as exc:
        print(f"[DOCBUILDER] API error: {exc}", flush=True)
        return {"success": False, "message": f"DocBuilder API error: {exc}", "modified": False}

    # Check for errors
    error = result.get("error")
    if error is not None and error != 0:
        error_messages = {
            -1: "Unknown error",
            -2: "Generation timeout",
            -3: "Document generation error",
            -4: "Error downloading file",
            -6: "Database access error",
            -8: "Invalid token",
        }
        msg = error_messages.get(error, f"Error {error}")
        print(f"[DOCBUILDER] Error: {msg}", flush=True)
        return {"success": False, "message": f"DocBuilder: {msg}", "modified": False}

    # Download the output file(s)
    urls = result.get("urls", {})
    if not urls:
        print("[DOCBUILDER] No output URLs returned", flush=True)
        return {"success": False, "message": "DocBuilder: no output files", "modified": False}

    # Get the first output URL
    output_url = next(iter(urls.values()))
    print(f"[DOCBUILDER] Downloading output from: {output_url}", flush=True)

    try:
        with urllib.request.urlopen(output_url, timeout=30) as resp:
            new_content = resp.read()
    except Exception as exc:
        print(f"[DOCBUILDER] Download error: {exc}", flush=True)
        return {"success": False, "message": f"DocBuilder: download error: {exc}", "modified": False}

    # Save back to local filesystem
    _file_storage.save_file(file_path, new_content)
    print(f"[DOCBUILDER] Saved {len(new_content)} bytes to {file_path}", flush=True)

    return {
        "success": True,
        "message": f"Document modifié via Document Builder: {request}",
        "modified": True,
        "output_size": len(new_content),
    }


def create_document(doc_type: str, filename: str, content_text: str = "", folder: str | None = None) -> dict[str, Any]:
    """Create a new document via the DocBuilder Web API.

    Args:
        doc_type: 'word', 'cell', or 'slide'
        filename: Output filename (e.g., 'report.docx')
        content_text: Optional initial text content
        folder: Optional destination folder (relative path under memory/files/)

    Returns:
        dict with success, file_path, content
    """
    _, default_ext = _DOC_TYPES.get(doc_type, ("word", "docx"))
    if "." not in filename:
        filename = f"{filename}.{default_ext}"

    script_id = f"viito_create_{uuid.uuid4().hex[:12]}"
    output_name = filename
    file_path = f"{folder.strip('/').strip('\\')}/{filename}" if folder else filename

    lines = [_js_create_file(doc_type)]
    if content_text:
        lines.append(_js_insert_text(content_text))
    lines.append(_js_save_file(doc_type, output_name))
    lines.append(_js_close())
    js_code = "\n".join(lines)

    try:
        reg_resp = _requests.post(
            f"http://127.0.0.1:{APP_INTERNAL_PORT}/docbuilder/script",
            json={"script_id": script_id, "js_code": js_code},
            timeout=5,
        )
        reg_resp.raise_for_status()
        hosted_url = reg_resp.json().get("url", "")
    except Exception as exc:
        return {"success": False, "message": f"Script registration failed: {exc}"}

    payload = {"async": False, "url": hosted_url, "token": _sign({"url": hosted_url})}

    try:
        resp = _requests.post(f"{DOCSERVER_HOST_URL}/docbuilder", json=payload, timeout=60)
        resp.raise_for_status()
        result = resp.json()
    except Exception as exc:
        return {"success": False, "message": f"DocBuilder API error: {exc}"}

    error = result.get("error")
    if error is not None and error != 0:
        return {"success": False, "message": f"DocBuilder error {error}"}

    urls = result.get("urls", {})
    if not urls:
        return {"success": False, "message": "No output files"}

    output_url = next(iter(urls.values()))
    with urllib.request.urlopen(output_url, timeout=30) as resp:
        content = resp.read()

    # Save to memory/files/
    _file_storage.save_file(file_path, content)

    return {"success": True, "file_path": file_path, "content": content.decode("utf-8", errors="replace")}
