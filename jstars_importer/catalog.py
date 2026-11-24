"""Disc catalog: scans the ISO once and keeps a persistent, gzip'd JSON index.

No bpy in here, so the scan can run on a worker thread and be unit-tested
outside Blender.

Index layout::

    {
      "version": N,
      "iso": {"path", "size", "mtime"},
      "paks": {pak_path: [abs_offset, size]},          # every CPK entry
      "contents": {pak_path: [[entry, size], ...]},    # listed packs only
      "models": [...], "stages": [...], "motion_sets": [...]
    }
"""
import gzip
import json
import os
import re
import threading
import time
from collections import OrderedDict

from .formats import cmp as _cmp
from .formats import cpk as _cpk
from .formats import iso as _iso
from .formats import stpk as _stpk

CACHE_VERSION = 3
DATA_DIR = "/PS3_GAME/USRDIR/data/PS3/"

# Packs whose contents get listed during a scan (everything else is only indexed).
_LIST_RX = re.compile(
    r"^(character/(model|main_mode_model|battle_character|battle_character_assist(_skill)?/\d+)/"
    r"|map/(battle_map|battle_map_gimmick)/"
    r"|progress/worldmap/(map|ship|symbol|animal|sky)/)")

_SKIP_SRD = ("_shader.srd", "_texture.srd", "_ability.srd", "_lightmap_texture.srd",
             "_locator.srd", "_positioning.srd")

# Official US names (game_resident_US: K_MapName_US/MapName_NNN_US.txt), keyed by
# the mapNN pack number. The MapName table is in menu order, not pack order; the
# pairing below comes from each pack's stage-select card (progress_map_img_NN)
# and texture set (map01 k_* Konoha, map02 ara_* Alabasta, map04 p_* Penguin ...).
STAGE_NAMES = {
    0: "Planet Namek", 1: "Hidden Leaf Village", 2: "Alabasta", 3: "Vegetable Sky",
    4: "Penguin Village", 5: "Kameari Police Station", 6: "Temple of Athena",
    7: "Soul Society", 8: "Inferno Crags", 9: "Dark Tournament Stadium",
    10: "Tenraikyousintojo", 11: "Yoshiwara Paradise",
    99: "Developer test stage",
}


def _companion(pak, kind):
    """battle_x_m.pak -> battle_x_i.pak / battle_x_v.pak"""
    return re.sub(r"_m\.pak$", "_%s.pak" % kind, pak)


def pretty(s):
    s = re.sub(r"_PS3$", "", s)
    return s.replace("_", " ")


