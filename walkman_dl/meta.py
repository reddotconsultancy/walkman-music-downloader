"""Metadata lookup: clean up YouTube titles, then match against the iTunes
Search API for proper tags and high-resolution cover art."""
from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

ITUNES_SEARCH = "https://itunes.apple.com/search"
UA = "walkman-dl/1.0 (+local tool)"

# Junk commonly appended to YouTube music titles.
_JUNK_PATTERNS = [
    r"\((?:official\s*)?(?:music\s*)?video\)",
    r"\[(?:official\s*)?(?:music\s*)?video\]",
    r"\((?:official\s*)?(?:lyric|lyrics)\s*(?:video)?\)",
    r"\[(?:official\s*)?(?:lyric|lyrics)\s*(?:video)?\]",
    r"\(official\s*audio\)",
    r"\[official\s*audio\]",
    r"\(official\)",
    r"\(audio\)",
    r"\[audio\]",
    r"\(visualizer\)",
    r"\(hd\)|\[hd\]|\(hq\)|\[hq\]|\(4k\)|\[4k\]",
    r"\(full\s*song\)",
    r"\(with\s*lyrics\)",
    r"\blyrical\b",
    r"\bfull\s*video\s*song\b",
    r"\bfull\s*audio\s*song\b",
    r"\bvideo\s*song\b",
    r"\bofficial\s*trailer\b",
    r"#\w+",
    r"\bno\s*copyright\s*sounds?\b",
    r"\bcopyright[\s-]*free(?:\s*music)?\b",
    r"\bfree\s*(?:to\s*use\s*)?music\b",
    r"\bncs\s*(?:release|music)?\b",
    r"\bfull\s*(?:hd\s*)?quality\b",
    r"\bcolor\s*coded\b",
]

_FEAT = re.compile(r"\s*[\(\[]?\b(?:feat\.?|ft\.?|featuring)\b\s*([^\)\]]+)[\)\]]?", re.I)

# Bracketed release/edition noise. Only groups containing one of these words
# are dropped, so a real title like "Breathe (In the Air)" survives.
_BRACKET_NOISE = re.compile(
    r"\s*[\(\[][^)\]]*\b(?:remaster(?:ed)?|anniversary|official|video|audio|"
    r"visuali[sz]er|lyrics?|concert\s*screen|british\s*tour|competition\s*winner|"
    r"hd|4k|mono|stereo mix|radio\s*edit|full\s*song|"
    r"musical\s*short\s*film|short\s*film|shortfilm|music\s*video)\b"
    r"[^)\]]*[\)\]]", re.I)


def clean_title(raw: str) -> str:
    t = _BRACKET_NOISE.sub(" ", raw)
    for pat in _JUNK_PATTERNS:
        t = re.sub(pat, " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -–—|·•\t")
    return t


def split_artist_title(raw: str) -> tuple[Optional[str], str]:
    """Best-effort 'Artist - Title' split on a cleaned YouTube title."""
    t = clean_title(raw)
    for sep in (" - ", " – ", " — ", " -", "- "):
        if sep in t:
            left, right = t.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right and len(left) < 60:
                return left, right
    return None, t


def strip_feat(s: str) -> str:
    return re.sub(r"\s+", " ", _FEAT.sub(" ", s)).strip()


@dataclass
class TrackMeta:
    title: str
    artist: str
    album: str = ""
    album_artist: str = ""
    track_no: Optional[int] = None
    track_total: Optional[int] = None
    disc_no: Optional[int] = None
    disc_total: Optional[int] = None
    year: str = ""
    genre: str = ""
    art_url: str = ""
    source: str = "youtube"
    score: float = 0.0
    notes: list[str] = field(default_factory=list)


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _soft_ratio(a: str, b: str) -> float:
    """Similarity that tolerates extra collaborators or subtitles.

    "AP Dhillon" vs "AP Dhillon, Gminxr & Gurinder Gill" is a match, and a
    plain SequenceMatcher ratio would score it around 0.4.
    """
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if short in long and len(short) >= 4:
        return 0.95
    return _ratio(a, b)


# Releases that are usually not the track the user asked for.
_IMPOSTER = re.compile(
    r"\b(karaoke|tribute|cover version|covers?|instrumental|lofi|lo-fi|"
    r"made famous by|in the style of|ringtone|8d audio|slowed|reverb|"
    r"flute|piano version|tabla|acoustic|live at|live in|live version|"
    r"sped up|nightcore|remix|radio edit|demo version)\b", re.I)


