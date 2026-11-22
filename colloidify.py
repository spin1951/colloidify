#!/usr/bin/env python3
"""
colloidify — give app icons that Colloid doesn't ship a Colloid-style icon.

  * Icon already has a solid/gradient background (square, circle, rounded…)
      -> background is extended and reshaped into the Colloid squircle.
  * Icon is a bare glyph/logo on transparency
      -> logo is placed on a white Colloid tile (dark tile if the logo is white).

The new icon is written into a per-user overlay of every installed Colloid
variant (~/.local/share/icons/Colloid*/…), under the SAME icon name the app
already uses, so the app menu picks it up without touching system files.
If a .desktop file points at an absolute icon path, a user override of that
.desktop file is created in ~/.local/share/applications with the new name.

Usage:
  colloidify.py artcraft                 # match by app name / desktop id
  colloidify.py artcraft --source ~/Downloads/logo.png   # use your own image
  colloidify.py --all --dry-run          # list every app Colloid is missing
  colloidify.py --all                    # theme all of them
  colloidify.py --list                   # what colloidify has changed
  colloidify.py --restore artcraft       # undo
  colloidify.py --ignore steam           # never touch this app (blacklist)
  colloidify.py --unignore steam         # remove it from the blacklist
  colloidify.py --show-ignored           # print the blacklist
Blacklist file: ~/.config/colloidify/blacklist (editable by hand, wildcards ok)
Options for tuning: --zoom, --glyph-size, --bg, --force

Requires: python-pillow, librsvg (rsvg-convert)   ->  sudo pacman -S python-pillow librsvg
"""

import argparse
import base64
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from PIL import Image, ImageStat
except ImportError:
    sys.exit("colloidify needs Pillow:  sudo pacman -S python-pillow")

# ---------------------------------------------------------------- paths ---

HOME = Path.home()
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local/share")
DATA_DIRS = [Path(p) for p in
             (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if p]
FLATPAK = [DATA_HOME / "flatpak/exports/share", Path("/var/lib/flatpak/exports/share")]
STATE = DATA_HOME / "colloidify"
MANIFEST = STATE / "manifest.json"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
BLACKLIST = CONFIG_HOME / "colloidify" / "blacklist"
PREFIX = "colloidify-"  # name used when an app has an absolute-path icon

# Colloid geometry (64x64 canvas), taken from the theme's own app icons.
TILE_X, TILE_Y, TILE_S, TILE_R = 4.0, 3.969, 56.002, 13.002
SCALE = 16  # raster resolution of the embedded artwork: 64 * 16 = 1024 px

SHADE = ("M3.998 45.998v1c0 7.203 5.8 13.004 13.004 13.004h29.996c7.203 0 13.004-5.8 "
         "13.004-13.004v-1c0 7.203-5.8 13.002-13.004 13.002H17.002A12.975 12.975 0 0 1 3.998 45.998")
HIGHLIGHT = ("M3.998 18.004v-1C3.998 9.801 9.798 4 17.002 4h29.996c7.203 0 13.004 5.8 13.004 "
             "13.004v1c0-7.203-5.8-13.002-13.004-13.002H17.002A12.975 12.975 0 0 0 3.998 18.004")


def data_dirs():
    out = []
    for d in [DATA_HOME, *DATA_DIRS, *FLATPAK]:
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def icon_base_dirs():
    out = [HOME / ".icons"] + [d / "icons" for d in data_dirs()]
    return [d for d in out if d.is_dir()]


# ------------------------------------------------------------ manifest ---

def load_manifest():
    try:
        return json.loads(MANIFEST.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_manifest(m):
    STATE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, indent=2))


def our_files(manifest):
    return {f for e in manifest.values() for f in e.get("files", [])}


# ----------------------------------------------------------- blacklist ---
# One entry per line in ~/.config/colloidify/blacklist. An entry can be a
# desktop id (org.gnome.Foo or org.gnome.Foo.desktop), the app's Name, or its
# Icon name, matched case-insensitively. Shell wildcards work: org.kde.*

