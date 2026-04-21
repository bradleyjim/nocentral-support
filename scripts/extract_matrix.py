#!/usr/bin/env python3
"""
NoCentral AP Capability Matrix Extractor

Reads page 5 of the HPE WLAN Platforms Software Support Matrix PDF,
extracts the Instant 8.x support data for AP models in NoCentral's
hardware scope (AP-3xx through AP-6xx), and writes a structured JSON
file consumed by the NoCentral iOS/macOS app.

Output formatting matches the hand-built reference file exactly —
blank lines between top-level sections, single-line model entries
with column-aligned fields — so git diffs across refreshes show
only real data changes, not formatting churn.

Exit code 0 = success, non-zero = failure (validation or extraction error).
"""

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

import pdfplumber


PDF_PATH = Path("hpe-matrix.pdf")
OUTPUT_PATH = Path("ap-capability-matrix.json")


# ----- Fixed context fields (don't change between refreshes) ----------------

SCHEMA_NOTES = (
    "NoCentral AP capability matrix. Data extracted verbatim from the HPE "
    "Aruba Networking WLAN Platforms Software Support Matrix, Instant 8.x "
    "columns only. Bump schema_version only when the shape of this file "
    "changes (new fields, renamed fields, etc.). Do NOT bump it for "
    "content-only refreshes from HPE — those bump matrix_revision and "
    "matrix_data_date instead."
)

SOURCE_URL_NOTE = (
    "Pulled directly from the HPE support portal by an SE. Document updates "
    "ad-hoc — no fixed release cadence. Jim's workflow: refresh PDF from "
    "portal, compare Rev number and data date, regenerate this file, commit."
)

INSTANT_8X_CONTEXT = {
    "final_release": "8.13",
    "status": "final",
    "notes": (
        "HPE has announced 8.13 as the final Instant 8.x release. Every TBD "
        "max in this file will ultimately resolve to 8.13 (or earlier if "
        "HPE drops a platform mid-train). There is no 8.14 and there will "
        "not be. Features, bug fixes, and CVE patches beyond 8.13 land in "
        "AOS-10 only, which requires HPE Aruba Networking Central and is "
        "outside NoCentral's scope."
    ),
}

NOCENTRAL_HARDWARE_SCOPE = {
    "min_series": "3xx",
    "max_series": "6xx",
    "notes": (
        "NoCentral supports AP-3xx through AP-6xx hardware only. Older "
        "models (2xx and earlier) are not applicable. Models outside this "
        "range should not appear in the matrix below — if the VC reports "
        "one, the Planner should treat it as out-of-scope rather than "
        "unsupported."
    ),
}

TBD_SEMANTICS = {
    "meaning": (
        "Max version not yet decided by HPE. Literal — HPE may announce "
        "a cap at any point."
    ),
    "planner_interpretation": (
        "For any train currently in existence (up to and including the "
        "instant_8x_final_release), TBD passes the max check. UI should "
        "display 'currently uncapped' rather than implying open-ended "
        "future support. Combine with instant_8x_context.final_release "
        "to show the effective ceiling: e.g., AP-505 max = TBD, "
        "effective ceiling = 8.13."
    ),
}

UNKNOWN_MODEL_HANDLING = {
    "guidance": (
        "If the VC reports an AP model not present in this file AND within "
        "nocentral_hardware_scope, flag it as 'unknown — matrix update "
        "needed'. This is the canary signal that HPE has shipped a new AP "
        "and Jim needs to refresh the matrix. Do not assume sensible "
        "defaults — unknown means unknown."
    ),
}


def build_aos10_only_note(excluded_models: list[str]) -> str:
    """Build the aos10-only note, enumerating the actual excluded models."""
    if not excluded_models:
        enumeration = ""
    else:
        enumeration = f" ({', '.join(excluded_models)})"
    return (
        f"Models listed on page 5 of the source matrix with N/A for Instant "
        f"8.x{enumeration} are AOS-10-only hardware and cannot run Instant "
        f"firmware. They are DELIBERATELY EXCLUDED from this file. If such "
        f"a model appears in a VC cluster report, that is a hardware/"
        f"licensing mismatch, not a version mismatch. The Planner should "
        f"detect this case separately (by checking an out-of-band AOS-10-"
        f"only model list) and surface a distinct message — remediation "
        f"is platform migration to Central, not firmware upgrade or "
        f"hardware replacement."
    )


# ----- Filtering ------------------------------------------------------------

