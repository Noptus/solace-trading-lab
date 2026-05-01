"""Replay command-line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from replay.player import ReplayPlayer
from replay.recorder import ReplayRecorder


def run() -> None:
    """Run recorder or player mode from the command line."""

    parser = argparse.ArgumentParser(description="Record or replay Solace Trading Lab messages.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    record_parser = subparsers.add_parser("record")
    record_parser.add_argument("path", type=Path)
    play_parser = subparsers.add_parser("play")
    play_parser.add_argument("path", type=Path)
    play_parser.add_argument("--realtime", action="store_true")
    args = parser.parse_args()

    if args.command == "record":
        recorder = ReplayRecorder(args.path)
        recorder.start()
        try:
            input("Recording. Press Enter to stop.\n")
        finally:
            recorder.close()
    else:
        ReplayPlayer(args.path).play(realtime=args.realtime)


if __name__ == "__main__":
    run()