def _live_penalty(res: dict, query_title: str) -> float:
    """A live recording is rarely what someone asked for by plain title."""
    if re.search(r"\blive\b", query_title, re.I):
        return 0.0
    blob = f"{res.get('collectionName','')} {res.get('trackName','')}"
    return 0.30 if re.search(r"\blive\b", blob, re.I) else 0.0


def _imposter_penalty(res: dict, query_title: str) -> float:
    blob = f"{res.get('artistName','')} {res.get('collectionName','')} {res.get('trackName','')}"
    pen = 0.0
    for m in _IMPOSTER.finditer(blob):
        if m.group(0).lower() not in query_title.lower():
            pen += 0.22
    # Non-original-language re-recordings of film songs.
    if re.search(r"\(?\b(punjabi|tamil|telugu|kannada|malayalam|bengali|marathi)"
                 r"\s+version\b", blob, re.I) and \
       not re.search(r"version", query_title, re.I):
        pen += 0.25
    return min(pen, 0.6)


def _itunes(term: str, country: str, limit: int = 12) -> list[dict]:
    try:
        r = requests.get(
            ITUNES_SEARCH,
            params={"term": term, "media": "music", "entity": "song",
                    "limit": limit, "country": country},
            headers={"User-Agent": UA},
            timeout=12,
        )
        if r.status_code == 403:
            time.sleep(1.0)
            return []
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception:
        return []


# Pipe-separated title segments that are never the song name.
_SEG_NOISE = re.compile(
    r"^(?:lyrics?|lyrical|official|audio|video|hd|4k|full\s*song|"
    r"full\s*video|visualizer|slowed|reverb|house|deep\s*house|complextro|"
    r"witch\s*house|dubstep|trap|edm|drum\s*&?\s*bass|dnb|hyperpop|"
    r"future\s*bass|electronic|phonk|hip\s*hop|rap|pop|rock|t-series|"
    r"copyright\s*free\s*music|ncs(?:\s*release)?)$", re.I)


def pipe_segments(raw: str) -> list[str]:
    """Split a YouTube title on | / • / · and drop the noise segments."""
    parts = [p.strip(" -–—\t") for p in re.split(r"[|•·]", raw)]
    parts = [p for p in parts if p]
    keep = [p for p in parts if not _SEG_NOISE.match(clean_title(p) or p)]
    return keep or parts[:1]


