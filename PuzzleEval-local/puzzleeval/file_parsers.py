# ============================================================================
# File Parsers — Extract content from uploaded workflow files
# ============================================================================
# Users can optionally upload their current workflow as a file.
# This module handles converting those files into content that Claude can read.
#
# KEY INSIGHT: Claude can NATIVELY read some file types:
#   - PDFs → sent as "document" content blocks (Claude's vision reads each page)
#   - Images (PNG, JPG, GIF, WEBP) → sent as "image" content blocks
#
# For these, we DON'T extract text ourselves — we send the raw file to Claude
# and let it do the reading. This is better because Claude can see charts,
# tables, and layouts that text extraction would miss.
#
# For file types Claude CAN'T natively read:
#   - DOCX → we extract text using python-docx library
#   - CSV → we read using Python's built-in csv module
#   - TXT → we read the raw text
#
# Each parser is a PURE FUNCTION: takes a file path, returns content.
# No side effects, no state, easy to test.
# ============================================================================

import base64
import csv
import io
import os
from typing import Any

from puzzleeval.exceptions import AgentFileParseError


# ---------------------------------------------------------------------------
# File extension to media type mapping
# ---------------------------------------------------------------------------
# Used to tell Claude what type of file we're sending.
# ---------------------------------------------------------------------------
IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def parse_pdf(file_path: str) -> dict[str, Any]:
    """
    Prepare a PDF for Claude's native document reading.

    Instead of extracting text ourselves (which loses formatting, charts, tables),
    we encode the PDF as base64 and send it as a "document" content block.
    Claude's vision model reads each page as an image + extracts the text.

    Args:
        file_path: Absolute path to the PDF file.

    Returns:
        A dict in the Anthropic API's "document" content block format:
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": "<base64-encoded-pdf>"
            }
        }
    """
    try:
        with open(file_path, "rb") as f:
            pdf_data = base64.standard_b64encode(f.read()).decode("utf-8")

        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": pdf_data,
            },
        }
    except FileNotFoundError:
        raise AgentFileParseError(f"PDF file not found: {file_path}")
    except Exception as e:
        raise AgentFileParseError(f"Failed to read PDF: {e}")


def parse_image(file_path: str) -> dict[str, Any]:
    """
    Prepare an image for Claude's native vision reading.

    Similar to PDFs, we send images directly to Claude as base64-encoded
    content blocks. Claude's vision model interprets the image.

    Args:
        file_path: Absolute path to the image file (PNG, JPG, GIF, or WEBP).

    Returns:
        A dict in the Anthropic API's "image" content block format:
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "<base64-encoded-image>"
            }
        }
    """
    ext = os.path.splitext(file_path)[1].lower()
    media_type = IMAGE_EXTENSIONS.get(ext)

    if not media_type:
        raise AgentFileParseError(
            f"Unsupported image format: {ext}. Supported: {list(IMAGE_EXTENSIONS.keys())}"
        )

    try:
        with open(file_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode("utf-8")

        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_data,
            },
        }
    except FileNotFoundError:
        raise AgentFileParseError(f"Image file not found: {file_path}")
    except Exception as e:
        raise AgentFileParseError(f"Failed to read image: {e}")


def parse_docx(file_path: str) -> str:
    """
    Extract text from a Microsoft Word (.docx) file.

    Claude can't natively read DOCX files, so we extract the text ourselves
    and pass it as a plain string. This loses formatting but preserves content.

    Uses the python-docx library (installed via pyproject.toml).

    Args:
        file_path: Absolute path to the .docx file.

    Returns:
        The full text content of the document as a string, with paragraphs
        separated by newlines.
    """
    try:
        # Import here (not at top) so the module doesn't crash if python-docx
        # isn't installed and someone only uses PDF/image parsing.
        from docx import Document

        doc = Document(file_path)

        # Extract text from all paragraphs, joining with newlines.
        # Each paragraph in a Word doc becomes one element in doc.paragraphs.
        paragraphs = [para.text for para in doc.paragraphs if para.text.strip()]
        return "\n".join(paragraphs)

    except FileNotFoundError:
        raise AgentFileParseError(f"DOCX file not found: {file_path}")
    except Exception as e:
        raise AgentFileParseError(f"Failed to read DOCX: {e}")


def parse_csv(file_path: str, max_rows: int = 100) -> str:
    """
    Read a CSV file and convert it to readable text.

    We cap at 100 rows to avoid sending too many tokens to Claude
    (each token costs money, and a large CSV could blow the budget).
    The first row is assumed to be headers.

    Args:
        file_path: Absolute path to the .csv file.
        max_rows: Maximum number of data rows to include (default: 100).
                  Header row doesn't count toward this limit.

    Returns:
        A formatted text representation of the CSV with headers and rows.
        If truncated, includes a note saying how many total rows exist.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if not rows:
            return "(Empty CSV file)"

        # First row = headers
        headers = rows[0]
        data_rows = rows[1:]
        total_rows = len(data_rows)

        # Truncate if too many rows
        display_rows = data_rows[:max_rows]

        # Format as a readable table
        lines = [" | ".join(headers)]
        lines.append("-" * len(lines[0]))  # Separator line
        for row in display_rows:
            lines.append(" | ".join(row))

        result = "\n".join(lines)

        # Add truncation notice if we didn't show all rows
        if total_rows > max_rows:
            result += f"\n\n(Showing {max_rows} of {total_rows} total rows)"

        return result

    except FileNotFoundError:
        raise AgentFileParseError(f"CSV file not found: {file_path}")
    except Exception as e:
        raise AgentFileParseError(f"Failed to read CSV: {e}")


def parse_txt(file_path: str) -> str:
    """
    Read a plain text file.

    Args:
        file_path: Absolute path to the .txt file.

    Returns:
        The file's content as a string.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        raise AgentFileParseError(f"Text file not found: {file_path}")
    except Exception as e:
        raise AgentFileParseError(f"Failed to read text file: {e}")


# ============================================================================
# Main Dispatcher
# ============================================================================

def parse_file(file_path: str) -> str | dict[str, Any]:
    """
    Parse any supported file type. Detects the format from the file extension
    and calls the appropriate parser.

    Returns EITHER:
      - A string (for DOCX, CSV, TXT) — text content to include in the prompt
      - A dict (for PDF, images) — an Anthropic content block to include in the
        message's content array directly

    The caller (the agent) needs to handle these two cases differently:
      - Strings get appended to the text prompt
      - Dicts get added as separate content blocks in the API message

    Supported formats:
      - .pdf  → Claude native document reading
      - .png, .jpg, .jpeg, .gif, .webp → Claude native vision
      - .docx → text extraction via python-docx
      - .csv  → text extraction via stdlib csv
      - .txt  → raw text reading

    Args:
        file_path: Path to the file to parse.

    Returns:
        Either a string (text content) or a dict (Anthropic content block).

    Raises:
        AgentFileParseError: If the file doesn't exist, can't be read, or
                            has an unsupported extension.
    """
    ext = os.path.splitext(file_path)[1].lower()

    # Route to the appropriate parser based on file extension
    if ext == ".pdf":
        return parse_pdf(file_path)
    elif ext in IMAGE_EXTENSIONS:
        return parse_image(file_path)
    elif ext == ".docx":
        return parse_docx(file_path)
    elif ext == ".csv":
        return parse_csv(file_path)
    elif ext == ".txt":
        return parse_txt(file_path)
    else:
        supported = [".pdf", ".docx", ".csv", ".txt"] + list(IMAGE_EXTENSIONS.keys())
        raise AgentFileParseError(
            f"Unsupported file format: '{ext}'. Supported formats: {supported}"
        )
