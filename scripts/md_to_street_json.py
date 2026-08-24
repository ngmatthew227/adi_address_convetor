#!/usr/bin/env python3
"""
Convert Street_Name_List.md into JSON with bilingual street names.

Output JSON format:
[
  {
    "chi_street_name": "...",
    "eng_street_name": "..."
  },
  ...
]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


EN_HEADER = "English Name"
ZH_HEADER = "Chinese Name"
DISTRICT_HEADER = "District Code"
DISTRICT_LEGEND_MARKER = "District Code | English District Name | Chinese District Name"


def normalize_spaces(value: str) -> str:
    """Normalize unusual spaces into plain single spaces."""
    # Some rows contain non-breaking spaces from PDF export.
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


def is_separator_row(first_col: str) -> bool:
    """Return True for markdown separator rows like '----'."""
    compact = first_col.replace(" ", "")
    if not compact:
        return True
    return all(ch in "-:|" for ch in compact)


def parse_line_as_row(line: str) -> list[str] | None:
    """
    Parse a markdown table line into columns.

    Returns None when line is not a table row.
    """
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    raw_cols = stripped.strip("|").split("|")
    return [normalize_spaces(col) for col in raw_cols]


def is_district_legend_header(cols: list[str]) -> bool:
    """Detect the district legend table header at the file end."""
    if len(cols) < 3:
        return False
    return (
        cols[0] == "District Code"
        and cols[1] == "English District Name"
        and cols[2] == "Chinese District Name"
    )


def extract_streets(markdown_path: Path) -> list[dict[str, str]]:
    content = markdown_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    streets: list[dict[str, str]] = []
    pending_index: int | None = None

    for raw_line in lines:
        line = raw_line.strip()
        normalized_line = normalize_spaces(line)
        if not line:
            continue

        # Stop once the district legend section starts.
        if DISTRICT_LEGEND_MARKER in normalized_line:
            break

        # Skip page markers.
        if line.startswith("Page "):
            continue

        cols = parse_line_as_row(raw_line)
        if cols is not None:
            if is_district_legend_header(cols):
                break

            # Ignore malformed or unrelated table rows.
            if len(cols) < 3:
                continue

            first_col = cols[0]
            second_col = cols[1]
            third_col = cols[2]

            # Skip headers and separator rows.
            if (
                EN_HEADER in first_col
                and ZH_HEADER in second_col
                and DISTRICT_HEADER in third_col
            ):
                continue
            if is_separator_row(first_col):
                continue

            eng_name = first_col
            chi_name = second_col

            if not eng_name:
                continue

            streets.append(
                {
                    "chi_street_name": chi_name,
                    "eng_street_name": eng_name,
                }
            )

            # Some broken rows have empty Chinese here, then Chinese appears on next plain line.
            pending_index = len(streets) - 1 if not chi_name else None
            continue

        # If previous row is missing Chinese, take this non-table line as fallback Chinese name.
        if pending_index is not None:
            maybe_chi = normalize_spaces(line)
            if maybe_chi:
                streets[pending_index]["chi_street_name"] = maybe_chi
                pending_index = None

    # Defensive cleanup: keep rows that contain both names only.
    cleaned = [
        row
        for row in streets
        if row.get("eng_street_name") and row.get("chi_street_name")
    ]
    return cleaned


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Street_Name_List.md to JSON containing "
            "chi_street_name and eng_street_name."
        )
    )
    parser.add_argument(
        "-i",
        "--input",
        default="data/raw/Street_Name_List.md",
        help="Path to input markdown file (default: data/raw/Street_Name_List.md)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="data/reference/street_names.json",
        help="Path to output JSON file (default: data/reference/street_names.json)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Write pretty-printed JSON.",
    )

    args = parser.parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    streets = extract_streets(input_path)
    output_path.write_text(
        json.dumps(
            streets,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
        ),
        encoding="utf-8",
    )

    print(f"Parsed {len(streets)} streets.")
    print(f"Wrote JSON to: {output_path}")


if __name__ == "__main__":
    main()
