# SoundCloud-CLI
Download SoundCloud tracks as MP3 from the terminal.

## Requirements

- Python 3.10+
- `curl_cffi` and `mutagen` (`pip install -r requirements.txt`)
- `ffmpeg` on PATH (only needed for tracks served as HLS)

## Run

    python soundcloud.py

    1) Search tracks
    2) Download from a link or id
    3) Quit

Pick tracks with `1`, `1,3`, `2-4`, or `a` for all.

## How it works

The client_id is read out of SoundCloud's shipped web JS on each run, with a
hardcoded fallback if that fails. Tracks come from the public api-v2 endpoints.
Most stream as a direct progressive MP3; anything that only exposes HLS is pulled
and muxed to MP3 with ffmpeg. Files land in `./downloads`, tagged with title,
artist, and cover art.

## Notes

- No login required. Track links, bare track ids, and playlist/album ids all work.
- Rate limits: hammering the API from one IP can return 404/429 or a junk 200
  body. The downloader validates the result and skips garbage instead of saving it.