# AP-3xx through AP-6xx, optional letter suffix (H, P, R, HR).
AP_MODEL_RE = re.compile(r"^AP-[3-6]\d{2}[A-Z]{0,2}$")


def is_target_model(name: str) -> bool:
    return bool(AP_MODEL_RE.match(name))


# ----- Revision + date extraction -------------------------------------------

REV_RE = re.compile(r"a50011736ENW,\s*(Rev\.\s*\d+)", re.IGNORECASE)
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def extract_revision(pdf) -> str:
    page5_text = pdf.pages[4].extract_text() or ""
    m = REV_RE.search(page5_text)
    if not m:
        raise RuntimeError(
            "Could not find 'a50011736ENW, Rev. N' on page 5. "
            "Document format may have changed."
        )
    return m.group(1)


def extract_data_date(pdf) -> str:
    """Pulls M/D/YYYY from top of page 1, returns ISO YYYY-MM-DD."""
    page1_text = pdf.pages[0].extract_text() or ""
    head = "\n".join(page1_text.splitlines()[:5])
    m = DATE_RE.search(head)
    if not m:
        raise RuntimeError(
            "Could not find an M/D/YYYY data date near the top of page 1. "
            "Document format may have changed."
        )
    month, day, year = m.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


# ----- Core extraction ------------------------------------------------------

def extract_models(pdf) -> tuple[list[dict], list[str]]:
    """
    Returns (included_models, aos10_only_models).

    included_models: AP-3xx to AP-6xx with non-N/A Instant 8.x range.
    aos10_only_models: All AP-prefixed rows with N/A Instant 8.x — the
                       canonical AOS-10-only list for the enumeration note.
    """
    page5 = pdf.pages[4]
    tables = page5.extract_tables()
    if not tables:
        raise RuntimeError("No tables detected on page 5.")

    table = tables[0]
    if len(table) < 2:
        raise RuntimeError(
            f"Page 5 table has {len(table)} rows; expected at least 2."
        )

    data_row = table[1]
    names = data_row[0].split("\n")
    values = data_row[1].split("\n")

    if len(names) != len(values):
        raise RuntimeError(
            f"Row count mismatch: {len(names)} names vs {len(values)} value "
            f"rows. PDF layout may have changed."
        )

    models = []
    aos10_only = []

    for name, value_line in zip(names, values):
        name = name.strip()
        parts = value_line.split()
        if len(parts) < 2:
            continue

        instant_min, instant_max = parts[0], parts[1]

        if name.startswith("AP-") and (instant_min == "N/A" or instant_max == "N/A"):
            aos10_only.append(name)
            continue

        if not is_target_model(name):
            continue

        models.append({
            "model": name,
            "instant_min": instant_min,
            "instant_max": instant_max,
        })

    models.sort(key=lambda m: m["model"])
    aos10_only.sort()
    return models, aos10_only


# ----- Validation -----------------------------------------------------------

def validate(models: list[dict], revision: str, data_date: str) -> None:
    if not (30 <= len(models) <= 80):
        raise RuntimeError(
            f"Model count {len(models)} is outside sanity range 30–80. "
            f"Extraction likely failed."
        )

    for m in models:
        if not AP_MODEL_RE.match(m["model"]):
            raise RuntimeError(f"Model '{m['model']}' does not match AP-3xx–6xx regex.")
        if not m["instant_min"] or m["instant_min"] == "N/A":
            raise RuntimeError(f"Empty/N/A instant_min for {m['model']}.")
        if not m["instant_max"] or m["instant_max"] == "N/A":
            raise RuntimeError(f"Empty/N/A instant_max for {m['model']}.")

    if not revision or "<" in revision:
        raise RuntimeError(f"Bad revision: {revision!r}")

    try:
        datetime.strptime(data_date, "%Y-%m-%d")
    except ValueError:
        raise RuntimeError(f"Bad data date: {data_date!r}")


# ----- Custom JSON formatter ------------------------------------------------
# Matches the hand-built reference formatting:
#   - Two-space indent overall
#   - Blank lines between top-level sections (schema_notes stays paired
#     with schema_version — no blank between them)
#   - Each model entry on one line with column-aligned fields
#   - Trailing newline

