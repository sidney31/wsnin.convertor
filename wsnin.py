"""K3-Mebel MDB  ->  .wsnin (nesting input JSON).

The output mirrors the structure of a reference .wsnin file:
  {"status": 0, "data": {"inputName": ..., "items": [{"sourceInfo": {...}, "panels": [...]}]}}
One item is produced per K3 product (cabinet); every K3 panel becomes one panel.

Where the K3 data lives:
  TElems          names, quantities, material (PriceID), parent/child tree
  TPanels         length / width / thickness, texture direction (Dir)
  TParams[TPaths] contour segments: Hold1=path, Hold2=0 meta (BandUnitPos),
                  Hold2=1 geometry (X1,Y1,X2,Y2 and XM,YM = arc midpoint)
  TPaths          path 2 (IsPlanePath=False, IsResult=True) = full-size contour
  THoles          panel-local matrix; column 3 = drilling axis, column 4 = position
  TSlots          grooves
  TNNomenclature  material / band / hardware names and articles
"""
import math
import re
from collections import defaultdict

from access_parser import AccessParser

import mdb_text

mdb_text.install()

EXTRA_COLUMNS = [
    "Наименование_изделия",
    "Поз.",
    "Путь",
    "Артикул",
    "Артикул изделия",
    "Проект",
    "Текстура (исходные данные)",
    "ID детали",
    "四边封边（字符串）",
    "上封边（字符串）",
    "下封边（字符串）",
    "左封边（字符串）",
    "右封边（字符串）",
    "数据来源",
    "Припуск на склейку",
    "Припуск на увеличенную деталь",
    "Фактический размер",
    "Толщина склейки",
    "Склейка",
    "Обозначение",
    "Артикул детали",
    "Артикул материала",
    "Направление текстуры",
    "Габариты",
    "Облицовка пласти",
    "Фурнитура",
    "Кол.Пазов",
    "Пользовательские свойства",
    "Заметки",
]

EPS = 0.6  # tolerance when deciding which edge/face a feature sits on
CUTOUT_TOOL_DIAM = 12.0  # cutter for inner cut-outs (K3 nomenclature: "Фреза прямая 12_40")
NOT_HARDWARE = {"Линия сверловки"}


# ---------------------------------------------------------------- helpers
def num(v) -> float:
    return round(float(v or 0), 3) + 0.0  # + 0.0 turns -0.0 into 0.0


def fmt(v) -> str:
    """Number as the reference writes it in str* fields: 179, 1078.5."""
    v = num(v)
    return str(int(v)) if v == int(v) else f"{v:g}"


def band_thickness_from_name(name: str):
    """'Кромка 19х04' -> 0.4, 'Кромка 22х1' -> 1.0, 'Кромка 35x2' -> 2.0."""
    m = re.search(r"\d+(?:[.,]\d+)?\s*[хxХX×/]\s*(\d+(?:[.,]\d+)?)", name or "")
    if not m:
        return None
    s = m.group(1).replace(",", ".")
    if "." not in s and len(s) > 1 and s.startswith("0"):
        s = "0." + s[1:]
    return float(s)


def load_tables(mdb_path: str) -> dict:
    db = AccessParser(mdb_path)
    tables = {}
    for name in db.catalog:
        if name.startswith("MSys"):
            continue
        try:
            t = db.parse_table(name)
        except Exception:
            continue
        cols = list(t)
        tables[name] = [dict(zip(cols, row)) for row in zip(*(t[c] for c in cols))]
    return tables


