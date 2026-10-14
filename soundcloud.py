"""SoundCloud downloader - pulls tracks as MP3 from the public web API.

The client_id is scraped from the site's shipped JS each run, with a hardcoded
fallback. No auth, no CORS proxy, no DRM.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

from curl_cffi import requests

HOST = "https://api-v2.soundcloud.com"
RESOLVE_HOST = "https://api.soundcloud.com"  # /resolve is v1; 404 on api-v2
WEB = "https://soundcloud.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# last-known-good client_id; used only if the live scrape below fails (SC rotates it)
FALLBACK_CLIENT_ID = "kR7dAfT5UXQgmgde1pLjfsRGiaoANLvT"
CLIENT_ID_RE = re.compile(r'client_id["\']?\s*:\s*["\']([0-9a-zA-Z]{32})')

DOWNLOAD_DIR = Path("downloads")
SEARCH_LIMIT = 15
CHUNK = 64 * 1024
ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*"})
CLIENT_ID = None


def client_id():
    """Scrape the current client_id from SoundCloud's JS, fall back if offline."""
    global CLIENT_ID
    if CLIENT_ID:
        return CLIENT_ID
    try:
        html = SESSION.get(WEB + "/", timeout=30).text
        for u in re.findall(r'src="([^"]+\.js)"', html):
            u = ("https:" + u if u.startswith("//")
                 else WEB + u if u.startswith("/") else u)
            try:
                t = SESSION.get(u, headers={"Referer": WEB + "/"}, timeout=30).text
            except Exception:
                continue
            m = CLIENT_ID_RE.search(t)
            if m:
                CLIENT_ID = m.group(1)
                break
    except Exception:
        pass
    if not CLIENT_ID:
        CLIENT_ID = FALLBACK_CLIENT_ID
    return CLIENT_ID


def api(path, **params):
    params["client_id"] = client_id()
    response = SESSION.get(HOST + path, params=params,
                           headers={"Origin": WEB, "Referer": WEB + "/"}, timeout=30)
    response.raise_for_status()
    return response.json()


def to_track(t):
    art = (t.get("artwork_url") or "").replace("-large", "-t500x500") \
        or (t.get("user") or {}).get("avatar_url", "")
    return {
        "id": f"sc:{t['id']}",
        "sc_id": t["id"],
        "title": t.get("title") or "Unknown",
        "artist": (t.get("user") or {}).get("username") or "SoundCloud",
        "artwork": art,
        "duration": int((t.get("duration") or 0) / 1000),
        "permalink": t.get("permalink_url") or "",
        "play_count": t.get("playback_count") or 0,
    }


def search_tracks(query, limit=SEARCH_LIMIT):
    data = api("/search/tracks", q=query, limit=limit)
    return [to_track(t) for t in data.get("collection", [])]


def track(sc_id):
    return to_track(api(f"/tracks/{sc_id}"))


def resolve(value):
    """Turn a link or id into a list of tracks. Handles track / playlist / album."""
    value = value.strip().strip("'\"")
    if "soundcloud.com" in value or value.startswith("http"):
        response = SESSION.get(RESOLVE_HOST + "/resolve",
                               params={"url": value, "client_id": client_id()},
                               headers={"Origin": WEB, "Referer": WEB + "/"}, timeout=30)
        response.raise_for_status()
        data = response.json()
        kind = data.get("kind")
        if kind == "track":
            return [to_track(data)]
        if kind in ("playlist", "album", "set"):
            tracks = data.get("tracks") or []
            if not tracks and data.get("id"):
                tracks = api(f"/playlists/{data['id']}/tracks",
                             limit=200).get("collection", [])
            return [to_track(t) for t in tracks]
        return []
    digits = re.findall(r"\d{6,}", value)
    if digits:
        return [track(digits[-1])]
    return []


def _resolve(transcoding):
    """Resolve a transcoding to its playable url (retry once on 404/429)."""
    url = transcoding["url"]
    for attempt in range(2):
        response = SESSION.get(url, params={"client_id": client_id()},
                               headers={"Origin": WEB, "Referer": WEB + "/"}, timeout=30)
        if response.status_code in (404, 429) and attempt == 0:
            continue
        response.raise_for_status()
        return response.json().get("url")
    return None


def stream_url(sc_id):
    """Return (kind, url) for the best stream, trying progressive then HLS.

    Tracks expose several transcodings; we try progressive-mp3, hls-mp3, then
    the rest, using the first that resolves. A throttled resolve can 404, so we
    retry once and fall through to the next type instead of failing outright.
    """
    data = api(f"/tracks/{sc_id}")
    transcodings = (data.get("media") or {}).get("transcodings") or []
    if not transcodings:
        raise RuntimeError("no stream available (SoundCloud Go+ only?)")

    def pick(protocol, mime_prefix=None):
        for x in transcodings:
            fmt = x.get("format") or {}
            if fmt.get("protocol") != protocol:
                continue
            if mime_prefix and not fmt.get("mime_type", "").startswith(mime_prefix):
                continue
            return x
        return None

    order = [pick("progressive", "audio/mpeg"), pick("hls", "audio/mpeg"),
             pick("progressive"), pick("hls")]
    order = [x for x in order if x]
    if not order:
        raise RuntimeError("no downloadable transcoding found")
    last_err = None
    for tc in order:
        try:
            url = _resolve(tc)
            if not url:
                continue
            kind = "hls" if (tc.get("format") or {}).get("protocol") == "hls" else "progressive"
            return kind, url
        except Exception as err:  # noqa: BLE001 - try next stream type
            last_err = err
    raise RuntimeError(f"all stream resolves failed (last: {last_err})")