def load_blacklist():
    try:
        lines = BLACKLIST.read_text().splitlines()
    except FileNotFoundError:
        return []
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def save_blacklist(entries):
    BLACKLIST.parent.mkdir(parents=True, exist_ok=True)
    BLACKLIST.write_text(
        "# Apps colloidify never touches. One per line: desktop id, app name or\n"
        "# icon name (case-insensitive). Wildcards allowed, e.g. org.kde.*\n"
        + "".join(e + "\n" for e in entries))


def blacklisted(did, entry, blacklist):
    keys = {did.lower(), did.lower().removesuffix(".desktop"),
            entry.get("Name", "").lower(), entry.get("Icon", "").lower()}
    keys.discard("")
    return next((pat for pat in blacklist
                 if any(fnmatch.fnmatchcase(k, pat.lower()) for k in keys)), None)


# ------------------------------------------------------- desktop files ---

def read_entry(path):
    """Return the [Desktop Entry] keys of a .desktop file as a dict."""
    entry, section = {}, None
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            section = line
            continue
        if section == "[Desktop Entry]" and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            entry.setdefault(k.strip(), v.strip())
    return entry


def all_desktops():
    """desktop-id -> path, earlier data dirs win (user overrides first)."""
    found = {}
    for d in data_dirs():
        appdir = d / "applications"
        if not appdir.is_dir():
            continue
        for f in sorted(appdir.rglob("*.desktop")):
            did = str(f.relative_to(appdir)).replace("/", "-")
            found.setdefault(did, f)
    return found


def find_desktops(query):
    p = Path(query).expanduser()
    if p.suffix == ".desktop" and p.is_file():
        return {p.name: p}
    found = all_desktops()
    q = query.lower().removesuffix(".desktop")
    exact = {did: f for did, f in found.items() if did.lower().removesuffix(".desktop") == q}
    if exact:
        return exact
    hits = {}
    for did, f in found.items():
        e = read_entry(f) or {}
        if q in did.lower() or q in e.get("Name", "").lower() or q == e.get("Icon", "").lower():
            hits[did] = f
    return hits


def visible(entry):
    return (entry.get("Type", "Application") == "Application"
            and entry.get("NoDisplay", "false").lower() != "true"
            and entry.get("Hidden", "false").lower() != "true"
            and entry.get("Icon"))


# -------------------------------------------------------- icon themes ---

def parse_index(path):
    """Minimal index.theme parser: {section: {key: value}}."""
    sections, cur = {}, None
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = sections.setdefault(line[1:-1], {})
        elif cur is not None and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            cur[k.strip()] = v.strip()
    return sections


def colloid_themes():
    """{theme name: scalable apps subdir} for every installed Colloid variant."""
    themes = {}
    for base in icon_base_dirs():
        for tdir in sorted(base.glob("Colloid*")):
            name = tdir.name
            if name in themes or not (tdir / "index.theme").is_file():
                continue
            idx = parse_index(tdir / "index.theme")
            dirs = idx.get("Icon Theme", {}).get("Directories", "").split(",")
            dirs += idx.get("Icon Theme", {}).get("ScaledDirectories", "").split(",")
            best = None
            for d in filter(None, (x.strip() for x in dirs)):
                sec = idx.get(d, {})
                if sec.get("Context") == "Applications" and sec.get("Type") == "Scalable":
                    if best is None or "scalable" in d.lower():
                        best = d
            themes[name] = best or "scalable/apps"
    return themes


def colloid_icon_names(themes, ignore):
    """Every icon name shipped by Colloid (any size/context), minus our own files."""
    names = set()
    for base in icon_base_dirs():
        for t in themes:
            tdir = base / t
            if not tdir.is_dir():
                continue
            for root, _dirs, files in os.walk(tdir, followlinks=True):
                for fn in files:
                    full = os.path.join(root, fn)
                    if full in ignore:
                        continue
                    stem, ext = os.path.splitext(fn)
                    if ext in (".svg", ".png", ".xpm"):
                        names.add(stem)
    return names