# ---------------------------------------------------------------- builder
class Converter:
    def __init__(self, tables: dict, input_name: str):
        self.t = tables
        self.input_name = input_name
        self.elems = {r["UnitPos"]: r for r in tables.get("TElems", [])}
        self.nom = {r["ID"]: r for r in tables.get("TNNomenclature", [])}
        self.objects = {r["UnitPos"] for r in tables.get("TObjects", [])}

        # contour params: (unit, path, hold2, seg) -> {param: value}
        self.path_params = defaultdict(dict)
        for r in tables.get("TParams", []):
            if r["HoldTable"] == "TPaths":
                val = r["NumValue"] if r["StrValue"] is None else r["StrValue"]
                self.path_params[(r["UnitPos"], r["Hold1"], r["Hold2"], r["Hold3"])][r["ParamName"]] = val

        # full-size result contours per panel: largest = outer, rest = inner cut-outs
        self.outer_path, self.inner_paths, self.plane_path = {}, defaultdict(list), {}
        by_panel = defaultdict(list)
        for p in tables.get("TPaths", []):
            if p["IsResult"] and not p["IsTCuts"]:
                by_panel[p["PanelPos"]].append(p)
        for unit, paths in by_panel.items():
            full = sorted((p for p in paths if not p["IsPlanePath"]), key=lambda p: -(p["Square"] or 0))
            plane = sorted((p for p in paths if p["IsPlanePath"]), key=lambda p: -(p["Square"] or 0))
            if full:
                self.outer_path[unit] = full[0]["PathID"]
                self.inner_paths[unit] = [p["PathID"] for p in full[1:]]
            if plane:
                self.plane_path[unit] = plane[0]["PathID"]

        self.holes = defaultdict(list)
        for h in tables.get("THoles", []):
            self.holes[h["UnitPos"]].append(h)
        self.slots = defaultdict(list)
        for s in tables.get("TSlots", []):
            self.slots[s["PanelPos"]].append(s)

    # -- lookups
    def material_of(self, unit_pos):
        e = self.elems.get(unit_pos)
        return self.nom.get(e["PriceID"]) if e else None

    def material_name(self, unit_pos) -> str:
        m = self.material_of(unit_pos)
        return (m or {}).get("Name") or ""

    def product_of(self, unit_pos):
        u = unit_pos
        while u and u not in self.objects:
            u = self.elems.get(u, {}).get("ParentPos")
        return u or 0

    # -- contour
    def contour_segments(self, unit_pos, path_id):
        """Ordered (geometry, meta) pairs. Hold2=1 is a line, Hold2=2 an arc."""
        if path_id is None:
            return []
        segs = []
        for seg in range(1, 10000):
            geo = self.path_params.get((unit_pos, path_id, 1, seg)) or self.path_params.get((unit_pos, path_id, 2, seg))
            if geo is None:
                if not self.path_params.get((unit_pos, path_id, 0, seg)):
                    break
                continue
            meta = self.path_params.get((unit_pos, path_id, 0, seg), {})
            segs.append((geo, meta))
        return segs

    def band_of_segment(self, geo, meta, plane_geo):
        band_unit = meta.get("BandUnitPos")
        if not band_unit:
            return None
        band_unit = int(band_unit)
        mat = self.material_of(band_unit) or {}
        name = mat.get("Name") or ""
        thickness = band_thickness_from_name(name)
        if thickness is None and plane_geo:  # fall back to K3's own offset
            thickness = segment_offset(geo, plane_geo)
        return {"thickness": num(thickness or 0), "name": name, "article": mat.get("Article") or ""}

    def build_contour(self, unit_pos):
        full = ccw(self.contour_segments(unit_pos, self.outer_path.get(unit_pos)))
        plane_segs = self.contour_segments(unit_pos, self.plane_path.get(unit_pos))
        plane = {i: g for i, (g, _) in enumerate(plane_segs)} if len(plane_segs) == len(full) else {}
        vertices, seg_bands = [], []
        for i, (geo, meta) in enumerate(full):
            v = {"x": num(geo.get("X1")), "y": num(geo.get("Y1"))}
            if "XM" in geo:
                v["bulge"] = arc_bulge(geo)
            band = self.band_of_segment(geo, meta, plane.get(i))
            if band:
                v["thickness"] = band["thickness"]
                v["material"] = band["name"]
            vertices.append(v)
            seg_bands.append((geo, band))
        return vertices, seg_bands

    def cutouts_of(self, unit_pos, thickness, start_order):
        """Inner through cut-outs as closed milling paths (format of the reference 'paths')."""
        out = []
        for path_id in self.inner_paths.get(unit_pos, []):
            segs = ccw(self.contour_segments(unit_pos, path_id))
            if len(segs) < 2:
                continue
            verts = []
            for geo, _ in segs + segs[:1]:  # closed: repeat the first point
                x, y = num(geo.get("X1")), num(geo.get("Y1"))
                verts.append({"x": x, "strX": fmt(x), "y": y, "strY": fmt(y), "strZ": "0",
                              "bulge": arc_bulge(geo) if "XM" in geo else 0.0})
            verts[-1]["bulge"] = 0.0
            out.append({
                "pathType": 0,
                "width": CUTOUT_TOOL_DIAM, "strWidth": fmt(CUTOUT_TOOL_DIAM),
                "depth": thickness, "strDepth": fmt(thickness),
                "close": True,
                "empty": True,
                "correction": "LEFT",  # contour is CCW, so LEFT keeps the tool inside the cut-out
                "direction": "ZM",
                "strEntryZ": "0",
                "strEntryL": "0",
                "order": start_order + len(out),
                "vecVertex": verts,
            })
        return out

    # -- panel
    def panel_sides(self, seg_bands, length, width):
        """Assign banded straight segments to left/right/top/bottom edges."""
        xs = [g.get(k) for g, _ in seg_bands for k in ("X1", "X2")]
        ys = [g.get(k) for g, _ in seg_bands for k in ("Y1", "Y2")]
        min_x, max_x = (min(xs), max(xs)) if xs else (0, length)
        min_y, max_y = (min(ys), max(ys)) if ys else (0, width)
        sides = {"left": [], "right": [], "top": [], "bottom": []}
        for g, band in seg_bands:
            if not band or "XM" in g:
                continue
            x1, y1, x2, y2 = g["X1"], g["Y1"], g["X2"], g["Y2"]
            if abs(x1 - x2) < EPS and abs(x1 - min_x) < EPS:
                sides["left"].append(band)
            elif abs(x1 - x2) < EPS and abs(x1 - max_x) < EPS:
                sides["right"].append(band)
            elif abs(y1 - y2) < EPS and abs(y1 - max_y) < EPS:
                sides["top"].append(band)
            elif abs(y1 - y2) < EPS and abs(y1 - min_y) < EPS:
                sides["bottom"].append(band)
        return sides

    def holes_of(self, unit_pos, thickness):
        out = []
        for h in sorted(self.holes.get(unit_pos, []), key=lambda r: r["HolePos"] or 0):
            ax = (h["MatrA13"] or 0, h["MatrA23"] or 0, h["MatrA33"] or 0)
            x, y, z = num(h["MatrA14"]), num(h["MatrA24"]), num(h["MatrA34"])
            direction = hole_direction(ax)
            if direction is None:
                continue  # inclined hole: not representable
            if direction in ("ZM", "ZP"):
                z = 0.0
            out.append({
                "x": x, "strX": fmt(x),
                "y": y, "strY": fmt(y),
                "z": z, "strZ": fmt(z),
                "diam": num(h["Diameter"]), "strDiam": fmt(h["Diameter"]),
                "depth": num(h["Depth"]), "strDepth": fmt(h["Depth"]),
                "direction": direction,
                "order": len(out) + 1,
            })
        return out

    def grooves_of(self, unit_pos):
        """K3 straight grooves as rectangular pockets.

        ASAI Nest rejects the 'grooves' list (optimisation error 80), but accepts
        'pockets' in the reference format, and a straight groove is a long pocket.
        """
        out = []
        for s in sorted(self.slots.get(unit_pos, []), key=lambda r: r["SlotPos"] or 0):
            x1, y1, x2, y2 = (num(s[k]) for k in ("BegX", "BegY", "EndX", "EndY"))
            cx, cy = num((x1 + x2) / 2), num((y1 + y2) / 2)
            length = num(math.hypot(x2 - x1, y2 - y1))
            angle = num(math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180)
            out.append({
                "centerX": cx, "strCenterX": fmt(cx),
                "centerY": cy, "strCenterY": fmt(cy),
                "centerZ": 0.0,
                "length": length, "strLength": fmt(length),
                "width": num(s["Width"]), "strWidth": fmt(s["Width"]),
                "depth": num(s["Depth"]), "strDepth": fmt(s["Depth"]),
                "radiusCorner": 0.0, "strRadiusCorner": "0",
                "angle": angle, "strAngle": fmt(angle),
                "direction": "ZP" if s["Map"] == 6 else "ZM",
                "strEntryZ": "0", "strEntryL": "0",
                "entryL": 0.0,
            })
        return out

    def hardware_of(self, unit_pos) -> str:
        """'Name\\rArticle ×N; ...' — one fitting instance per HolderPos."""
        counts = {}
        for h in self.holes.get(unit_pos, []):
            holder = self.elems.get(h["HolderPos"])
            if not holder:
                continue
            name = (holder["Name"] or "").strip()
            if not name or name in NOT_HARDWARE:
                continue
            art = (self.nom.get(holder["PriceID"]) or {}).get("Article") or ""
            label = f"{name}\r{art}" if art else name
            counts.setdefault(label, set()).add(h["HolderPos"])
        return "; ".join(f"{k} ×{len(v)}" for k, v in counts.items())

    def build_panel(self, unit_pos, product_name):
        p = self.panel_rows[unit_pos]
        e = self.elems[unit_pos]
        mat = self.material_of(unit_pos) or {}
        material = mat.get("Name") or ""
        length, width, thick = num(p["Length"]), num(p["Width"]), num(p["Thickness"])
        pos = str(e.get("CommonPos") or unit_pos)
        code = f"{self.input_name}_{pos}"
        name = (e.get("Name") or "").strip()
        qty = int(e.get("Count") or 1)

        vertices, seg_bands = self.build_contour(unit_pos)
        # K3's Length is not always along X: take sizes from the contour (holes use its frame)
        swapped = False
        if vertices:
            size_x = num(max(v["x"] for v in vertices) - min(v["x"] for v in vertices))
            size_y = num(max(v["y"] for v in vertices) - min(v["y"] for v in vertices))
            swapped = abs(size_x - length) > 1.5 and abs(size_x - width) <= 1.5
            if swapped:
                length, width = width, length
        # the nesting program expects the contour to start at (0, 0): shift everything
        dx = dy = 0.0
        if vertices:
            dx = -min(v["x"] for v in vertices)
            dy = -min(v["y"] for v in vertices)
            for v in vertices:
                v["x"], v["y"] = num(v["x"] + dx), num(v["y"] + dy)
        grain_along_length = abs(num(p.get("Dir")) - 90) >= 1  # K3 Dir: 0 = along Length
        texture = 1 if grain_along_length != swapped else 2
        tex_text = "вдоль ширины" if texture == 2 else "вдоль длины"

        sides = self.panel_sides(seg_bands, length, width)

        def side_thk(side):
            return max((b["thickness"] for b in sides[side]), default=0.0)

        def band_str(b):
            s = f"{fmt(b['thickness'])}||{b['name']}"
            return f"{s}  {b['article']}" if b["article"] else s

        def side_str(side):
            return ",".join(band_str(b) for b in sides[side])

        side_texts = [side_str(s) for s in ("top", "bottom", "left", "right")]
        holes = self.holes_of(unit_pos, thick)
        grooves = self.grooves_of(unit_pos)
        cutouts = self.cutouts_of(unit_pos, thick, len(holes) + 1)
        if dx or dy:
            shift_features(holes, grooves, cutouts, dx, dy)

        source_panel = {
            "code": code,
            "material": material,
            "materialRaw": material,
            "identifyCode": "",
            "thickness": thick,
            "length": length,
            "width": width,
            "actualLength": length,
            "actualWidth": width,
            "texture": texture,
            "bandLeft": side_thk("left"),
            "bandRight": side_thk("right"),
            "bandTop": side_thk("top"),
            "bandBottom": side_thk("bottom"),
            "name": name,
            "frontId": pos,
            "quantity": qty,
            "optBatch": self.input_name,
            "loadedFile": "",
            "extraColumn": [
                product_name,
                pos,
                "",
                f"\\{self.input_name}\\{pos}",
                "",
                self.input_name,
                "Y" if texture else "N",
                code,
                ",".join(t for t in side_texts if t),
                *side_texts,
                f"{self.input_name}.wsnin",
                "", "", "", "", "", "", "",
                mat.get("Article") or "",
                tex_text,
                f"{fmt(length)}×{fmt(width)}×{fmt(thick)}",
                "",
                self.hardware_of(unit_pos),
                str(len(grooves)),
                "",
                "",
            ],
        }
        if not vertices:  # no contour stored: plain rectangle
            vertices = [{"x": length, "y": 0.0}, {"x": length, "y": width},
                        {"x": 0.0, "y": width}, {"x": 0.0, "y": 0.0}]
        panel = {
            "strId": code,
            "material": material,
            "codeFront": pos,
            "thickness": thick,
            "length": length,
            "width": width,
            "texture": texture,
            "name": name,
            "quantity": qty,
            "sourceFile": pos,
            "outer": {"vecVertex": vertices},
            "holes": holes,
            "grooves": [],
            "paths": cutouts,
            "pockets": grooves,
            "lamellos": [],
            "words": [],
        }
        return source_panel, panel

    def build(self) -> dict:
        self.panel_rows = {r["UnitPos"]: r for r in self.t.get("TPanels", []) if r["UnitPos"] in self.elems}
        groups = defaultdict(list)
        for u in sorted(self.panel_rows, key=lambda u: self.elems[u].get("CommonPos") or u):
            groups[self.product_of(u)].append(u)

        order = (self.t.get("TOrderInfo") or [{}])[0]
        items = []
        for product, units in groups.items():
            product_name = (self.elems.get(product, {}).get("Name") or order.get("OrderName") or self.input_name).strip()
            src_panels, panels = [], []
            for u in units:
                sp, p = self.build_panel(u, product_name)
                src_panels.append(sp)
                panels.append(p)
            items.append({
                "sourceInfo": {
                    "fileName": product_name,
                    "filePath": "",
                    "sourceType": 1,
                    "identifyCode": "",
                    "extraColumn": EXTRA_COLUMNS,
                    "panels": src_panels,
                },
                "panels": panels,
            })
        return {"status": 0, "data": {"inputName": self.input_name, "items": items}}


