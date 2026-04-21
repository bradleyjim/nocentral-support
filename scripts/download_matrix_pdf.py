#!/usr/bin/env python3
"""
Downloads the HPE WLAN Platforms Software Support Matrix PDF.

Uses the `requests` library instead of curl because HPE's server has
historically been flaky about HTTP/2 negotiation with curl — requests
(via urllib3) handles the TLS handshake differently and has proven
more reliable against this specific endpoint.

Exit 0 on success, non-zero on failure.
"""

import sys
from pathlib import Path

import requests


URL = "https://www.hpe.com/psnow/doc/a50011736enw"
OUT_PATH = Path("hpe-matrix.pdf")
MIN_SIZE_BYTES = 100 * 1024  # 100 KB sanity floor

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def main() -> int:
    try:
        print(f"Downloading {URL} ...")
        response = requests.get(
            URL,
            headers=HEADERS,
            allow_redirects=True,
            timeout=(30, 120),  # (connect, read) timeouts in seconds
            stream=True,
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        print(f"  Status:       {response.status_code}")
        print(f"  Content-Type: {content_type}")
        print(f"  Final URL:    {response.url}")

        with OUT_PATH.open("wb") as f:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)

        size = OUT_PATH.stat().st_size
        print(f"  Saved to:     {OUT_PATH} ({size:,} bytes)")

        if size < MIN_SIZE_BYTES:
            print(
                f"ERROR: Downloaded file is {size} bytes, "
                f"below the {MIN_SIZE_BYTES}-byte sanity floor.",
                file=sys.stderr,
            )
            return 2

        # Verify it's actually a PDF (PDFs start with '%PDF-')
        with OUT_PATH.open("rb") as f:
            header = f.read(5)
        if header != b"%PDF-":
            print(
                f"ERROR: Downloaded file does not start with '%PDF-' "
                f"(got {header!r}). Server likely returned an error page.",
                file=sys.stderr,
            )
            return 3

        print("OK — downloaded file looks like a valid PDF.")
        return 0

    except requests.exceptions.RequestException as e:
        print(f"ERROR: Download failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