def find_source_icon(name):
    """Locate the app's own icon (hicolor / pixmaps / flatpak), biggest first."""
    p = Path(name).expanduser()
    if p.is_absolute():
        return p if p.is_file() else None
    cands = []
    for d in data_dirs():
        hic = d / "icons" / "hicolor"
        if hic.is_dir():
            cands += [f for f in hic.glob("*/apps/*") if f.stem == name]
        pix = d / "pixmaps"
        if pix.is_dir():
            cands += [f for f in pix.glob("*") if f.stem == name]
    cands = [f for f in cands
             if f.suffix.lower() in (".svg", ".svgz", ".png", ".xpm") and "symbolic" not in str(f)]

    def rank(f):
        if f.suffix.lower() in (".svg", ".svgz"):
            return 10 ** 6
        m = re.match(r"(\d+)x\d+", f.parent.parent.name)
        size = int(m.group(1)) if m else 0
        if not size:
            try:
                size = Image.open(f).size[0]
            except Exception:
                size = 1
        return size

    return max(cands, key=rank) if cands else None


# ------------------------------------------------------------- imaging ---

def load_image(path, px=1024):
    if path.suffix.lower() in (".svg", ".svgz"):
        rsvg = shutil.which("rsvg-convert")
        if not rsvg:
            sys.exit("colloidify needs rsvg-convert:  sudo pacman -S librsvg")
        res = subprocess.run([rsvg, "-w", str(px), "-h", str(px), "-a", str(path)],
                             capture_output=True)
        if res.returncode:
            raise RuntimeError(res.stderr.decode(errors="replace").strip())
        img = Image.open(io.BytesIO(res.stdout))
    else:
        img = Image.open(path)
    return img.convert("RGBA")


def band_color(rgb, mask, bbox, fx0, fy0, fx1, fy1):
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    box = (int(x0 + fx0 * w), int(y0 + fy0 * h), max(int(x0 + fx1 * w), int(x0 + fx0 * w) + 1),
           max(int(y0 + fy1 * h), int(y0 + fy0 * h) + 1))
    m = mask.crop(box)
    if not m.histogram()[255]:
        return None
    return tuple(int(v) for v in ImageStat.Stat(rgb.crop(box), m).median)


def dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def luminance(c):
    return (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255


def hexc(c):
    return "#%02x%02x%02x" % tuple(c[:3])


def parse_color(s):
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def analyse(img):
    alpha = img.getchannel("A")
    mask = alpha.point(lambda v: 255 if v > 200 else 0)
    bbox = mask.getbbox()
    if not bbox:
        raise RuntimeError("icon is fully transparent")
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    coverage = mask.crop(bbox).histogram()[255] / (w * h)
    squareish = min(w, h) / max(w, h) >= 0.85
    rgb = img.convert("RGB")
    info = {"bbox": bbox, "coverage": coverage}

    bands = {
        "top": band_color(rgb, mask, bbox, .30, .05, .70, .11),
        "bottom": band_color(rgb, mask, bbox, .30, .89, .70, .95),
        "left": band_color(rgb, mask, bbox, .05, .30, .11, .70),
        "right": band_color(rgb, mask, bbox, .89, .30, .95, .70),
    }
    info["has_bg"] = coverage >= 0.72 and squareish and all(bands.values())
    if info["has_bg"]:
        t, b = bands["top"], bands["bottom"]
        if dist(t, b) > 18:
            info["fill"] = ("gradient", t, b)
        else:
            avg = tuple(sorted(v[i] for v in bands.values())[1] for i in range(3))
            info["fill"] = ("solid", avg)
    else:
        info["glyph_lum"] = luminance(ImageStat.Stat(rgb, mask).mean)
    return info


def compose(img, info, zoom, glyph_size):
    """Place the artwork on a 1024px canvas aligned with the Colloid tile."""
    C = 64 * SCALE
    x0, y0, x1, y1 = info["bbox"]
    w, h = x1 - x0, y1 - y0
    art = img.crop(info["bbox"])
    if info["has_bg"]:
        s = TILE_S * SCALE * zoom / min(w, h)        # background covers the tile
    else:
        s = glyph_size * SCALE * zoom / max(w, h)    # glyph sits inside the tile
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    art = art.resize((nw, nh), Image.LANCZOS)
    cx, cy = (TILE_X + TILE_S / 2) * SCALE, (TILE_Y + TILE_S / 2) * SCALE
    canvas = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    canvas.paste(art, (round(cx - nw / 2), round(cy - nh / 2)))
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def build_svg(png_b64, fill):
    if fill[0] == "gradient":
        top, bot = fill[1], fill[2]
        defs_fill = (f'<linearGradient id="g" x1="0" x2="0" y1="0" y2="1">'
                     f'<stop offset="0" stop-color="{hexc(top)}"/>'
                     f'<stop offset="1" stop-color="{hexc(bot)}"/></linearGradient>')
        paint, lum = "url(#g)", (luminance(top) + luminance(bot)) / 2
    else:
        defs_fill, paint, lum = "", hexc(fill[1]), luminance(fill[1])
    light = lum > 0.8
    shade_op, hi_op = (".1", ".5") if light else (".15", ".15")
    rect = (f'x="{TILE_X}" y="{TILE_Y}" width="{TILE_S}" height="{TILE_S}" '
            f'rx="{TILE_R}" ry="{TILE_R}"')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="64" height="64" viewBox="0 0 64 64">'
        '<defs>'
        '<filter id="s" x="-.1" y="-.1" width="1.2" height="1.25" '
        'style="color-interpolation-filters:sRGB"><feGaussianBlur stdDeviation="1.2"/></filter>'
        f'<clipPath id="t"><rect {rect}/></clipPath>{defs_fill}'
        '</defs>'
        f'<rect x="{TILE_X}" y="{TILE_Y + 1}" width="{TILE_S}" height="{TILE_S}" '
        f'rx="{TILE_R}" ry="{TILE_R}" fill="#000" opacity=".3" filter="url(#s)"/>'
        f'<rect {rect} fill="{paint}"/>'
        f'<image width="64" height="64" preserveAspectRatio="none" clip-path="url(#t)" '
        f'xlink:href="data:image/png;base64,{png_b64}"/>'
        f'<path d="{SHADE}" opacity="{shade_op}"/>'
        f'<path fill="#fff" d="{HIGHLIGHT}" opacity="{hi_op}"/>'
        '</svg>\n'
    )


def make_icon(src, args):
    img = load_image(src)
    info = analyse(img)
    if args.bg:
        info["has_bg"] = False
        info["fill"] = ("solid", parse_color(args.bg))
    elif not info["has_bg"]:
        # white tile, unless the logo itself is (nearly) white
        info["fill"] = ("solid", (0xf2, 0xf2, 0xf2) if info["glyph_lum"] < 0.85 else (0x33, 0x33, 0x33))
    png = compose(img, info, args.zoom, args.glyph_size)
    return build_svg(png, info["fill"]), info


# ------------------------------------------------------------ actions ---

def update_caches(theme_names):
    tool = shutil.which("gtk-update-icon-cache") or shutil.which("gtk4-update-icon-cache")
    for t in theme_names:
        tdir = DATA_HOME / "icons" / t
        if tool and (tdir / "icon-theme.cache").exists():
            subprocess.run([tool, "-f", "-q", "-t", str(tdir)], capture_output=True)


def reload_icon_theme():
    """Nudge GNOME to re-read the icon theme (toggles it away and back)."""
    gs = shutil.which("gsettings")
    if not gs:
        return
    key = ["org.gnome.desktop.interface", "icon-theme"]
    cur = subprocess.run([gs, "get", *key], capture_output=True, text=True).stdout.strip().strip("'")
    if not cur:
        return
    subprocess.run([gs, "set", *key, "Adwaita" if cur != "Adwaita" else "hicolor"])
    time.sleep(0.6)
    subprocess.run([gs, "set", *key, cur])


def write_desktop_override(desktop, old_icon, new_icon, manifest_entry):
    target = DATA_HOME / "applications" / desktop.name
    target.parent.mkdir(parents=True, exist_ok=True)
    text = desktop.read_text(errors="replace")
    if target.exists() and desktop.resolve() == target.resolve():
        backup = STATE / "backup" / (desktop.name + ".orig")
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(target, backup)
        manifest_entry["desktop_backup"] = str(backup)
    else:
        manifest_entry["desktop_created"] = True
    new = re.sub(r"^Icon=" + re.escape(old_icon) + r"\s*$", "Icon=" + new_icon, text, flags=re.M)
    target.write_text(new)
    manifest_entry["desktop"] = str(target)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", "-q", str(target.parent)], capture_output=True)


