#!/usr/bin/env python3
"""Export the completed private Taiko research into a compact production catalog."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from poolrotater_host.catalog_taiko import export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    manifest = export(args.research, args.destination)
    print(json.dumps({'destination': str(args.destination), 'manifest': manifest}, sort_keys=True))


if __name__ == '__main__':
    main()
