#!/usr/bin/env python3
"""Build study.json, the DJ's study-mode pool, straight from the Music library (read-only: no playlists
are created or edited). Edit the lists below and rerun to change what plays.

Lanes:  A calm (jazz, classical, piano: settling in)   F focus (steady, mostly instrumental)
        J fire jazz (Caravan energy)   E surge (epic soundtrack / Worlds anthems, the Counter Attack energy)
"""
import json, os, subprocess
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
LANES = {  # (title prefix, artist prefix or "")
    "A": [("So What", "Miles"), ("Blue In Green", "Miles"), ("Flamenco Sketches", "Miles"), ("All Blues", "Miles"),
          ("Freddie Freeloader", "Miles"), ("Générique", "Miles"), ("Blue Haze", "Miles"), ("Take Five", "Dave"),
          ("Theme From \"Mr. Broadway\"", "Dave"), ("Desmond Blue", ""), ("Ill Wind", "Paul"), ("A Taste of Honey", ""),
          ("Then I'll Be Tired of You", ""), ("Jazzabelle", ""), ("Scenery", "RYO"), ("Autumn Leaves", "RYO"),
          ("Girl Talk", "Cedar"), ("Singing the Blues", "Al Cohn"),
          ("Again", "Masaru"), ("Steep Hill", ""), ("Fletcher's Song In Club", ""), ("Concerto in D for Guitar", ""),
          ("Concerto for Lute", ""), ("Serenade in E Minor", ""), ("Piano Concerto No. 5", ""),
          ("Concerto for Violin and Orchestra No. 2", ""), ("Evening Shadows Fall", "")],
    "F": [("Music is Code you can Hear", ""), ("Airglow", "DJ_Dave"), ("Await", "DJ_Dave"), ("Hard Refresh", ""),
          ("Memory Leak", ""), ("Always Learning", ""), ("Forget", "DJ_Dave"), ("Fail-safe", "DJ_Dave"),
          ("Galactic Neighborhood", ""), ("Modularity", ""), ("Raven", "GoGo"),
          ("The Promised Neverland", ""), ("the image theme of Xenosaga II", ""), ("A Love Supreme, Pt. 1", ""),
          ("A Love Supreme, Pt. 2", "")],
    "J": [("Caravan", "John Wasson"), ("Spain", "Chick"), ("Dat Dere", "Cannonball"), ("This Here", "Bobby"),
          ("Dexterity", ""), ("A Love Supreme, Pt. 3", "")],
    "E": [("Before My Body Is Dry", ""), ("Wildfire", "HOYO"), ("Mass Destruction", ""), ("Burn My Dread", ""),
          ("Throw Away Your Mask", ""), ("Counter Strike", "Lyn"), ("Legends Never Die", ""), ("RISE", "League"),
          ("GIANTS", "True Damage"), ("Counterattack", "Kenji Hiramatsu")],
}

SCRIPT = '''tell application "Music"
set ids to persistent ID of every track of library playlist 1
set nms to name of every track of library playlist 1
set ars to artist of every track of library playlist 1
set gns to genre of every track of library playlist 1
set pcs to played count of every track of library playlist 1
set out to ""
repeat with i from 1 to (length of ids)
set out to out & (item i of ids) & "|" & (item i of nms) & "|" & (item i of ars) & "|" & (item i of gns) & "|" & (item i of pcs) & linefeed
end repeat
return out
end tell'''


def main():
    r = subprocess.run(["osascript", "-"], input=SCRIPT, capture_output=True, text=True, check=True)
    lib = [l.split("|") for l in r.stdout.splitlines() if l.count("|") == 4]
    pool, seen = [], set()
    for lane, picks in LANES.items():
        for title, artist in picks:
            hits = [t for t in lib if t[1].startswith(title) and t[2].startswith(artist)]
            if not hits:
                print(f"  not in library: {title}")
            for pid, name, art, genre, plays in hits[:1]:
                if pid not in seen:
                    seen.add(pid)
                    pool.append({"pid": pid, "playlist": None, "name": name, "artist": art, "genre": genre,
                                 "plays": int(plays or 0), "bucket": lane})
    json.dump(pool, open(os.path.join(HERE, "study.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{len(pool)} songs → study.json  {dict(Counter(s['bucket'] for s in pool))}")


if __name__ == "__main__":
    main()