def process(did, desktop, args, themes, manifest, shipped):
    entry = read_entry(desktop) or {}
    icon = entry.get("Icon", "")
    label = entry.get("Name", did)
    hit = blacklisted(did, entry, args.blacklist)
    if hit:
        print(f"  skip  {label}: blacklisted ('{hit}')")
        return False
    if not icon and not args.source:
        print(f"  skip  {label}: no Icon= line")
        return False
    absolute = icon.startswith("/")
    if not args.force and not absolute and icon in shipped:
        print(f"  skip  {label}: Colloid already has '{icon}' (use --force to replace)")
        return False
    src = Path(args.source).expanduser() if args.source else find_source_icon(icon)
    if not src or not src.is_file():
        print(f"  skip  {label}: couldn't find the original icon file for '{icon}'")
        return False

    new_name = icon if (icon and not absolute) else PREFIX + Path(did).stem
    if args.dry_run:
        print(f"  would theme  {label}  [{new_name}]  from {src}")
        return False

    try:
        svg, info = make_icon(src, args)
    except Exception as e:  # noqa: BLE001
        print(f"  fail  {label}: {e}")
        return False

    m = manifest.setdefault(did, {"files": [], "backups": {}})
    written = set()
    for theme, subdir in themes.items():
        out = DATA_HOME / "icons" / theme / subdir / f"{new_name}.svg"
        real = out.resolve()
        if real in written:
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and str(out) not in m["files"]:
            backup = STATE / "backup" / theme / f"{new_name}.svg"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out, backup)
            m["backups"][str(out)] = str(backup)
        if out.is_symlink():
            out.unlink()
        out.write_text(svg)
        written.add(out.resolve())
        if str(out) not in m["files"]:
            m["files"].append(str(out))
    m.update(name=label, icon=new_name, source=str(src))

    if absolute and icon:
        write_desktop_override(desktop, icon, new_name, m)

    kind = "background extended" if info.get("has_bg") and not args.bg else "placed on tile"
    print(f"  done  {label}  [{new_name}]  ({kind})")
    return True


def restore(query, manifest):
    keys = [k for k, v in manifest.items()
            if query.lower() in k.lower() or query.lower() in v.get("name", "").lower()]
    if not keys:
        sys.exit(f"nothing colloidified matches '{query}'  (see --list)")
    for k in keys:
        e = manifest.pop(k)
        for f in e.get("files", []):
            Path(f).unlink(missing_ok=True)
            b = e.get("backups", {}).get(f)
            if b and Path(b).exists():
                shutil.move(b, f)
        if e.get("desktop_created"):
            Path(e["desktop"]).unlink(missing_ok=True)
        elif e.get("desktop_backup") and Path(e["desktop_backup"]).exists():
            shutil.move(e["desktop_backup"], e["desktop"])
        print(f"  restored  {e.get('name', k)}")