def safe_name(value):
    return re.sub(r"\s+", " ", ILLEGAL.sub("", value)).strip(" .")[:120] or "track"


def unique_path(path):
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def clock(seconds):
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds else "--:--"


def tag(path, track):
    try:
        from mutagen.id3 import ID3, APIC, TIT2, TPE1, TCON
        from mutagen.mp3 import MP3
    except ImportError:
        return
    try:
        m = MP3(path, ID3=ID3)
        if m.tags is None:
            m.tags = ID3()
        m.tags.add(TIT2(encoding=3, text=track["title"]))
        m.tags.add(TPE1(encoding=3, text=track["artist"]))
        m.tags.add(TCON(encoding=3, text="SoundCloud"))
        if track.get("artwork"):
            try:
                pic = SESSION.get(track["artwork"], timeout=30).content
                if pic:
                    m.tags.add(APIC(encoding=3, mime="image/jpeg",
                                    type=3, desc="cover", data=pic))
            except Exception:
                pass
        m.save(path)
    except Exception:
        return


def download_hls(m3u8_url, path):
    """Pull an HLS/fMP4 playlist into a single MP3 via ffmpeg (AAC -> mp3)."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found on PATH (required for HLS tracks)")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", m3u8_url,
           "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(path)]
    rc = subprocess.run(cmd).returncode
    if rc != 0 or not path.exists() or path.stat().st_size < 1024:
        if path.exists():
            path.unlink()
        raise RuntimeError("ffmpeg failed to pull HLS stream")
    return path


def download(track, outdir=DOWNLOAD_DIR, with_cover=True):
    kind, url = stream_url(track["sc_id"])
    outdir.mkdir(exist_ok=True)
    name = safe_name(f"{track['artist']} - {track['title']}")
    path = unique_path(outdir / f"{name}.mp3")
    if kind == "hls":
        print("  [hls] pulling stream ...")
        download_hls(url, path)
    else:
        response = SESSION.get(url, timeout=120, stream=True)
        if response.status_code != 200:
            raise RuntimeError(f"GET stream -> {response.status_code}")
        ctype = (response.headers.get("content-type") or "").lower()
        if ctype and "audio" not in ctype and "octet-stream" not in ctype:
            raise RuntimeError(f"stream not audio (content-type: {ctype})")
        total = int(response.headers.get("content-length") or 0)
        done = 0
        with open(path, "wb") as handle:
            for chunk in response.iter_content(CHUNK):
                handle.write(chunk)
                done += len(chunk)
                if total:
                    sys.stdout.write(f"\r  {done * 100 // total:3d}%  {done // 1024} KB")
                    sys.stdout.flush()
        print()
        # Guard against a non-MP3 body (e.g. a rate-limit / error page as 200).
        head = open(path, "rb").read(4)
        if head[:3] != b"ID3" and head[:2] not in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
            path.unlink()
            raise RuntimeError("stream did not return a valid MP3 (rate-limited?)")
    if with_cover:
        tag(path, track)
    return path


# ---- interactive layer ------------------------------------------------------

def show(tracks):
    print(f"\n  {'#':>3}  {'Title':<40} {'Artist':<26} {'Time':>5}")
    print("  " + "-" * 78)
    for index, t in enumerate(tracks, 1):
        print(f"  {index:>3}  {t['title'][:40]:<40} {t['artist'][:26]:<26} "
              f"{clock(t['duration']):>5}")


def prompt(question):
    try:
        return input(question)
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0) from None


def choose(tracks):
    answer = prompt("  Select: 1 / 1,3 / 2-4 / a=all / Enter=back : ").strip().lower()
    if answer in ("a", "all", "*"):
        return tracks
    picked = set()
    for part in answer.replace(" ", "").split(","):
        if "-" in part:
            low, _, high = part.partition("-")
            if low.isdigit() and high.isdigit():
                picked.update(range(int(low), int(high) + 1))
        elif part.isdigit():
            picked.add(int(part))
    return [tracks[i - 1] for i in sorted(picked) if 1 <= i <= len(tracks)]


def fetch(tracks):
    if not tracks:
        print("  nothing playable found")
        return
    show(tracks)
    picked = choose(tracks)
    if not picked:
        return
    if len(picked) > 20 and prompt(
            f"  Download {len(picked)} tracks? [y/N]: ").strip().lower() not in ("y", "yes"):
        return
    for t in picked:
        print(f"\n  {t['title']} - {t['artist']}")
        try:
            print(f"  saved -> {download(t)}")
        except Exception as error:
            print(f"  skipped: {error}")


def LastPerson07():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n  LastPerson07  |  SoundCloud downloader   (files land in ./downloads)")
    while True:
        print("\n  1) Search tracks\n  2) Download from link or id"
              "\n  3) Quit")
        choice = prompt("  > ").strip().lower()
        if choice == "1":
            query = prompt("  Search: ").strip()
            if query:
                fetch(search_tracks(query))
        elif choice == "2":
            for value in prompt("  Link(s) or id(s), space separated: ").strip().split():
                songs = resolve(value)
                if songs:
                    fetch(songs)
                else:
                    print(f"  nothing found for {value} (dead link? try searching by name)")
        elif choice in ("3", "q", "quit", "exit"):
            print("  bye\n")
            return
        elif choice:
            print("  unknown option")


if __name__ == "__main__":
    LastPerson07()
