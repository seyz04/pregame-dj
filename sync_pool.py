#!/usr/bin/env python3
"""Re-read a Music playlist into <name>.json for the DJ. Read-only: never edits the playlist.

Usage:  ./sync_pool.py            (reads "yer " into yer.json)
        ./sync_pool.py "pregame" pregame
"""
import json, os, subprocess, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
GENRE = {"Hip-Hop/Rap": "R", "Rap": "R", "Hip-Hop": "R", "Dance": "H", "House": "H", "R&B/Soul": "M",
         "Hard Rock": "X", "Metal": "X", "Rock": "X", "Alternative": "X"}
OVERRIDE = {  # by title prefix; everything else goes by genre, defaulting to pop
    "B": ["Fancy", "Problem", "23 (feat", "URRRGE", "Hollaback Girl", "What's My Name", "1, 2 Step", "Level Up",
          "Bad (Remix)", "Why I Love You", "Turn My Swag On", "Don't Stop The Music", "S&M"],
    "C": ["I Love It", "We R Who We R", "Like a G6", "Lights (Single Version)", "Blah Blah Blah", "Party Rock Anthem",
          "I Gotta Feeling", "Give Me Everything", "Die Young"],
    "M": ["All I Need", "Pretty", "Killshot", "just a waste", "Like a taxi"],
    "H": ["Cycles", "I'm Not Alone", "I Need Your Love", "B2b", "gymnastics", "Alive Internet Theory", "Give it 2 me",
          "London Song", "Canary Wharf drift", "Confidence"],
}

def bucket(title, genre):
    return next((k for k, pre in OVERRIDE.items() if any(title.startswith(p) for p in pre)), GENRE.get(genre, "P"))


SCRIPT = '''on run argv
tell application "Music"
set out to ""
repeat with t in (get every track of user playlist (item 1 of argv))
set out to out & (persistent ID of t) & "|" & (name of t) & "|" & (artist of t) & "|" & (genre of t) & "|" & (played count of t) & linefeed
end repeat
return out
end tell
end run'''


def main():
    playlist = sys.argv[1] if len(sys.argv) > 1 else "yer "
    name = sys.argv[2] if len(sys.argv) > 2 else playlist.strip()
    r = subprocess.run(["osascript", "-", playlist], input=SCRIPT, capture_output=True, text=True)
    if r.returncode:
        sys.exit("Couldn't read the playlist: " + r.stderr.strip())
    path = os.path.join(HERE, name + ".json")
    old = {s["pid"] for s in json.load(open(path, encoding="utf-8"))} if os.path.exists(path) else set()
    pool = []
    for l in r.stdout.splitlines():
        if l.count("|") != 4:
            continue
        pid, title, artist, genre, plays = l.split("|")
        b = bucket(title, genre)
        pool.append({"pid": pid, "playlist": playlist, "name": title, "artist": artist, "genre": genre,
                     "plays": int(plays or 0), "bucket": b})
    json.dump(pool, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    new = {s["pid"] for s in pool}
    print(f"{len(pool)} songs → {name}.json  {dict(Counter(s['bucket'] for s in pool))}")
    print(f"  {len(new - old)} new, {len(old - new)} removed since last sync")
    for s in pool:
        if s["pid"] not in old:
            print(f"  + [{s['bucket']}] {s['name']} — {s['artist']} ({s['genre']})")


if __name__ == "__main__":
    main()
