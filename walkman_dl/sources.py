"""Work out what a pasted link is, and how its music can legally be fetched.

Two kinds of source:

  "ytdlp"     - the platform hosts the audio and yt-dlp can read it directly.
                YouTube, SoundCloud, Bandcamp, Mixcloud, Audiomack.

  "tracklist" - the platform streams DRM-protected audio that this tool does
                NOT touch. Only the public tracklist is read - song names,
                artists, durations - and each song is then looked for on a
                source of the first kind. Apple Music and Spotify work this
                way. Their audio is never decrypted, downloaded or accessed.

Tidal, Deezer and Amazon Music have no keyless public tracklist endpoint, so
they are reported as unsupported rather than guessed at.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Hosts whose audio yt-dlp can fetch directly.
DIRECT_HOSTS = {
    "youtube.com": "YouTube", "www.youtube.com": "YouTube",
    "m.youtube.com": "YouTube", "youtu.be": "YouTube",
    "music.youtube.com": "YouTube Music",
    "soundcloud.com": "SoundCloud", "www.soundcloud.com": "SoundCloud",
    "m.soundcloud.com": "SoundCloud", "on.soundcloud.com": "SoundCloud",
    "bandcamp.com": "Bandcamp",
    "mixcloud.com": "Mixcloud", "www.mixcloud.com": "Mixcloud",
    "audiomack.com": "Audiomack", "www.audiomack.com": "Audiomack",
}

# Streaming services with no public, keyless tracklist to read.
UNSUPPORTED = {
    "tidal.com": "Tidal", "listen.tidal.com": "Tidal",
    "deezer.com": "Deezer", "www.deezer.com": "Deezer",
    "music.amazon.com": "Amazon Music",
    "pandora.com": "Pandora", "www.pandora.com": "Pandora",
}


class SourceError(RuntimeError):
    pass


@dataclass
class SourceJob:
    kind: str                       # "ytdlp" | "tracklist"
    platform: str = ""
    url: str = ""                   # for kind == "ytdlp"
    name: str = ""                  # album or playlist name
    artist: str = ""
    year: str = ""
    art_url: str = ""
    is_album: bool = False
    tracks: list[dict] = field(default_factory=list)   # for kind=="tracklist"


# ------------------------------------------------------------------ detect

def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def platform_of(url: str) -> str:
    h = _host(url)
    if not h:
        return ""
    if h in DIRECT_HOSTS:
        return DIRECT_HOSTS[h]
    if h.endswith(".bandcamp.com"):
        return "Bandcamp"
    if h in ("open.spotify.com", "spotify.com", "www.spotify.com"):
        return "Spotify"
    if h in ("music.apple.com", "itunes.apple.com"):
        return "Apple Music"
    if h in UNSUPPORTED:
        return UNSUPPORTED[h]
    return ""


# ------------------------------------------------------------------ spotify

_SPOTIFY_ID = re.compile(r"/(album|playlist|track)/([A-Za-z0-9]+)")


def spotify_tracklist(url: str) -> SourceJob:
    """Read a public Spotify album/playlist/track's tracklist.

    Uses the same public embed payload that powers Spotify's own shareable
    player. No audio is requested and no account or key is involved.
    """
    m = _SPOTIFY_ID.search(url)
    if not m:
        raise SourceError("that Spotify link has no album, playlist or track id")
    kind, sid = m.group(1), m.group(2)

    try:
        r = requests.get(f"https://open.spotify.com/embed/{kind}/{sid}",
                         headers={"User-Agent": BROWSER_UA}, timeout=20)
        r.raise_for_status()
    except Exception as exc:
        raise SourceError(f"could not reach Spotify: {str(exc)[:120]}")

    m2 = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                   r.text, re.S)
    if not m2:
        raise SourceError("Spotify did not return a readable tracklist "
                          "(the link may be private or region-locked)")
    try:
        ent = (json.loads(m2.group(1))["props"]["pageProps"]["state"]
               ["data"]["entity"])
    except Exception:
        raise SourceError("Spotify's tracklist format was not recognised")

    art = ""
    sources = ((ent.get("coverArt") or {}).get("sources") or [])
    if sources:
        art = max(sources, key=lambda s: s.get("width") or 0).get("url", "")

    raw = ent.get("trackList") or []
    if not raw:
        raise SourceError("that Spotify link has no visible tracks")

    tracks = [{
        "title": t.get("title") or "",
        "artist": t.get("subtitle") or ent.get("subtitle") or "",
        "track_no": i,
        "disc_no": 1,
        "duration": (t.get("duration") or 0) / 1000.0,
        "genre": "",
    } for i, t in enumerate(raw, 1)]

    return SourceJob(
        kind="tracklist", platform="Spotify",
        name=ent.get("name") or "", artist=ent.get("subtitle") or "",
        art_url=art, is_album=(kind == "album"), tracks=tracks,
    )


# -------------------------------------------------------------- apple music

_APPLE_ID = re.compile(r"/(?:album|playlist|song)/[^/]*/(?:pl\.)?([0-9]+)")
_APPLE_TRACK = re.compile(r"[?&]i=([0-9]+)")


def apple_tracklist(url: str, country: str = "US") -> SourceJob:
    """Read an Apple Music album's tracklist via the public iTunes API."""
    if "/playlist/" in url:
        raise SourceError(
            "Apple Music *playlists* have no public tracklist endpoint - "
            "paste an album link, or the songs individually")
    m = _APPLE_ID.search(url)
    if not m:
        raise SourceError("that Apple Music link has no album id in it")
    collection_id = m.group(1)
    one_track = _APPLE_TRACK.search(url)

    try:
        r = requests.get("https://itunes.apple.com/lookup",
                         params={"id": collection_id, "entity": "song",
                                 "limit": 300, "country": country},
                         headers={"User-Agent": "walkman-dl/1.0"}, timeout=20)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as exc:
        raise SourceError(f"could not reach the iTunes API: {str(exc)[:120]}")

    collection = next((x for x in results
                       if x.get("wrapperType") == "collection"), None)
    songs = [x for x in results if x.get("wrapperType") == "track"]
    if not songs:
        raise SourceError("no tracks found for that Apple Music album")

    if one_track:
        songs = [s for s in songs
                 if str(s.get("trackId")) == one_track.group(1)] or songs[:1]

    art = (collection or songs[0]).get("artworkUrl100", "")
    art = re.sub(r"/\d+x\d+bb\.(jpg|png)$", "/600x600bb.jpg", art)

    return SourceJob(
        kind="tracklist", platform="Apple Music",
        name=(collection or {}).get("collectionName") or "",
        artist=(collection or {}).get("artistName") or songs[0].get("artistName", ""),
        year=((collection or {}).get("releaseDate") or "")[:4],
        art_url=art, is_album=True,
        tracks=[{
            "title": s.get("trackName") or "",
            "artist": s.get("artistName") or "",
            "track_no": s.get("trackNumber") or i,
            "disc_no": s.get("discNumber") or 1,
            "duration": (s.get("trackTimeMillis") or 0) / 1000.0,
            "genre": s.get("primaryGenreName") or "",
        } for i, s in enumerate(songs, 1)],
    )


# ------------------------------------------------------------------ resolve

def resolve(url: str, country: str = "US") -> SourceJob:
    """Turn a pasted link into a job this downloader knows how to run."""
    url = (url or "").strip()
    plat = platform_of(url)

    if plat in ("Spotify",):
        return spotify_tracklist(url)
    if plat in ("Apple Music",):
        return apple_tracklist(url, country)
    if plat in UNSUPPORTED.values():
        raise SourceError(
            f"{plat} streams are DRM-protected and it publishes no public "
            f"tracklist, so there is nothing this tool can legally read. "
            f"Paste the same album from Apple Music or Spotify and its track "
            f"list will be used instead.")
    # Anything else is handed to yt-dlp, which supports far more sites than
    # the short list above.
    return SourceJob(kind="ytdlp", platform=plat or "yt-dlp supported", url=url)
