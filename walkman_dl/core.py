"""Download a YouTube playlist as Walkman-friendly MP3s.

Pipeline per track:
  yt-dlp (bestaudio) -> metadata lookup -> bitrate chosen from a size budget
  -> ffmpeg/libmp3lame CBR -> ID3v2.3 tags + baseline-JPEG cover art.

Sony Walkman E-series notes baked in here:
  * MP3 CBR, 44.1 kHz, stereo - the safest format the player indexes.
  * ID3v2.3 tags (v2.4 is only partly understood by these players).
  * Cover art embedded as baseline (non-progressive) JPEG, square, <= 500 px.
  * FAT32-safe filenames, flat "Artist/Album" folders, no deep nesting.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests
from PIL import Image

from .meta import (AlbumMeta, TrackMeta, _soft_ratio, clean_title,
                   guess_album_artist, lookup, lookup_album,
                   match_in_album, split_artist_title)
from .sources import SourceError, SourceJob, resolve

# Bitrate ladder, highest first. Only legal MPEG-1 Layer III rates appear
# here - ask libmp3lame for 288k and it silently gives you 256k instead.
BITRATE_LADDER = [320, 256, 224, 192, 160, 128, 112, 96]

# Room left inside the size budget for cover art + tag frames.
TAG_OVERHEAD_BYTES = 180 * 1024

Logger = Callable[[str], None]


def _log_noop(msg: str) -> None:
    pass


@dataclass
class Options:
    out_dir: Path
    max_mb: float = 10.0
    target_bitrate: int = 320
    min_bitrate: int = 128
    art_px: int = 500
    art_quality: int = 88
    country: str = "US"          # iTunes storefront for metadata lookup
    folders: bool = True          # Artist/Album subfolders
    number_files: bool = True     # "01 - Title.mp3"
    overwrite: bool = False
    cookies_from_browser: str = ""  # e.g. "chrome" for age/region-locked items
    limit: int = 0                # 0 = whole playlist
    album_mode: str = "auto"      # auto | always | never
    items: str = ""               # e.g. "4,5" or "2-6": which tracks to fetch


@dataclass
class Result:
    index: int
    video_id: str
    url: str
    status: str                  # ok | skipped | error
    path: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    bitrate: int = 0
    size_mb: float = 0.0
    duration: float = 0.0
    has_art: bool = False
    meta_source: str = ""
    meta_score: float = 0.0
    message: str = ""


# ---------------------------------------------------------------- urls

# YouTube Music album playlists always carry an OLAK5uy_ list id.
_ALBUM_LIST = re.compile(r"OLAK5uy_[\w-]+")
_LIST_ID = re.compile(r"^[A-Za-z0-9_-]{12,}$")


def normalise_url(raw: str) -> str:
    """Accept anything that identifies a playlist, album or video.

    Handles music.youtube.com links, album browse ids, bare list ids pasted on
    their own, and ordinary watch/playlist URLs.
    """
    u = (raw or "").strip().strip("<>\"'")
    if not u:
        return u

    if u.startswith(("OLAK5uy_", "PL", "LL", "UU", "RD", "OLA")) and _LIST_ID.match(u):
        return f"https://www.youtube.com/playlist?list={u}"
    if u.startswith("MPREb_") or u.startswith("MPLA"):
        return f"https://music.youtube.com/browse/{u}"
    if not u.startswith("http"):
        u = "https://" + u.lstrip("/")

    # music.youtube.com works, but the www host is better supported by yt-dlp.
    u = re.sub(r"^https?://music\.youtube\.com/playlist",
               "https://www.youtube.com/playlist", u)
    u = re.sub(r"^https?://music\.youtube\.com/watch",
               "https://www.youtube.com/watch", u)
    return u


def parse_items(spec: str, total: int) -> set:
    """Parse "4,5" or "2-6" into a set of 1-based playlist positions."""
    out: set = set()
    for part in (spec or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part[1:]:
            a, _, b = part.partition("-")
            try:
                out.update(range(int(a), int(b) + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(part))
            except ValueError:
                continue
    return {i for i in out if 1 <= i <= total}


def is_album_url(url: str, playlist_id: str = "") -> bool:
    return bool(_ALBUM_LIST.search(url) or _ALBUM_LIST.search(playlist_id or "")
                or "/browse/MPRE" in url)


# ---------------------------------------------------------------- filenames

_BAD_FS = r'<>:"/\|?*'


def fat32_safe(name: str, maxlen: int = 60) -> str:
    """FAT32-safe, ASCII-leaning filename component."""
    name = unicodedata.normalize("NFC", name)
    name = "".join("_" if c in _BAD_FS else c for c in name)
    name = "".join(c for c in name if ord(c) >= 32)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" .")
    return name or "Untitled"


# ---------------------------------------------------------------- bitrate

def pick_bitrate(duration: float, opts: Options) -> tuple[int, str]:
    """Highest ladder bitrate whose encoded size fits the per-file cap."""
    if not duration or duration <= 0:
        return opts.target_bitrate, "unknown duration; used target bitrate"
    budget = opts.max_mb * 1024 * 1024 - TAG_OVERHEAD_BYTES
    if budget <= 0:
        return opts.min_bitrate, "size cap smaller than tag overhead"
    kbps_max = (budget * 8) / duration / 1000.0
    for br in BITRATE_LADDER:
        if br > opts.target_bitrate:
            continue
        if br <= kbps_max:
            note = "" if br == opts.target_bitrate else (
                f"stepped {opts.target_bitrate}->{br} kbps to stay under {opts.max_mb:g} MB"
            )
            return br, note
    return opts.min_bitrate, (
        f"track is {duration/60:.1f} min; even {opts.min_bitrate} kbps exceeds "
        f"{opts.max_mb:g} MB"
    )


# ---------------------------------------------------------------- cover art

def fetch_art(url: str, px: int, quality: int) -> Optional[bytes]:
    """Download cover art and re-encode as a square baseline JPEG."""
    if not url:
        return None
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "walkman-dl/1.0"})
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        img = img.convert("RGB")
        w, h = img.size
        if w != h:                                  # centre-crop to square
            side = min(w, h)
            img = img.crop(((w - side) // 2, (h - side) // 2,
                            (w - side) // 2 + side, (h - side) // 2 + side))
        if img.size[0] > px:
            img = img.resize((px, px), Image.LANCZOS)
        buf = io.BytesIO()
        # progressive=False matters: Walkman firmware won't render progressive JPEG.
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=False)
        return buf.getvalue()
    except Exception:
        return None


# ---------------------------------------------------------------- tagging

def write_tags(mp3_path: Path, m: TrackMeta, art: Optional[bytes],
               comment: str = "") -> None:
    from mutagen.id3 import (
        ID3, ID3NoHeaderError, APIC, TIT2, TPE1, TPE2, TALB, TRCK, TPOS,
        TDRC, TCON, TYER, COMM,
    )

    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delete()

    def enc_for(*vals: str) -> int:
        # 0 = ISO-8859-1 (most compatible), 1 = UTF-16 with BOM (needed for
        # non-Latin1 text; E-series reads UTF-16 fine, UTF-8 frames it does not).
        for v in vals:
            if v:
                try:
                    v.encode("latin-1")
                except UnicodeEncodeError:
                    return 1
        return 0

    e = enc_for(m.title, m.artist, m.album, m.album_artist, m.genre)
    tags.add(TIT2(encoding=e, text=m.title))
    tags.add(TPE1(encoding=e, text=m.artist))
    tags.add(TPE2(encoding=e, text=m.album_artist or m.artist))
    if m.album:
        tags.add(TALB(encoding=e, text=m.album))
    if m.track_no:
        trck = f"{m.track_no}/{m.track_total}" if m.track_total else str(m.track_no)
        tags.add(TRCK(encoding=0, text=trck))
    if m.disc_no:
        tpos = f"{m.disc_no}/{m.disc_total}" if m.disc_total else str(m.disc_no)
        tags.add(TPOS(encoding=0, text=tpos))
    if m.year:
        tags.add(TDRC(encoding=0, text=m.year))
        tags.add(TYER(encoding=0, text=m.year))   # v2.3 year frame
    if m.genre:
        tags.add(TCON(encoding=e, text=m.genre))
    if comment:
        tags.add(COMM(encoding=e, lang="eng", desc="", text=comment))
    if art:
        tags.add(APIC(encoding=0, mime="image/jpeg", type=3,
                      desc="Cover", data=art))

    # v2.3 + ID3v1 for maximum player compatibility.
    tags.save(mp3_path, v2_version=3, v1=2)


# ---------------------------------------------------------------- yt-dlp

def playlist_entries(url: str, opts: Options,
                     log: Logger = _log_noop) -> tuple[dict, list[dict]]:
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    if opts.cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (opts.cookies_from_browser,)
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("could not read that URL (private playlist, or bad link)")

    entries = info.get("entries")
    if entries is None:                       # single video link
        entries = [info]
    out = []
    for en in entries:
        if not en:
            continue
        if en.get("_type") == "playlist" and en.get("entries"):   # nested tabs
            out.extend([e for e in en["entries"] if e])
            continue
        out.append(en)
    if opts.limit:
        out = out[: opts.limit]
    return info, out


# YouTube hands out 403s on some streams depending on which player client
# asked for them, so each client gets a turn before the track is given up on.
PLAYER_CLIENTS = [None, ["android"], ["web_safari"], ["tv"], ["ios"]]


def _download_audio(video_url: str, tmp: Path, opts: Options,
                    log: Logger = _log_noop) -> tuple[Path, dict]:
    import yt_dlp

    last_err: Optional[Exception] = None
    for attempt, clients in enumerate(PLAYER_CLIENTS):
        for stale in tmp.iterdir():
            stale.unlink(missing_ok=True)
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "format": "bestaudio[acodec!=none]/bestaudio/best",
            "outtmpl": str(tmp / "src.%(ext)s"),
            "retries": 4,
            "fragment_retries": 4,
            "concurrent_fragment_downloads": 4,
            "writethumbnail": True,
        }
        if clients:
            ydl_opts["extractor_args"] = {"youtube": {"player_client": clients}}
        if opts.cookies_from_browser:
            ydl_opts["cookiesfrombrowser"] = (opts.cookies_from_browser,)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
            files = [p for p in tmp.iterdir() if p.stem == "src"]
            audio = [p for p in files if p.suffix.lower() not in
                     (".jpg", ".jpeg", ".png", ".webp")]
            if not audio:
                raise RuntimeError("yt-dlp produced no audio file")
            if attempt:
                log(f"      (succeeded with player_client={clients})")
            return audio[0], info
        except Exception as exc:
            last_err = exc
            if attempt + 1 < len(PLAYER_CLIENTS):
                log(f"      stream refused ({str(exc)[:80]}); retrying with "
                    f"player_client={PLAYER_CLIENTS[attempt + 1]}")
    raise RuntimeError(
        f"all download attempts failed: {str(last_err)[:300]}"
        " - if this keeps happening, pass --cookies-from-browser chrome"
    )


# ---------------------------------------------------------------- encoding

def encode_mp3(src: Path, dst: Path, bitrate: int) -> None:
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src),
        "-vn", "-map", "0:a:0",
        "-c:a", "libmp3lame",
        "-b:a", f"{bitrate}k",
        "-ar", "44100", "-ac", "2",
        "-write_xing", "1",
        "-id3v2_version", "3",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[:400]}")


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except Exception:
        return 0.0


# ------------------------------------------------- find a known song on YT

# Uploads that are not the studio recording someone asked for.
_WRONG_CUT = re.compile(
    r"\b(live|cover|remix|reaction|review|karaoke|instrumental|sped\s*up|"
    r"slowed|nightcore|8d|loop(ed)?|mashup|tutorial|behind\s*the)\b", re.I)


def find_on_youtube(artist: str, title: str, duration: float, opts: Options,
                    log: Logger = _log_noop) -> Optional[dict]:
    """Search YouTube for one known song and return the closest upload.

    Duration is the strongest signal: a studio recording that matches the
    release length to within a few seconds is almost always the right one.
    """
    import yt_dlp

    query = f"{artist} {title}".strip()
    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True,
                "skip_download": True}
    if opts.cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (opts.cookies_from_browser,)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            res = ydl.extract_info(f"ytsearch6:{query}", download=False)
    except Exception as exc:
        log(f"      search failed: {str(exc)[:90]}")
        return None

    best, best_score = None, 0.0
    for en in (res or {}).get("entries") or []:
        if not en:
            continue
        cand = clean_title(en.get("title") or "")
        _, song = split_artist_title(cand)
        s = max(_soft_ratio(title, song), _soft_ratio(title, cand))
        if artist:
            up = f"{en.get('uploader') or ''} {en.get('title') or ''}"
            if _soft_ratio(artist, en.get("uploader") or "") > 0.6 or \
                    artist.lower() in up.lower():
                s += 0.15
        d = en.get("duration") or 0
        if duration and d:
            delta = abs(duration - d)
            if delta <= 3:
                s += 0.30
            elif delta <= 10:
                s += 0.15
            elif delta > 30:
                s -= 0.50          # a different cut of the song
        if _WRONG_CUT.search(en.get("title") or "") and \
                not _WRONG_CUT.search(title):
            s -= 0.35
        if (en.get("uploader") or "").endswith("- Topic"):
            s += 0.10             # auto-generated official audio
        if s > best_score:
            best_score, best = s, en

    if not best or best_score < 0.55:
        return None
    return best


def run_tracklist(job: SourceJob, opts: Options, log: Logger,
                  on_result: Optional[Callable[[Result], None]] = None
                  ) -> list[Result]:
    """Fetch every song on a tracklist read from a streaming service.

    The streaming service supplies only names, artists and durations. Each
    song is then located on a site whose audio yt-dlp can legally read, and
    the tags come from the tracklist - so the files are named and foldered
    correctly even though the audio came from elsewhere.
    """
    album = AlbumMeta(
        album=job.name if job.is_album else "",
        album_artist=job.artist,
        year=job.year,
        genre=next((t.get("genre") for t in job.tracks if t.get("genre")), ""),
        art_url=job.art_url,
        track_total=len(job.tracks),
        disc_total=max([t.get("disc_no") or 1 for t in job.tracks] or [1]),
        tracks=job.tracks,
        source=job.platform.lower().replace(" ", ""),
    )
    # Spotify's public payload carries no release year or genre, so those are
    # filled in from the keyless catalogue APIs without disturbing the
    # tracklist or cover art the source already gave us.
    if job.is_album and job.name and (not album.year or not album.genre):
        enrich = lookup_album(job.name, job.artist,
                              [t["title"] for t in job.tracks],
                              country=opts.country)
        if enrich.source in ("itunes", "musicbrainz"):
            album.year = album.year or enrich.year
            album.genre = album.genre or enrich.genre
            album.art_url = album.art_url or enrich.art_url

    log(f'{job.platform}: "{job.name}"'
        + (f" by {job.artist}" if job.artist else "")
        + f" - {len(job.tracks)} track(s)")
    log("reading the tracklist only; this tool never touches "
        f"{job.platform} audio")

    results: list[Result] = []
    used_paths: set = set()
    selected = parse_items(opts.items, len(job.tracks))
    todo = [t for i, t in enumerate(job.tracks, 1)
            if not selected or i in selected]
    if opts.limit:
        todo = todo[: opts.limit]

    for n, t in enumerate(todo, 1):
        idx = t.get("track_no") or n
        label = f"{t['artist']} - {t['title']}"
        log(f"[{idx}] looking for: {label}")
        hit = find_on_youtube(t["artist"], t["title"], t.get("duration") or 0,
                              opts, log)
        if not hit:
            r = Result(index=idx, video_id="", url="", status="error",
                       title=t["title"], artist=t["artist"],
                       album=album.album,
                       message="no close enough match found on YouTube")
            log(f"[{idx}] NOT FOUND: {label}")
            results.append(r)
            if on_result:
                on_result(r)
            continue

        entry = {
            "id": hit.get("id"),
            "url": hit.get("url") or f"https://www.youtube.com/watch?v={hit.get('id')}",
            "title": f"{t['artist']} - {t['title']}",
            "duration": hit.get("duration"),
        }
        r = process_one(entry, idx, opts, log, album, len(job.tracks), used_paths)
        results.append(r)
        if on_result:
            on_result(r)
    return results


# ---------------------------------------------------------------- one track

def process_one(entry: dict, index: int, opts: Options,
                log: Logger = _log_noop,
                album: Optional[AlbumMeta] = None,
                total: int = 0,
                used_paths: Optional[set] = None) -> Result:
    vid = entry.get("id") or ""
    url = entry.get("url") or entry.get("webpage_url") or ""
    if url and not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"
    if not url and vid:
        url = f"https://www.youtube.com/watch?v={vid}"
    raw_title = entry.get("title") or vid or "unknown"
    res = Result(index=index, video_id=vid, url=url, status="error", title=raw_title)

    if raw_title in ("[Private video]", "[Deleted video]", "[Unavailable video]"):
        res.status = "skipped"
        res.message = raw_title
        return res

    tmp = Path(tempfile.mkdtemp(prefix="wdl_"))
    try:
        log(f"[{index}] downloading: {raw_title}")
        src, info = _download_audio(url, tmp, opts, log)
        duration = float(info.get("duration") or 0) or probe_duration(src)
        res.duration = duration

        yt_title = info.get("title") or raw_title
        if album is not None:
            m = match_in_album(album, yt_title, duration, index)
            if m is None:
                # Playlist order plus the uploading channel is ALREADY correct
                # metadata for an album. A global per-track search can only
                # corrupt it - it is what matched "Money" to Michael Jackson -
                # so in album mode it is never consulted.
                _, song = split_artist_title(yt_title)
                m = TrackMeta(
                    title=clean_title(song) or clean_title(yt_title) or yt_title,
                    artist=album.album_artist or "Unknown Artist",
                    album=album.album,
                    album_artist=album.album_artist,
                    track_no=index,
                    track_total=album.track_total or total or None,
                    disc_no=1, disc_total=1,
                    year=album.year, genre=album.genre,
                    art_url=album.art_url,
                    source="album-order",
                )
        else:
            m = lookup(
                raw_title=yt_title,
                uploader=info.get("uploader") or info.get("channel") or "",
                duration=duration,
                yt_track=info.get("track") or "",
                yt_artist=(info.get("artist") or info.get("creator") or ""),
                yt_album=info.get("album") or "",
                country=opts.country,
            )
        if not m.track_no and opts.number_files:
            m.track_no = index
        res.title, res.artist, res.album = m.title, m.artist, m.album
        res.meta_source, res.meta_score = m.source, m.score
        log(f"[{index}] tags: {m.artist} - {m.title}"
            + (f" [{m.album}]" if m.album else "")
            + f"  ({m.source} {m.score})")

        bitrate, note = pick_bitrate(duration, opts)
        if note:
            log(f"[{index}] {note}")
        res.bitrate = bitrate

        # Destination path
        folder = opts.out_dir
        if opts.folders:
            folder = folder / fat32_safe(m.album_artist or m.artist, 40)
            folder = folder / fat32_safe(m.album or "Singles", 40)
        folder.mkdir(parents=True, exist_ok=True)
        base = fat32_safe(m.title, 64)
        if opts.number_files:
            # In flat mode the playlist position keeps files in order and
            # unique; inside album folders the real track number is better.
            n = index if not opts.folders else (m.track_no or index)
            base = f"{int(n):02d} - {base}"
        dst = folder / f"{base}.mp3"
        if used_paths is not None and str(dst).lower() in used_paths:
            base = f"{base} ({index})"
            dst = folder / f"{base}.mp3"
        if used_paths is not None:
            used_paths.add(str(dst).lower())
        if dst.exists() and not opts.overwrite:
            res.status = "skipped"
            res.path = str(dst)
            res.message = "file already exists"
            log(f"[{index}] skip (exists): {dst.name}")
            return res

        tmp_mp3 = tmp / "out.mp3"
        encode_mp3(src, tmp_mp3, bitrate)

        art = fetch_art(m.art_url, opts.art_px, opts.art_quality)
        if not art:
            thumbs = sorted(
                [p for p in tmp.iterdir()
                 if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")],
                key=lambda p: p.stat().st_size, reverse=True)
            if thumbs:
                try:
                    img = Image.open(thumbs[0]).convert("RGB")
                    w, h = img.size
                    side = min(w, h)
                    img = img.crop(((w - side) // 2, (h - side) // 2,
                                    (w - side) // 2 + side, (h - side) // 2 + side))
                    img = img.resize((opts.art_px, opts.art_px), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=opts.art_quality,
                             optimize=True, progressive=False)
                    art = buf.getvalue()
                    m.notes.append("cover art cropped from YouTube thumbnail")
                except Exception:
                    art = None
        res.has_art = bool(art)

        write_tags(tmp_mp3, m, art, comment="")
        shutil.move(str(tmp_mp3), str(dst))

        size = dst.stat().st_size
        res.size_mb = round(size / 1024 / 1024, 2)
        res.path = str(dst)
        res.status = "ok"
        over = " OVER CAP" if size > opts.max_mb * 1024 * 1024 else ""
        log(f"[{index}] done: {dst.name}  {bitrate}kbps  {res.size_mb} MB"
            f"  art={'yes' if art else 'no'}{over}")
        return res
    except Exception as exc:                        # one bad track must not stop the run
        res.status = "error"
        res.message = str(exc)[:500]
        log(f"[{index}] ERROR: {res.message}")
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_playlist(url: str, opts: Options, log: Logger = _log_noop,
                 on_result: Optional[Callable[[Result], None]] = None) -> list[Result]:
    opts.out_dir = Path(opts.out_dir)
    opts.out_dir.mkdir(parents=True, exist_ok=True)

    # Streaming services that only expose a tracklist take a different route.
    job = resolve(url, opts.country)
    if job.kind == "tracklist":
        results = run_tracklist(job, opts, log, on_result)
        ok = [r for r in results if r.status == "ok"]
        log(f"finished: {len(ok)} ok, "
            f"{sum(1 for r in results if r.status=='error')} failed")
        return results

    url = normalise_url(job.url or url)
    log("reading link...")
    info, entries = playlist_entries(url, opts, log)
    container = info.get("title") or ""
    log(f"{len(entries)} item(s) found"
        + (f' in "{container}"' if container and len(entries) > 1 else ""))

    album: Optional[AlbumMeta] = None
    treat_as_album = (
        opts.album_mode == "always"
        or (opts.album_mode == "auto" and len(entries) > 1
            and is_album_url(url, info.get("id") or ""))
    )
    if treat_as_album and container:
        titles = [e.get("title") or "" for e in entries]
        uploaders = [e.get("uploader") or e.get("channel") or ""
                     for e in entries]
        artist = guess_album_artist(titles, info.get("uploader")
                                    or info.get("channel") or "", uploaders)
        log(f'album mode: looking up "{container}"'
            + (f" by {artist}" if artist else ""))
        album = lookup_album(container, artist, titles, country=opts.country)
        if album.source in ("itunes", "musicbrainz"):
            log(f"  matched on {album.source}: {album.album_artist} - "
                f"{album.album} ({album.year}, {album.track_total} tracks, "
                f"score {album.score})")
        else:
            log("  no iTunes release matched; using the YouTube album title "
                "and playlist order")
            album.album = album.album or container
            album.album_artist = album.album_artist or artist
            album.track_total = len(entries)

    # Album context is built from the WHOLE release, then only the requested
    # positions are fetched - so a single track keeps its real track number.
    selected = parse_items(opts.items, len(entries))
    if selected:
        log(f"fetching {len(selected)} of {len(entries)}: "
            f"{', '.join(str(i) for i in sorted(selected))}")

    results: list[Result] = []
    used_paths: set = set()
    for i, en in enumerate(entries, 1):
        if selected and i not in selected:
            continue
        r = process_one(en, i, opts, log, album, len(entries), used_paths)
        results.append(r)
        if on_result:
            on_result(r)
    ok = [r for r in results if r.status == "ok"]
    log(f"finished: {len(ok)} ok, "
        f"{sum(1 for r in results if r.status=='skipped')} skipped, "
        f"{sum(1 for r in results if r.status=='error')} failed")
    if ok:
        log(f"total size: {sum(r.size_mb for r in ok):.1f} MB, "
            f"largest file: {max(r.size_mb for r in ok):.2f} MB")
    return results