# ---------------------------------------------------------------- geometry
def arc_bulge(geo) -> float:
    """Bulge (tan(angle/4), + = counter-clockwise) from start, end, midpoint."""
    sx, sy, ex, ey = geo["X1"], geo["Y1"], geo["X2"], geo["Y2"]
    mx, my = geo["XM"], geo["YM"]
    half_chord = math.hypot(ex - sx, ey - sy) / 2
    if half_chord < 1e-9:
        return 1.0  # full circle
    cross = (ex - sx) * (my - sy) - (ey - sy) * (mx - sx)
    sagitta = abs(cross) / (2 * half_chord)
    # midpoint to the right of the chord => counter-clockwise arc
    return round(math.copysign(sagitta / half_chord, -cross), 6)


def shift_features(holes, grooves, cutouts, dx, dy):
    """Move machining by (dx, dy) and keep the str* twins in sync."""
    def move(obj, xk, yk, sxk, syk):
        obj[xk] = num(obj[xk] + dx)
        obj[yk] = num(obj[yk] + dy)
        obj[sxk], obj[syk] = fmt(obj[xk]), fmt(obj[yk])

    for h in holes:
        move(h, "x", "y", "strX", "strY")
    for g in grooves:  # pockets
        move(g, "centerX", "centerY", "strCenterX", "strCenterY")
    for c in cutouts:
        for v in c["vecVertex"]:
            move(v, "x", "y", "strX", "strY")