def format_json(matrix: dict) -> str:
    """Serialize the matrix dict to match the reference file's formatting."""

    def dumps_inline(value) -> str:
        return json.dumps(value, ensure_ascii=False)

    def dumps_block(value, indent: int) -> str:
        pad = " " * indent
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, (int, float, bool)) or value is None:
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, dict):
            if not value:
                return "{}"
            inner = ",\n".join(
                f"{pad}  {json.dumps(k, ensure_ascii=False)}: "
                f"{dumps_block(v, indent + 2)}"
                for k, v in value.items()
            )
            return "{\n" + inner + "\n" + pad + "}"
        if isinstance(value, list):
            if not value:
                return "[]"
            inner = ",\n".join(
                f"{pad}  {dumps_block(item, indent + 2)}"
                for item in value
            )
            return "[\n" + inner + "\n" + pad + "]"
        raise TypeError(f"Unsupported type: {type(value)}")

    # Compute column widths for model entries so they align.
    models = matrix["models"]
    if models:
        model_w = max(len(dumps_inline(m["model"])) for m in models)
        min_w = max(len(dumps_inline(m["instant_min"])) for m in models)
        max_w = max(len(dumps_inline(m["instant_max"])) for m in models)
    else:
        model_w = min_w = max_w = 0

    def format_model(m: dict) -> str:
        model_s = dumps_inline(m["model"])
        min_s = dumps_inline(m["instant_min"])
        max_s = dumps_inline(m["instant_max"])
        model_pad = " " * (model_w - len(model_s))
        min_pad = " " * (min_w - len(min_s))
        max_pad = " " * (max_w - len(max_s))
        return (
            f'    {{ "model": {model_s},{model_pad} '
            f'"instant_min": {min_s},{min_pad} '
            f'"instant_max": {max_s}{max_pad} }}'
        )

    # Build top level manually so we can insert blank lines between sections.
    lines = ["{"]
    keys = list(matrix.keys())
    for i, key in enumerate(keys):
        value = matrix[key]
        comma = "," if i < len(keys) - 1 else ""

        if key == "models":
            lines.append(f'  "{key}": [')
            for j, m in enumerate(value):
                entry = format_model(m)
                entry_comma = "," if j < len(value) - 1 else ""
                lines.append(entry + entry_comma)
            lines.append(f"  ]{comma}")
        else:
            rendered = dumps_block(value, 2)
            lines.append(f'  "{key}": {rendered}{comma}')

        # Blank line AFTER a key if the next key starts a new section.
        # schema_notes stays paired with schema_version (no blank between).
        if i < len(keys) - 1:
            next_key = keys[i + 1]
            if next_key != "schema_notes":
                lines.append("")

    lines.append("}")
    return "\n".join(lines) + "\n"


# ----- JSON assembly --------------------------------------------------------

def build_matrix(models: list[dict], aos10_only: list[str],
                 revision: str, data_date: str) -> dict:
    return {
        "schema_version": 1,
        "schema_notes": SCHEMA_NOTES,
        "matrix_source": {
            "publisher": "HPE Aruba Networking",
            "document_title": "WLAN Platforms Software Support Matrix",
            "document_id": "a50011736ENW",
            "matrix_revision": revision,
            "matrix_data_date": data_date,
            "source_url_note": SOURCE_URL_NOTE,
            "extracted_from_page": 5,
            "extraction_date": date.today().isoformat(),
        },
        "instant_8x_context": INSTANT_8X_CONTEXT,
        "nocentral_hardware_scope": NOCENTRAL_HARDWARE_SCOPE,
        "tbd_semantics": TBD_SEMANTICS,
        "unknown_model_handling": UNKNOWN_MODEL_HANDLING,
        "aos10_only_models_note": build_aos10_only_note(aos10_only),
        "models": models,
    }


# ----- Entry point ----------------------------------------------------------

def main() -> int:
    if not PDF_PATH.exists():
        print(f"ERROR: {PDF_PATH} not found in current directory.",
              file=sys.stderr)
        return 1

    try:
        with pdfplumber.open(PDF_PATH) as pdf:
            if len(pdf.pages) < 5:
                raise RuntimeError(
                    f"Expected at least 5 pages in the PDF; got {len(pdf.pages)}."
                )
            revision = extract_revision(pdf)
            data_date = extract_data_date(pdf)
            models, aos10_only = extract_models(pdf)

        validate(models, revision, data_date)
        matrix = build_matrix(models, aos10_only, revision, data_date)

        OUTPUT_PATH.write_text(format_json(matrix), encoding="utf-8")

        print(f"Wrote {OUTPUT_PATH}")
        print(f"  Revision:     {revision}")
        print(f"  Data date:    {data_date}")
        print(f"  Models:       {len(models)}")
        print(f"  AOS-10-only:  {len(aos10_only)} ({', '.join(aos10_only)})")
        return 0

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