def main():
    ap = argparse.ArgumentParser(description="Give non-Colloid app icons the Colloid look.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("Usage:")[1])
    ap.add_argument("apps", nargs="*", help="app name, desktop id, or path to a .desktop file")
    ap.add_argument("--source", help="use this image instead of the app's current icon")
    ap.add_argument("--all", action="store_true", help="every visible app Colloid doesn't cover")
    ap.add_argument("--dry-run", action="store_true", help="show what would change")
    ap.add_argument("--force", action="store_true", help="also replace icons Colloid already has")
    ap.add_argument("--bg", metavar="COLOR", help="force a tile color, e.g. '#ffffff' or '#1e1e1e'")
    ap.add_argument("--zoom", type=float, default=1.0,
                    help="scale the artwork (e.g. 1.08 to crop off an original border)")
    ap.add_argument("--glyph-size", type=float, default=36,
                    help="size of bare logos inside the 56px tile (default 36)")
    ap.add_argument("--list", action="store_true", help="list colloidified apps")
    ap.add_argument("--restore", metavar="APP", help="undo colloidify for an app")
    ap.add_argument("--ignore", nargs="+", metavar="APP",
                    help="add apps to the blacklist so colloidify never touches them")
    ap.add_argument("--unignore", nargs="+", metavar="APP", help="remove apps from the blacklist")
    ap.add_argument("--show-ignored", action="store_true", help="print the blacklist")
    ap.add_argument("--no-reload", action="store_true", help="don't refresh the GNOME icon theme")
    args = ap.parse_args()

    manifest = load_manifest()
    args.blacklist = load_blacklist()

    if args.show_ignored:
        print(f"{BLACKLIST}:")
        for e in args.blacklist or ["  (empty)"]:
            print("  " + e)
        return
    if args.ignore or args.unignore:
        bl = args.blacklist
        for q in args.ignore or []:
            hits = find_desktops(q) if not any(c in q for c in "*?[") else {}
            # store the desktop id when the name matches exactly one app
            key = (Path(next(iter(hits))).stem if len(hits) == 1 else q)
            if key.lower() in (e.lower() for e in bl):
                print(f"  already ignored  {key}")
                continue
            bl.append(key)
            print(f"  ignored  {key}")
            done = [v.get("name", k) for k, v in manifest.items()
                    if blacklisted(k, {"Name": v.get("name", ""), "Icon": v.get("icon", "")}, [key])]
            for name in done:
                print(f"    note: {name} is already colloidified; "
                      f"run --restore '{name}' to bring its original icon back")
        for q in args.unignore or []:
            before = len(bl)
            bl = [e for e in bl if e.lower() != q.lower()
                  and e.lower() != q.lower().removesuffix(".desktop")]
            if len(bl) == before:
                hits = find_desktops(q)
                if len(hits) == 1:
                    did, f = next(iter(hits.items()))
                    pat = blacklisted(did, read_entry(f) or {}, bl)
                    if pat and any(c in pat for c in "*?["):
                        print(f"  {q} is covered by the wildcard '{pat}'; "
                              f"edit {BLACKLIST} to change it")
                        continue
                    if pat:
                        bl.remove(pat)
            print(f"  {'unignored' if len(bl) < before else 'not in blacklist'}  {q}")
        save_blacklist(bl)
        return

    if args.list:
        for k, v in sorted(manifest.items()):
            print(f"{v.get('name', k):30} {v.get('icon', '')}")
        return
    if args.restore:
        restore(args.restore, manifest)
        save_manifest(manifest)
        update_caches(colloid_themes())
        if not args.no_reload:
            reload_icon_theme()
        return

    if not args.apps and not args.all:
        ap.print_help()
        return
    if args.source and (args.all or len(args.apps) != 1):
        sys.exit("--source works with exactly one app")

    themes = colloid_themes()
    if not themes:
        sys.exit("No Colloid icon theme found in ~/.icons, ~/.local/share/icons or /usr/share/icons")
    print("Colloid variants: " + ", ".join(themes))
    shipped = colloid_icon_names(themes, our_files(manifest))

    targets = {}
    if args.all:
        for did, f in all_desktops().items():
            e = read_entry(f) or {}
            if visible(e) and (e["Icon"].startswith("/") or e["Icon"] not in shipped):
                targets[did] = f
    for q in args.apps:
        hits = find_desktops(q)
        if not hits:
            print(f"  no app matches '{q}'")
        elif len(hits) > 1 and not args.all:
            print(f"  '{q}' matches several apps, be more specific:")
            for did, f in hits.items():
                print(f"      {did:45} {(read_entry(f) or {}).get('Name', '')}")
        else:
            targets.update(hits)

    changed = 0
    for did, f in sorted(targets.items()):
        changed += process(did, f, args, themes, manifest, shipped)

    if changed:
        save_manifest(manifest)
        update_caches(themes)
        if not args.no_reload:
            reload_icon_theme()
        print(f"\n{changed} icon(s) updated. If the menu still shows the old icon, "
              "log out and back in.")


if __name__ == "__main__":
    main()