def ccw(segs):
    """Return segments ordered counter-clockwise (reversing arcs' direction if needed)."""
    if len(segs) < 3:
        return segs
    area = sum(g["X1"] * g["Y2"] - g["X2"] * g["Y1"] for g, _ in segs)
    if area >= 0:
        return segs
    out = []
    for g, meta in reversed(segs):
        r = dict(g)
        r["X1"], r["Y1"], r["X2"], r["Y2"] = g["X2"], g["Y2"], g["X1"], g["Y1"]
        out.append((r, meta))  # arc midpoint stays the same, bulge sign flips by itself
    return out


def segment_offset(a, b) -> float:
    """Distance between two (parallel) segments' lines."""
    ax, ay, bx, by = a["X1"], a["Y1"], a["X2"], a["Y2"]
    length = math.hypot(bx - ax, by - ay)
    if length < 1e-9:
        return 0.0
    px, py = (b["X1"] + b["X2"]) / 2, (b["Y1"] + b["Y2"]) / 2
    return abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / length


def hole_direction(axis):
    ax, ay, az = axis
    if abs(az) > 0.9:
        return "ZP" if az > 0 else "ZM"
    if abs(ax) > 0.9:
        return "XP" if ax > 0 else "XM"
    if abs(ay) > 0.9:
        return "YP" if ay > 0 else "YM"
    return None


def convert_mdb(mdb_path: str, input_name: str) -> dict:
    return Converter(load_tables(mdb_path), input_name).build()
