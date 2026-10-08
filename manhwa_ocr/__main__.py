"""Allows `python -m manhwa_ocr ...` regardless of PATH setup."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