class Catalog:
    def __init__(self):
        self.data = None
        self.path = None          # ISO path the index belongs to
        self._src = None
        self._lock = threading.Lock()
        self._pak_cache = OrderedDict()   # pak path -> {entry: bytes}
        self.pak_cache_limit = 12

    # ------------------------------------------------------------------ io
    @property
    def ready(self):
        return self.data is not None

    def load(self, cache_file):
        try:
            with gzip.open(cache_file, "rt", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return False
        if d.get("version") != CACHE_VERSION:
            return False
        self.data = d
        # Lists and labels are derived from the raw pack listing, so naming
        # fixes apply to an existing index without a rescan.
        self._derive()
        self.path = d["iso"]["path"]
        self._close()
        return True

    def save(self, cache_file):
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp = cache_file + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(self.data, f, separators=(",", ":"))
        os.replace(tmp, cache_file)

    def is_stale(self):
        """True if the ISO moved or changed since the scan."""
        if not self.data:
            return True
        info = self.data["iso"]
        try:
            st = os.stat(info["path"])
        except OSError:
            return True
        return st.st_size != info["size"] or int(st.st_mtime) != info["mtime"]

    def _source(self):
        if self._src is None:
            self._src = _iso.open_source(self.path)
        return self._src

    def _close(self):
        if self._src is not None:
            self._src.close()
            self._src = None
        self._pak_cache.clear()

    def clear_memory(self):
        with self._lock:
            self._pak_cache.clear()

    # ------------------------------------------------------------------ scan
    def scan(self, iso_path, progress=None, cancel=None):
        """Build a fresh index. progress(fraction, message); cancel() -> bool."""
        def report(f, msg):
            if progress:
                progress(f, msg)

        t0 = time.time()
        src = _iso.open_source(iso_path)
        try:
            st = os.stat(iso_path)
            paks = {}
            report(0.0, "Reading archive tables")
            cpks = [e for e in src.files()
                    if e.path.upper().startswith(DATA_DIR.upper()) and e.path.lower().endswith(".cpk")]
            for e in cpks:
                base = e.extents[0][0] * _iso.SECTOR if isinstance(src, _iso.IsoReader) else 0
                try:
                    files = _cpk.read_cpk_toc(lambda o, n, e=e: src.read(e, o, n))
                except ValueError:
                    continue
                for f in files:
                    if isinstance(src, _iso.IsoReader):
                        paks[f.path] = [base + f.offset, f.size]
                    else:
                        paks[f.path] = [e.path + "@" + str(f.offset), f.size]
            # loose .pak files outside CPKs (install/, etc.)
            for e in src.files():
                p = e.path
                if p.upper().startswith(DATA_DIR.upper()) and p.lower().endswith(".pak"):
                    rel = p[len(DATA_DIR):]
                    if isinstance(src, _iso.IsoReader):
                        paks[rel] = [e.extents[0][0] * _iso.SECTOR, e.size]

            todo = [p for p in sorted(paks) if _LIST_RX.match(p)
                    and not re.search(r"_(i|v)\.pak$", p) and paks[p][1] > 0]
            contents = {}
            raw = open(iso_path, "rb") if isinstance(src, _iso.IsoReader) else None
            for i, p in enumerate(todo):
                if cancel and cancel():
                    return False
                if i % 8 == 0:
                    report(0.05 + 0.9 * i / max(1, len(todo)), "Listing %s" % p.rsplit("/", 1)[-1])
                try:
                    blob = self._read_raw(src, raw, paks[p])
                    blob = _cmp.decode(blob)
                    if not _stpk.is_stpk(blob):
                        continue
                    items = [[name, n] for name, n in _stpk.list_tree(blob)
                             if name.lower().endswith((".srd", ".srdi", ".srdv", ".san"))]
                except (ValueError, IndexError):
                    continue
                if items:
                    contents[p] = items
            if raw:
                raw.close()
        finally:
            src.close()

        report(0.97, "Building lists")
        self.data = {
            "version": CACHE_VERSION,
            "iso": {"path": iso_path, "size": st.st_size, "mtime": int(st.st_mtime)},
            "scanned": time.strftime("%Y-%m-%d %H:%M"),
            "scan_seconds": round(time.time() - t0, 1),
            "paks": paks,
            "contents": contents,
        }
        self._derive()
        self.path = iso_path
        self._close()
        report(1.0, "Done")
        return True

    @staticmethod
    def _read_raw(src, raw, loc):
        off, size = loc
        if raw is not None:
            raw.seek(off)
            return raw.read(size)
        cpk_path, rel = off.split("@")
        e = src.find(cpk_path)
        return src.read(e, int(rel), size)

    # ------------------------------------------------------------------ lists
    def _derive(self):
        d = self.data
        contents = d["contents"]
        models, stages, motion_sets = [], [], []
        for pak, items in sorted(contents.items()):
            names = {n: s for n, s in items}
            # ---- models (characters, supports, world-map props)
            if pak.startswith(("character/model/", "character/main_mode_model/",
                               "character/battle_character_assist/", "progress/worldmap/")):
                for n, s in items:
                    low = n.lower()
                    if not low.endswith(".srd") or s == 0 or low.endswith(_SKIP_SRD) or "/" in n:
                        continue
                    if "_facial" in low:
                        continue
                    base = n[:-4]
                    kind = ("Battle" if pak.startswith("character/model/") else
                            "J-Adventure" if pak.startswith("character/main_mode_model/") else
                            "Support" if "assist" in pak else "World Map")
                    m = re.match(r"(\d{3})_", base)
                    models.append({
                        "key": pak + "|" + base,
                        "pak": pak, "srd": n, "base": base,
                        "id": int(m.group(1)) if m else -1,
                        "kind": kind,
                        "label": pretty(base),
                    })
            # ---- stages
            m = re.match(r"map/battle_map/map(\d\d)(_2p)?_m\.pak$", pak)
            if m:
                sid = int(m.group(1))
                two = bool(m.group(2))
                stages.append({
                    "key": pak,
                    "id": sid, "split": two,
                    "pak": pak,
                    "param": pak.replace("_m.pak", "_param_m.pak"),
                    "label": "%02d %s%s" % (sid, STAGE_NAMES.get(sid, "Stage"), " (split screen)" if two else ""),
                    "parts": [n for n, s in items if n.lower().endswith(".srd") and s
                              and not n.lower().endswith(_SKIP_SRD)],
                })
            m = re.match(r"map/battle_map_gimmick/(.+)_m\.pak$", pak)
            if m:
                srds = [n for n, s in items if n.lower().endswith(".srd") and s and not n.lower().endswith(_SKIP_SRD)]
                if srds:
                    stages.append({"key": pak, "id": 100, "split": False, "pak": pak, "param": "",
                                   "label": "Gimmick: " + pretty(m.group(1)), "parts": srds})
            # ---- motion sets
            sans = [n for n, s in items if n.lower().endswith(".san") and s]
            if sans:
                cid = re.search(r"_(\d{3})(?:_\d\d)?_m\.pak$", pak) or re.search(r"/(\d{3})/", pak)
                kind = ("Fighter" if pak.startswith("character/battle_character/") else
                        "Support skill" if "assist_skill" in pak else
                        "Support" if "assist" in pak else
                        "Stage" if pak.startswith("map/") else "Other")
                motion_sets.append({
                    "key": pak, "pak": pak, "kind": kind,
                    "id": int(cid.group(1)) if cid else -1,
                    "label": "%s %s" % (kind, pak.rsplit("/", 1)[-1][:-4]),
                    "motions": sorted(sans),
                })
        d["models"] = models
        d["stages"] = stages
        d["motion_sets"] = motion_sets

    def models(self):
        return self.data["models"] if self.data else []

    def stages(self):
        return self.data["stages"] if self.data else []

    def motion_sets(self):
        return self.data["motion_sets"] if self.data else []

    def motion_sets_for(self, char_id):
        return [m for m in self.motion_sets() if m["id"] == char_id]

    # ------------------------------------------------------------------ data
    def pak_entries(self, pak):
        """Decompressed {entry_path: bytes} for a pak (LRU cached in memory)."""
        with self._lock:
            hit = self._pak_cache.get(pak)
            if hit is not None:
                self._pak_cache.move_to_end(pak)
                return hit
            loc = self.data["paks"].get(pak) if self.data else None
            if not loc or loc[1] == 0:
                return {}
            src = self._source()
            if isinstance(src, _iso.IsoReader):
                src.fp.seek(loc[0])
                blob = src.fp.read(loc[1])
            else:
                blob = self._read_raw(src, None, loc)
            blob = _cmp.decode(blob)
            entries = dict(_stpk.walk(blob)) if _stpk.is_stpk(blob) else {pak: blob}
            self._pak_cache[pak] = entries
            while len(self._pak_cache) > self.pak_cache_limit:
                self._pak_cache.popitem(last=False)
            return entries

    def entry(self, pak, name):
        return self.pak_entries(pak).get(name)

    def srd_triplet(self, pak, srd_name):
        """Return (srd, srdi, srdv) bytes for an .srd inside a _m pak."""
        srd = self.entry(pak, srd_name)
        base = srd_name[:-4]
        srdi = srdv = None
        for kind, ext in (("i", ".srdi"), ("v", ".srdv")):
            other = _companion(pak, kind)
            ents = self.pak_entries(other) if other != pak else {}
            cand = ents.get(base + ext)
            if cand is None:
                # 32-char name truncation inside STPK: match by prefix
                stem = (base + ext)[:32]
                for k, v in ents.items():
                    if k.rsplit("/", 1)[-1] == stem.rsplit("/", 1)[-1]:
                        cand = v
                        break
            if kind == "i":
                srdi = cand
            else:
                srdv = cand
        return srd, srdi, srdv


CATALOG = Catalog()