def _candidates(raw_title: str, uploader: str, yt_track: str, yt_artist: str
                ) -> list[tuple[Optional[str], str]]:
    """Possible (artist, title) readings of a YouTube title.

    Titles arrive in every shape: "Artist - Song", "Song - Movie" (common for
    Indian film music), and "Song | Artist | Lyrical". Each plausible reading
    becomes a candidate, and the iTunes match score decides between them.
    """
    segs = pipe_segments(raw_title)
    main = segs[0] if segs else raw_title
    extra = [clean_title(x) for x in segs[1:]]

    whole = clean_title(main)
    left, right = split_artist_title(main)
    chan = re.sub(r"\s*-\s*Topic$", "", uploader or "").strip()
    chan = re.sub(r"VEVO$", "", chan, flags=re.I).strip()

    cands: list[tuple[Optional[str], str]] = []
    if yt_track:
        cands.append((yt_artist or chan or None, yt_track))
    if left and right:
        cands.append((left, right))           # "Artist - Song"
        cands.append((right, left))           # "Song - Album", read backwards
        cands.append((chan or None, left))
        cands.append((chan or None, right))
    cands.append((chan or None, whole))
    for x in extra[:2]:                        # "Song | Artist"
        if x and len(x) < 45:
            cands.append((x, whole))
    cands.append((None, whole))

    out, seen = [], set()
    for a, t in cands:
        t = strip_feat(clean_title(t or "")).strip()
        a = strip_feat((a or "")).strip() or None
        if not t or len(t) < 2:
            continue
        key = ((a or "").lower(), t.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((a, t))
    return out[:8]


def lookup(raw_title: str, uploader: str = "", duration: Optional[float] = None,
           yt_track: str = "", yt_artist: str = "", yt_album: str = "",
           country: str = "US", min_score: float = 0.62) -> TrackMeta:
    """Resolve clean metadata for one video. Falls back to parsed YouTube data."""
    cands = _candidates(raw_title, uploader, yt_track, yt_artist)
    guess_artist, guess_title = (cands[0] if cands else (None, clean_title(raw_title)))

    fallback = TrackMeta(
        title=yt_track or guess_title or clean_title(raw_title) or raw_title,
        artist=(yt_artist or guess_artist
                or re.sub(r"\s*-\s*Topic$", "", uploader) or "Unknown Artist"),
        album=yt_album or "",
        album_artist=(yt_artist or guess_artist or ""),
        source="youtube",
    )

    # The artist is known with reasonable confidence when YouTube says so, or
    # when the uploading channel agrees with the "Artist - Title" split. Any
    # result by somebody else is then the wrong recording, however well the
    # title scores - this is what matched "WILDFLOWER" to an unrelated artist.
    chan = clean_channel_name(uploader)
    split_left, _ = split_artist_title(raw_title)
    known_artist = ""
    if yt_artist:
        known_artist = yt_artist
    elif chan and split_left and _soft_ratio(chan, split_left) >= 0.6:
        known_artist = split_left
    elif chan and len(chan) > 2 and not is_label_channel(chan):
        known_artist = chan

    best: Optional[dict] = None
    best_score = 0.0
    best_cand: tuple[Optional[str], str] = (None, "")
    searched: set[str] = set()

    for cand_artist, cand_title in cands:
        queries = [f"{cand_artist} {cand_title}" if cand_artist else cand_title]
        if cand_artist:
            queries.append(cand_title)
        for q in queries:
            q = re.sub(r"\s+", " ", q).strip()
            if not q or q.lower() in searched:
                continue
            searched.add(q.lower())
            for res in _itunes(q, country):
                if known_artist and _soft_ratio(known_artist,
                                                res.get("artistName", "")) < 0.45:
                    continue
                s = _soft_ratio(cand_title, res.get("trackName", ""))
                if cand_artist:
                    s = 0.70 * s + 0.30 * _soft_ratio(cand_artist,
                                                      res.get("artistName", ""))
                if duration and res.get("trackTimeMillis"):
                    delta = abs(duration - res["trackTimeMillis"] / 1000.0)
                    if delta <= 4:
                        s += 0.10
                    elif delta <= 12:
                        s += 0.04
                    elif delta > 90:
                        s -= 0.25
                    elif delta > 40:
                        s -= 0.10
                s -= _imposter_penalty(res, cand_title)
                s -= _live_penalty(res, cand_title)
                if s > best_score:
                    best_score, best, best_cand = s, res, (cand_artist, cand_title)
        if best_score >= 0.95:
            break

    if not best or best_score < min_score:
        fallback.score = round(best_score, 3)
        fallback.notes.append("no confident iTunes match; using YouTube metadata")
        return fallback

    art = best.get("artworkUrl100") or best.get("artworkUrl60") or ""
    art = re.sub(r"/\d+x\d+bb\.(jpg|png)$", "/600x600bb.jpg", art)
    year = (best.get("releaseDate") or "")[:4]

    return TrackMeta(
        title=best.get("trackName") or fallback.title,
        artist=best.get("artistName") or fallback.artist,
        album=best.get("collectionName") or "",
        album_artist=best.get("collectionArtistName") or best.get("artistName") or "",
        track_no=best.get("trackNumber"),
        track_total=best.get("trackCount"),
        disc_no=best.get("discNumber"),
        disc_total=best.get("discCount"),
        year=year,
        genre=best.get("primaryGenreName") or "",
        art_url=art,
        source="itunes",
        score=round(best_score, 3),
    )


# ---------------------------------------------------------------- albums

ITUNES_LOOKUP = "https://itunes.apple.com/lookup"


@dataclass
class AlbumMeta:
    """One iTunes release plus its full tracklist."""
    album: str = ""
    album_artist: str = ""
    year: str = ""
    genre: str = ""
    art_url: str = ""
    track_total: int = 0
    disc_total: int = 1
    tracks: list[dict] = field(default_factory=list)
    source: str = "youtube"
    score: float = 0.0


def _itunes_album_search(term: str, country: str, limit: int = 10) -> list[dict]:
    try:
        r = requests.get(
            ITUNES_SEARCH,
            params={"term": term, "media": "music", "entity": "album",
                    "limit": limit, "country": country},
            headers={"User-Agent": UA}, timeout=12,
        )
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception:
        return []


def _itunes_album_tracks(collection_id: int, country: str) -> list[dict]:
    try:
        r = requests.get(
            ITUNES_LOOKUP,
            params={"id": collection_id, "entity": "song", "limit": 300,
                    "country": country},
            headers={"User-Agent": UA}, timeout=15,
        )
        r.raise_for_status()
        return [x for x in r.json().get("results", [])
                if x.get("wrapperType") == "track"]
    except Exception:
        return []


# Channels that are labels, aggregators or film studios rather than artists.
# Treating one of these as "the artist" makes the artist gate reject every
# genuine result - it is why a film soundtrack came back as the label.
_LABEL_CHANNEL = re.compile(
    r"\b(music|musics|records?|recordings?|entertainment|media|films?|"
    r"studios?|tunes?|audios?|movies?|label|productions?|network|digital|"
    r"cinemas?|channel|tv|official)\b", re.I)


def is_label_channel(name: str) -> bool:
    return bool(_LABEL_CHANNEL.search(name or ""))


def clean_channel_name(name: str) -> str:
    """Turn a YouTube channel name into something like an artist name."""
    n = (name or "").strip()
    n = re.sub(r"\s*-\s*Topic$", "", n)
    n = re.sub(r"VEVO$", "", n, flags=re.I)
    n = re.sub(r"^Official\s+", "", n, flags=re.I)
    n = re.sub(r"\s+Official$", "", n, flags=re.I)
    n = re.sub(r"\s*-\s*Official\s*(?:Channel)?$", "", n, flags=re.I)
    return n.strip()


def guess_album_artist(entry_titles: list[str], playlist_uploader: str = "",
                       entry_uploaders: Optional[list[str]] = None) -> str:
    """Infer the album artist from the tracks.

    YouTube Music album entries are often bare song titles with no
    "Artist - " prefix and no playlist uploader, so the channel each track
    was uploaded by is the more reliable signal.
    """
    artists = []
    for t in entry_titles:
        left, right = split_artist_title(t)
        if left and right:
            artists.append(left.strip().lower())
    if artists:
        top = max(set(artists), key=artists.count)
        if artists.count(top) >= max(2, len(artists) // 2):
            for t in entry_titles:                   # recover original casing
                left, _ = split_artist_title(t)
                if left and left.strip().lower() == top:
                    return left.strip()
    ups = [clean_channel_name(u) for u in (entry_uploaders or []) if u]
    ups = [u for u in ups if u]
    if ups:
        top = max(set(ups), key=ups.count)
        if ups.count(top) >= max(2, len(ups) // 2):
            return top

    return clean_channel_name(playlist_uploader)


def lookup_album(album_title: str, album_artist: str = "",
                 track_titles: Optional[list[str]] = None,
                 country: str = "US", min_score: float = 0.60) -> AlbumMeta:
    """Find the iTunes release matching a YouTube Music album, with tracklist.

    Matching a whole release once gives every track the same album name, year,
    cover and real track numbers - far better than looking each song up alone,
    which tends to resolve singles and compilations instead.
    """
    clean_album = clean_title(album_title)
    clean_album = re.sub(r"\s*[\(\[](?:full\s*)?album[\)\]]\s*$", "", clean_album, flags=re.I)
    empty = AlbumMeta(album=clean_album, album_artist=album_artist)
    if not clean_album:
        return empty

    terms = []
    if album_artist:
        terms.append(f"{album_artist} {clean_album}")
    terms.append(clean_album)

    best, best_score = None, 0.0
    for term in terms:
        for res in _itunes_album_search(term, country):
            t_sim = _soft_ratio(clean_album, res.get("collectionName", ""))
            # The title IS the album's identity; the artist only confirms it.
            # Without this gate a perfect artist match drags in the wrong
            # record entirely (any Pink Floyd album for "Dark Side").
            if t_sim < 0.55:
                continue
            s = t_sim
            if album_artist:
                a_sim = _soft_ratio(album_artist, res.get("artistName", ""))
                s = 0.50 * s + 0.50 * a_sim
                # A different act covering the same album title is the single
                # most common wrong answer, so a failed artist match is fatal.
                if a_sim < 0.50:
                    s -= 0.40
            if track_titles and res.get("trackCount"):
                if res["trackCount"] == len(track_titles):
                    s += 0.08
                elif abs(res["trackCount"] - len(track_titles)) > 4:
                    s -= 0.10
                # "<Album> - Single" by a cover act outranks the real record
                # on title alone; a real album has more than a few tracks.
                if len(track_titles) > 3 and res["trackCount"] <= 3:
                    s -= 0.35
            if re.search(r"\s-\s(Single|EP)$", res.get("collectionName", "")) \
                    and track_titles and len(track_titles) > 3:
                s -= 0.25
            s -= _imposter_penalty(res, clean_album)
            if s > best_score:
                best_score, best = s, res
        if best_score >= 0.95:
            break

    if not best or best_score < min_score:
        mb = lookup_album_musicbrainz(clean_album, album_artist,
                                      len(track_titles or []))
        if mb.source == "musicbrainz":
            return mb
        empty.score = round(best_score, 3)
        return empty

    raw = _itunes_album_tracks(best.get("collectionId"), country)
    art = best.get("artworkUrl100") or ""
    art = re.sub(r"/\d+x\d+bb\.(jpg|png)$", "/600x600bb.jpg", art)

    return AlbumMeta(
        album=best.get("collectionName") or clean_album,
        album_artist=best.get("artistName") or album_artist,
        year=(best.get("releaseDate") or "")[:4],
        genre=best.get("primaryGenreName") or "",
        art_url=art,
        track_total=best.get("trackCount") or len(raw),
        disc_total=max([t.get("discCount") or 1 for t in raw] or [1]),
        tracks=[{
            "title": t.get("trackName") or "",
            "artist": t.get("artistName") or "",
            "track_no": t.get("trackNumber"),
            "disc_no": t.get("discNumber") or 1,
            "duration": (t.get("trackTimeMillis") or 0) / 1000.0,
            "genre": t.get("primaryGenreName") or "",
        } for t in raw],
        source="itunes",
        score=round(best_score, 3),
    )


def match_in_album(am: AlbumMeta, raw_title: str, duration: Optional[float],
                   position: int) -> Optional[TrackMeta]:
    """Pick the album track a given YouTube video corresponds to."""
    if not am.tracks or am.source not in ("itunes", "musicbrainz"):
        return None

    cands = _candidates(raw_title, "", "", "")
    best, best_score = None, 0.0
    for t in am.tracks:
        for _, cand_title in cands:
            s = _soft_ratio(cand_title, t["title"])
            if duration and t["duration"]:
                delta = abs(duration - t["duration"])
                if delta <= 4:
                    s += 0.10
                elif delta > 45:
                    s -= 0.20
            # A YouTube Music album lists its tracks in album order, so the
            # track sitting at this position is a strong prior.
            if t.get("track_no") == position and t.get("disc_no", 1) == 1:
                s += 0.25
            if s > best_score:
                best_score, best = s, t

    # Fall back to playlist position when titles are unhelpful but the running
    # time lines up - YouTube Music albums are in track order.
    if best_score < 0.55:
        by_pos = next((t for t in am.tracks if t["track_no"] == position), None)
        if by_pos and duration and by_pos["duration"] and \
                abs(duration - by_pos["duration"]) <= 8:
            best, best_score = by_pos, 0.60
        else:
            return None

    return TrackMeta(
        title=best["title"],
        artist=best["artist"] or am.album_artist,
        album=am.album,
        album_artist=am.album_artist,
        track_no=best["track_no"],
        track_total=am.track_total,
        disc_no=best["disc_no"],
        disc_total=am.disc_total,
        year=am.year,
        genre=best["genre"] or am.genre,
        art_url=am.art_url,
        source="itunes-album",
        score=round(best_score, 3),
    )


# ------------------------------------------------- MusicBrainz (fallback)

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
# MusicBrainz requires a descriptive User-Agent and asks for <=1 req/sec.
MB_UA = "walkman-dl/1.0 (https://github.com/local/walkman-dl)"

_MB_SECONDARY_BAD = {"live", "compilation", "remix", "dj-mix", "mixtape/street",
                     "demo", "interview", "spokenword"}


def _mb_get(path: str, params: dict, attempts: int = 4) -> dict:
    """GET from MusicBrainz, retrying its 503 rate-limit responses.

    MusicBrainz throttles to roughly one request a second and answers 503
    when you go over. Swallowing that silently loses the whole album.
    """
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            r = requests.get(f"{MB_BASE}/{path}", params={**params, "fmt": "json"},
                             headers={"User-Agent": MB_UA}, timeout=20)
            if r.status_code == 503:
                raise RuntimeError("musicbrainz rate limit (503)")
            r.raise_for_status()
            time.sleep(1.1)
            return r.json()
        except Exception as exc:
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"musicbrainz unreachable: {last}")


def _caa_front(entity: str, mbid: str) -> str:
    """Cover Art Archive front image URL, if one exists."""
    url = f"{CAA_BASE}/{entity}/{mbid}/front-500"
    try:
        r = requests.head(url, allow_redirects=True, timeout=12,
                          headers={"User-Agent": MB_UA})
        return url if r.status_code == 200 else ""
    except Exception:
        return ""


def lookup_album_musicbrainz(album_title: str, album_artist: str = "",
                             n_tracks: int = 0) -> AlbumMeta:
    """Find an album on MusicBrainz.

    iTunes is missing a surprising number of catalogue records - The Dark Side
    of the Moon among them - so MusicBrainz covers what the storefront cannot.
    """
    clean_album = clean_title(album_title)
    empty = AlbumMeta(album=clean_album, album_artist=album_artist)
    if not clean_album:
        return empty

    q = f'release:"{clean_album}"'
    if album_artist:
        q += f' AND artist:"{album_artist}"'
    q += " AND status:official"
    try:
        data = _mb_get("release/", {"query": q, "limit": 25})
    except Exception:
        return empty

    best, best_score = None, 0.0
    for rel in data.get("releases", []):
        s = (rel.get("score") or 0) / 100.0
        tc = rel.get("track-count") or 0
        if n_tracks:
            if tc == n_tracks:
                s += 0.30
            elif abs(tc - n_tracks) <= 1:
                s += 0.10
            elif abs(tc - n_tracks) > 3:
                s -= 0.30
        rg = rel.get("release-group") or {}
        sec = {t.lower() for t in (rg.get("secondary-types") or [])}
        if sec & _MB_SECONDARY_BAD:
            s -= 0.35
        if (rg.get("primary-type") or "").lower() == "album":
            s += 0.05
        date = rel.get("date") or "9999"
        s -= min(int((date or "9999")[:4]) - 1900, 200) / 10000.0  # prefer the earliest press
        if s > best_score:
            best_score, best = s, rel

    if not best or best_score < 0.65:
        empty.score = round(best_score, 3)
        return empty

    try:
        full = _mb_get(f"release/{best['id']}",
                       {"inc": "recordings+artist-credits+release-groups+genres"})
    except Exception:
        return empty

    rg = full.get("release-group") or {}
    year = (rg.get("first-release-date") or full.get("date") or "")[:4]
    genres = sorted((rg.get("genres") or []),
                    key=lambda g: g.get("count", 0), reverse=True)
    genre = (genres[0]["name"].title() if genres else "")

    art = ""
    if rg.get("id"):
        art = _caa_front("release-group", rg["id"])
    if not art:
        art = _caa_front("release", full["id"])

    media = full.get("media") or []
    tracks = []
    for medium in media:
        disc = medium.get("position") or 1
        for t in medium.get("tracks") or []:
            credit = t.get("artist-credit") or \
                (t.get("recording") or {}).get("artist-credit") or []
            name = "".join((c.get("name") or "") + (c.get("joinphrase") or "")
                           for c in credit)
            tracks.append({
                "title": t.get("title") or "",
                "artist": name or album_artist,
                "track_no": t.get("position"),
                "disc_no": disc,
                "duration": (t.get("length") or 0) / 1000.0,
                "genre": genre,
            })

    artist_credit = full.get("artist-credit") or []
    album_artist_name = "".join((c.get("name") or "") + (c.get("joinphrase") or "")
                                for c in artist_credit) or album_artist

    return AlbumMeta(
        album=full.get("title") or clean_album,
        album_artist=album_artist_name,
        year=year,
        genre=genre,
        art_url=art,
        track_total=len(tracks),
        disc_total=max([m.get("position") or 1 for m in media] or [1]),
        tracks=tracks,
        source="musicbrainz",
        score=round(best_score, 3),
    )
