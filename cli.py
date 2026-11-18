#!/usr/bin/env python3
"""Command-line front end for walkman-dl."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from walkman_dl.core import Options, run_playlist


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="walkman-dl",
        description="Download a YouTube Music album or YouTube playlist as "
                    "Walkman-ready MP3s (tags + cover art, size-capped).",
    )
    p.add_argument("url", help="YouTube Music album, YouTube playlist, or video "
                              "URL - a bare list id works too")
    p.add_argument("-o", "--out",
                   default=str(Path.home() / "Desktop" / "Walkman Music"),
                   help="output folder (default: ~/Desktop/Walkman Music)")
    p.add_argument("--max-mb", type=float, default=10.0,
                   help="per-file size cap in MB (default 10)")
    p.add_argument("--bitrate", type=int, default=320,
                   help="target bitrate in kbps (default 320)")
    p.add_argument("--min-bitrate", type=int, default=128,
                   help="never go below this (default 128)")
    p.add_argument("--art-px", type=int, default=500, help="cover art size in px")
    p.add_argument("--country", default="US",
                   help="iTunes storefront for metadata, e.g. IN, US, GB")
    p.add_argument("--flat", action="store_true",
                   help="all files in one folder (no Artist/Album subfolders)")
    p.add_argument("--no-numbers", action="store_true",
                   help="do not prefix filenames with track numbers")
    p.add_argument("--overwrite", action="store_true", help="re-download existing files")
    p.add_argument("--limit", type=int, default=0, help="only first N tracks")
    p.add_argument("--items", default="",
                   help="only these playlist positions, e.g. 4,5 or 2-6 - "
                        "album tags and track numbers still come from the "
                        "whole release")
    p.add_argument("--album-mode", choices=["auto", "always", "never"],
                   default="auto",
                   help="treat the link as one album release: auto detects "
                        "YouTube Music albums, always forces it for any "
                        "playlist, never disables it")
    p.add_argument("--cookies-from-browser", default="",
                   help="e.g. chrome / safari / firefox, for age-restricted items")
    p.add_argument("--report", default="", help="write a CSV report to this path")
    a = p.parse_args(argv)

    opts = Options(
        out_dir=Path(a.out),
        max_mb=a.max_mb,
        target_bitrate=a.bitrate,
        min_bitrate=a.min_bitrate,
        art_px=a.art_px,
        country=a.country,
        folders=not a.flat,
        number_files=not a.no_numbers,
        overwrite=a.overwrite,
        limit=a.limit,
        cookies_from_browser=a.cookies_from_browser,
        album_mode=a.album_mode,
        items=a.items,
    )

    def log(msg: str) -> None:
        print(msg, flush=True)

    results = run_playlist(a.url, opts, log)

    if a.report:
        with open(a.report, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["index", "status", "artist", "title", "album", "bitrate",
                        "size_mb", "duration_s", "has_art", "meta_source",
                        "meta_score", "path", "message"])
            for r in results:
                w.writerow([r.index, r.status, r.artist, r.title, r.album, r.bitrate,
                            r.size_mb, round(r.duration, 1), int(r.has_art),
                            r.meta_source, r.meta_score, r.path, r.message])
        print(f"report written: {a.report}")

    return 0 if any(r.status == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
