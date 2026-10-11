#!/usr/bin/env python3
"""Internal entry point. An inherited socket is required; no standalone login."""
import argparse
import logging
from pathlib import Path
import socket

from rolling.worker import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fd", required=True, type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(process)d] %(name)s %(levelname)s %(message)s")
    return run(socket.socket(fileno=args.fd), Path(__file__).resolve().parent)


if __name__ == "__main__":
    raise SystemExit(main())
