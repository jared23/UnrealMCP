"""UserTools :: Landscape / terrain authoring (ADVANCED)  (spec: docs/spec/world.md lines 8-23)

The ADVANCED procedural suite that closes the WORLD category: full eroded terrain generation,
per-region ops, seamless region regen, geomorphic processes, multi-layer painting, height-based
splines, and steepest-descent river carving. Every tool builds on the SAME already-verified C++
landscape edit-data bridge used by landscape_write.py (UMCPReflectionLibrary.*_json statics) and
does ALL procedural math HOST-SIDE (in this MCP process, like scatter_foliage_in_box /
landscape_write.create_landscape's _procedural_field) -- NO C++, NO engine changes, NO build.

Design: heavy math runs host-side (this module, normal Python). The editor snippets are tiny and
only READ a region, WRITE a region, or PAINT a layer, then append the undo ledger entry. So there
is NO editor-side compute-timeout risk, and the snippet bodies stay trivially free of the forbidden
triple-quote / backslash tokens (all data crosses as base64 PARAMS, exactly like landscape_write.py).

Bridge handlers used (all #if WITH_EDITOR, JSON returns):
  - landscape_get_info_json(actor)                                  -> resolution/config/scale/layers
  - landscape_get_height_region_json(actor, x, y, w, h)            -> base64 uint16 (row-major, CLAMPED W*H)
  - landscape_set_height_region_json(actor, x, y, w, h, b64, prior) -> write heights (rejects a clamped region)
  - landscape_paint_weight_region_json(actor, layer, x,y,w,h, b64, layer_info_path) -> paint (returns prev)

WIRE FORMAT: heights = base64 LE uint16 row-major W*H; weights = base64 uint8. Height encoding:
uint16 H; flat=32768; WorldZ = ((H-32768)/128)*draw_scale_z + actor_loc_z. World cm delta -> uint16
delta dH = cm*128/draw_scale_z. Vertex<->world XY: worldX = actor_loc_x + Lx*draw_scale_x.

REVERSIBILITY -- reuses ONLY existing editor_level.undo branches (NO new fold):
  - every height write   -> ledger op "landscape_set_height_region" {op,actor_name,x,y,w,h,prev_b64}
                            (inverse re-writes prev_b64 -- EXISTING branch).
  - every layer paint    -> ledger op "landscape_paint_weight_region"
                            {op,actor_name,layer_name,x,y,w,h,prev_b64,layer_info_path}
                            (inverse re-paints prev_b64 -- EXISTING branch); a freshly created
                            LayerInfo asset is ALSO ledgered FIRST op "create_asset"
                            {op,asset_path,package_path,created_dir} (EXISTING branch; LIFO).
  - clear_landscape_splines is a NON-LEDGERED maintenance op (see its docstring) -- our own tools
    create NO persistent splines, so it normally clears 0 and never fabricates a fake inverse.

Scratch-only. Use a NON-World-Partition scratch level. The ONE unified undo lives in editor_level.py;
this module NEVER defines its own undo.
"""
import json
import base64
import textwrap
import os
import math
import random
import struct

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (verbatim from editor_level.py / landscape_write.py) -----------------
_LOG_HEAD = (
    "import unreal as _uu, os as _oo, json as _jj\n"
    "def _umcp_logmain():\n"
    "    d=_uu.Paths.convert_relative_path_to_full(_uu.Paths.project_log_dir())\n"
    "    for f in _oo.listdir(d):\n"
    "        if f.endswith('.log') and '-backup-' not in f:\n"
    "            return _oo.path.join(d,f)\n"
    "    return None\n"
    "try:\n"
    "    _umcp_main=_umcp_logmain(); _umcp_s0=_oo.path.getsize(_umcp_main) if _umcp_main else 0\n"
    "except Exception:\n"
    "    _umcp_main=None; _umcp_s0=0\n"
    "try:\n"
)
_LOG_TAILER = (
    "\nfinally:\n"
    "    try:\n"
    "        _uu.log_flush()\n"
    "        if _umcp_main:\n"
    "            _fh=open(_umcp_main,'rb'); _fh.seek(_umcp_s0); _dd=_fh.read().decode('utf-8','replace'); _fh.close()\n"
    "            _ww=[ln for ln in _dd.splitlines() if (': Warning:' in ln or ': Error:' in ln) and 'UMCP' not in ln and 'LogMCP:' not in ln]\n"
    "            if _ww: print('@@UMCP_LOG@@'+_jj.dumps(_ww[-50:]))\n"
    "    except Exception:\n"
    "        pass\n"
)


def _wrap(code):
    """Wrap a snippet with Output-Log delta capture (try/finally)."""
    return _LOG_HEAD + textwrap.indent(code, "    ") + _LOG_TAILER


# =================================================================================================
# HOST-SIDE numeric helpers (this MCP process; backslashes/comments allowed here -- NOT snippet bodies)
# =================================================================================================
def _b64_to_u16(b):
    raw = base64.b64decode(b) if b else b""
    n = len(raw) // 2
    return list(struct.unpack("<%dH" % n, raw)) if n else []


def _u16_to_b64(vals):
    raw = struct.pack("<%dH" % len(vals), *[max(0, min(65535, int(round(v)))) for v in vals])
    return base64.b64encode(raw).decode("ascii")


def _u8_to_b64(vals):
    raw = bytes([max(0, min(255, int(round(v)))) for v in vals])
    return base64.b64encode(raw).decode("ascii")


def _hash01(ix, iy, seed):
    """Deterministic pseudo-random float in [0,1) for an integer grid point (seeded)."""
    h = (int(ix) * 374761393 + int(iy) * 668265263 + int(seed) * 362437) & 0xFFFFFFFF
    h = (h ^ (h >> 13)) & 0xFFFFFFFF
    h = (h * 1274126177) & 0xFFFFFFFF
    h = (h ^ (h >> 16)) & 0xFFFFFFFF
    return h / 4294967295.0


def _vnoise(fx, fy, seed):
    """Bilinear value noise with smoothstep interpolation. Returns [0,1)."""
    x0 = int(math.floor(fx)); y0 = int(math.floor(fy))
    tx = fx - x0; ty = fy - y0
    sx = tx * tx * (3.0 - 2.0 * tx)
    sy = ty * ty * (3.0 - 2.0 * ty)
    n00 = _hash01(x0, y0, seed); n10 = _hash01(x0 + 1, y0, seed)
    n01 = _hash01(x0, y0 + 1, seed); n11 = _hash01(x0 + 1, y0 + 1, seed)
    a = n00 + (n10 - n00) * sx
    b = n01 + (n11 - n01) * sx
    return a + (b - a) * sy


def _fbm(fx, fy, seed, octaves, ridged, sharpness):
    """Fractal Brownian motion (optionally ridged) in [0,1], sharpened by a gamma exponent."""
    amp = 1.0; freq = 1.0; total = 0.0; norm = 0.0
    for o in range(max(1, int(octaves))):
        n = _vnoise(fx * freq, fy * freq, seed + o * 1013)
        if ridged:
            n = 1.0 - abs(2.0 * n - 1.0)
            n = n * n
        total += n * amp
        norm += amp
        amp *= 0.5
        freq *= 2.0
    v = total / norm if norm else 0.0
    v = max(0.0, min(1.0, v))
    if sharpness and abs(sharpness - 1.0) > 1e-6:
        v = pow(v, float(sharpness))
    return v


def _base_terrain(w, h, sz, sx, P):
    """Build the base procedural height field (float uint16-units) from seeded ridged/fBm + domain warp."""
    seed = int(P.get("seed", 1337))
    amplitude = float(P.get("amplitude", 6000.0))
    sharpness = float(P.get("sharpness", 1.35))
    warp = float(P.get("warp", 0.35))
    feature_scale = P.get("feature_scale", None)
    octaves = int(P.get("octaves", 6))
    ridged = bool(P.get("ridged", True))
    detail = float(P.get("detail", 0.25))
    uplift = float(P.get("uplift", 0.0))
    if not feature_scale:
        feature_scale = max(6.0, min(w, h) / 3.5)
    feature_scale = float(feature_scale)
    amp_h = amplitude * 128.0 / (sz if sz else 1.0)
    up_h = uplift * 128.0 / (sz if sz else 1.0)
    hf = [0.0] * (w * h)
    for j in range(h):
        for i in range(w):
            fx = i / feature_scale; fy = j / feature_scale
            if warp:
                wx = _vnoise(fx * 0.5 + 11.2, fy * 0.5 + 3.7, seed + 91)
                wy = _vnoise(fx * 0.5 + 5.1, fy * 0.5 + 9.3, seed + 57)
                fx = fx + warp * (wx - 0.5) * 2.0
                fy = fy + warp * (wy - 0.5) * 2.0
            n = _fbm(fx, fy, seed, octaves, ridged, sharpness)
            if detail:
                d = _fbm(fx * 3.0 + 20.0, fy * 3.0 + 20.0, seed + 333, 3, False, 1.0)
                n = max(0.0, min(1.0, n * (1.0 - detail) + d * detail))
            hf[j * w + i] = 32768.0 + up_h + amp_h * n
    return hf, amp_h


def _thermal_erode(hf, w, h, talus, iters):
    """Talus (thermal) smoothing: move material to 4-neighbours where the slope exceeds `talus`."""
    for _it in range(max(0, int(iters))):
        delta = [0.0] * (w * h)
        for j in range(h):
            for i in range(w):
                k = j * w + i; hc = hf[k]
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ii = i + di; jj = j + dj
                    if 0 <= ii < w and 0 <= jj < h:
                        diff = hc - hf[jj * w + ii]
                        if diff > talus:
                            move = (diff - talus) * 0.125
                            delta[k] -= move
                            delta[jj * w + ii] += move
        for k in range(w * h):
            hf[k] += delta[k]


def _hydraulic_erode(hf, w, h, intensity, radius, seed, amp_h):
    """Seeded droplet (hydraulic) erosion. Droplet count + lifetime are capped (O(N))."""
    if w < 4 or h < 4:
        return
    rng = random.Random((int(seed) ^ 0x9E3779B9) & 0xFFFFFFFF)
    n_drops = int(min(9000, max(150, intensity * w * h * 0.55)))
    max_life = 30
    inertia = 0.05; capacity = 4.0; deposit = 0.30; erode = 0.30; evap = 0.02
    for _d in range(n_drops):
        px = rng.uniform(1.0, w - 2.0); py = rng.uniform(1.0, h - 2.0)
        dx = 0.0; dy = 0.0; water = 1.0; sediment = 0.0
        for _l in range(max_life):
            ix = int(px); iy = int(py)
            if ix < 1 or ix >= w - 2 or iy < 1 or iy >= h - 2:
                break
            fx = px - ix; fy = py - iy
            k = iy * w + ix
            h00 = hf[k]; h10 = hf[k + 1]; h01 = hf[k + w]; h11 = hf[k + w + 1]
            gx = (h10 - h00) * (1 - fy) + (h11 - h01) * fy
            gy = (h01 - h00) * (1 - fx) + (h11 - h10) * fx
            dx = dx * inertia - gx * (1 - inertia)
            dy = dy * inertia - gy * (1 - inertia)
            mag = math.hypot(dx, dy)
            if mag < 1e-6:
                ang = rng.uniform(0.0, 6.2831853)
                dx = math.cos(ang); dy = math.sin(ang); mag = 1.0
            dx /= mag; dy /= mag
            nx = px + dx; ny = py + dy
            hold = (h00 * (1 - fx) * (1 - fy) + h10 * fx * (1 - fy) +
                    h01 * (1 - fx) * fy + h11 * fx * fy)
            if nx < 1 or nx >= w - 2 or ny < 1 or ny >= h - 2:
                break
            nix = int(nx); niy = int(ny); nfx = nx - nix; nfy = ny - niy
            kk = niy * w + nix
            hnew = (hf[kk] * (1 - nfx) * (1 - nfy) + hf[kk + 1] * nfx * (1 - nfy) +
                    hf[kk + w] * (1 - nfx) * nfy + hf[kk + w + 1] * nfx * nfy)
            dh = hnew - hold
            cap = max(-dh, 0.0) * water * capacity + 0.01
            if sediment > cap or dh > 0:
                amt = (sediment - cap) * deposit if dh <= 0 else min(sediment, dh)
                sediment -= amt
                hf[k] += amt * (1 - fx) * (1 - fy); hf[k + 1] += amt * fx * (1 - fy)
                hf[k + w] += amt * (1 - fx) * fy; hf[k + w + 1] += amt * fx * fy
            else:
                amt = min((cap - sediment) * erode, -dh)
                sediment += amt
                hf[k] -= amt * (1 - fx) * (1 - fy); hf[k + 1] -= amt * fx * (1 - fy)
                hf[k + w] -= amt * (1 - fx) * fy; hf[k + w + 1] -= amt * fx * fy
            water *= (1 - evap)
            px = nx; py = ny


def _flow_accum(hf, w, h):
    """Steepest-descent flow accumulation (O(N log N) by a height sort). Returns (acc, down, order)."""
    order = sorted(range(w * h), key=lambda k: hf[k], reverse=True)
    down = [-1] * (w * h)
    for k in order:
        j = k // w; i = k % w; lh = hf[k]; low = -1
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            ii = i + di; jj = j + dj
            if 0 <= ii < w and 0 <= jj < h:
                nk = jj * w + ii
                if hf[nk] < lh:
                    lh = hf[nk]; low = nk
        down[k] = low
    acc = [1.0] * (w * h)
    for k in order:
        d = down[k]
        if d >= 0:
            acc[d] += acc[k]
    return acc, down, order


def _hydrology_channels(hf, w, h, density, amp_h, seed):
    """Carve drainage channels where flow accumulation is high."""
    acc, down, order = _flow_accum(hf, w, h)
    mx = max(acc) if acc else 1.0
    thr = mx * (1.0 - min(0.95, max(0.05, density))) * 0.15 + mx * 0.02
    carve = amp_h * 0.15 * max(0.05, min(1.0, density))
    denom = (mx - thr) if (mx - thr) > 1e-6 else 1.0
    for k in range(w * h):
        if acc[k] > thr:
            f = min(1.0, (acc[k] - thr) / denom)
            hf[k] -= carve * f


def _smoothstep_down(t, inner):
    """1 at t<=inner, smooth 1->0 by t=1."""
    if t >= 1.0:
        return 0.0
    inner = max(0.0, min(0.95, inner))
    if t <= inner:
        return 1.0
    u = (t - inner) / (1.0 - inner)
    return 1.0 - (u * u * (3.0 - 2.0 * u))


def _clampi(v, lo, hi):
    return max(lo, min(hi, int(v)))


def _as_list(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            j = json.loads(s)
            return list(j) if isinstance(j, (list, tuple)) else [j]
        except Exception:
            parts = [p for p in s.replace(";", ",").split(",") if p.strip() != ""]
            return [float(p) for p in parts]
    return [v]


def _parse_points(points):
    if isinstance(points, str):
        points = json.loads(points)
    out = []
    for p in points:
        out.append([float(c) for c in p])
    return out


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        resp = send_command("execute_python", {"code": _wrap(code)})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError("execute_python did not succeed: %s" % (resp,))
        out = resp.get("result", {}).get("output", "").replace("\r\n", "\n")
        lines = out.splitlines()
        warns = []
        for line in lines:
            if LOG_MARKER in line:
                try:
                    warns = json.loads(line.split(LOG_MARKER, 1)[1])
                except Exception:
                    pass
        for line in reversed(lines):
            if MARKER in line:
                result = json.loads(line.split(MARKER, 1)[1])
                if warns and isinstance(result, dict):
                    result["_log_warnings"] = warns
                return result
        raise RuntimeError("no %s payload in output:\n%s" % (MARKER, out))

    def _exec(body, params):
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Shared Unreal-side helpers (prepended to every body). Session-aware ledger + bridge accessor +
    # base64<->uint16/uint8 packers + actor lookup. NO ''' and NO backslashes anywhere inside.
    _COERCE_HELPERS = r'''
import unreal, json, builtins, struct, math
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _lib():
    return unreal.MCPReflectionLibrary
def _b64_to_u16(b):
    raw = _b64.b64decode(b) if b else b""
    n = len(raw) // 2
    return list(struct.unpack("<%dH" % n, raw)) if n else []
def _u16_to_b64(vals):
    raw = struct.pack("<%dH" % len(vals), *[max(0, min(65535, int(v))) for v in vals])
    return _b64.b64encode(raw).decode("ascii")
def _b64_to_u8(b):
    raw = _b64.b64decode(b) if b else b""
    return list(raw)
def _u8_to_b64(vals):
    raw = bytes([max(0, min(255, int(v))) for v in vals])
    return _b64.b64encode(raw).decode("ascii")
def _find_actor(name):
    try:
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        for a in eas.get_all_level_actors():
            try:
                if a.get_name() == name or a.get_actor_label() == name:
                    return a
            except Exception:
                pass
    except Exception:
        pass
    return None
'''

    # ------------------------------------------------------------------ #
    # Reusable READ body: info + a height region (default whole extent) + actor world location.
    # ------------------------------------------------------------------ #
    _READ_BODY = _COERCE_HELPERS + r'''
actor = PARAMS["actor_name"]
info = json.loads(_lib().landscape_get_info_json(actor))
if "error" in info:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": info.get("error")}))
else:
    have = ("x" in PARAMS) and ("y" in PARAMS) and ("w" in PARAMS) and ("h" in PARAMS)
    if have:
        rx = int(PARAMS["x"]); ry = int(PARAMS["y"]); rw = int(PARAMS["w"]); rh = int(PARAMS["h"])
    else:
        rx = int(info["min_x"]); ry = int(info["min_y"]); rw = int(info["size_x"]); rh = int(info["size_y"])
    rd = json.loads(_lib().landscape_get_height_region_json(actor, rx, ry, rw, rh))
    if "error" in rd:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": rd.get("error")}))
    else:
        a = _find_actor(actor); loc = [0.0, 0.0, 0.0]
        if a is not None:
            try:
                v = a.get_actor_location(); loc = [float(v.x), float(v.y), float(v.z)]
            except Exception:
                pass
        print("@@UMCP@@" + json.dumps({"status": "success", "info": info,
            "region": {"x": rd["x"], "y": rd["y"], "w": rd["w"], "h": rd["h"], "heights_b64": rd["heights_b64"]},
            "actor_loc": loc}))
'''

    # ------------------------------------------------------------------ #
    # Reusable WRITE body: write new heights over an IN-BOUNDS region, capture prior, ledger.
    # ------------------------------------------------------------------ #
    _WRITE_BODY = _COERCE_HELPERS + r'''
actor = PARAMS["actor_name"]
x = int(PARAMS["x"]); y = int(PARAMS["y"]); w = int(PARAMS["w"]); h = int(PARAMS["h"])
new_b64 = PARAMS["heights_b64"]
rd = json.loads(_lib().landscape_get_height_region_json(actor, x, y, w, h))
if "error" in rd:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": rd.get("error")}))
else:
    ax = rd["x"]; ay = rd["y"]; aw = rd["w"]; ah = rd["h"]
    prev_b64 = rd["heights_b64"]
    if aw != w or ah != h:
        print("@@UMCP@@" + json.dumps({"status": "error",
            "message": "region clamped to %dx%d from %dx%d; re-issue in-bounds" % (aw, ah, w, h)}))
    else:
        wr = json.loads(_lib().landscape_set_height_region_json(actor, ax, ay, aw, ah, new_b64, False))
        if "error" in wr:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": wr.get("error")}))
        else:
            with unreal.ScopedEditorTransaction("MCP landscape_advanced height"):
                pass
            _ledger().append({"op": "landscape_set_height_region", "actor_name": actor,
                              "x": ax, "y": ay, "w": aw, "h": ah, "prev_b64": prev_b64})
            vals = _b64_to_u16(new_b64)
            print("@@UMCP@@" + json.dumps({"status": "success", "actor_name": actor,
                "x": ax, "y": ay, "w": aw, "h": ah, "cells": len(vals),
                "height_min_u16": (min(vals) if vals else 0), "height_max_u16": (max(vals) if vals else 0),
                "undo_op": "landscape_set_height_region", "ledger_depth": len(_ledger())}))
'''

    # ------------------------------------------------------------------ #
    # Reusable PAINT body: paint one full-extent weight region for a layer (host-computed uint8).
    # ------------------------------------------------------------------ #
    _PAINT_BODY = _COERCE_HELPERS + r'''
actor = PARAMS["actor_name"]
layer = PARAMS["layer_name"]
x = int(PARAMS["x"]); y = int(PARAMS["y"]); w = int(PARAMS["w"]); h = int(PARAMS["h"])
wb64 = PARAMS["weights_b64"]
pkg = PARAMS.get("package_path", "/Game/MCP_Scratch")
explicit_li = PARAMS.get("layer_info_path") or ""
info = json.loads(_lib().landscape_get_info_json(actor))
if "error" in info:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": info.get("error")}))
else:
    existing = None
    for L in info.get("layers", []):
        if L.get("name") == layer:
            existing = L
            break
    layer_info_path = ""
    created_asset = None
    if explicit_li:
        layer_info_path = explicit_li
    elif existing and existing.get("has_layer_info"):
        layer_info_path = existing.get("layer_info_path", "")
    else:
        aname = "MCP_A_" + layer
        full = pkg + "/" + aname
        if unreal.EditorAssetLibrary.does_asset_exist(full):
            layer_info_path = full + "." + aname
        else:
            if not unreal.EditorAssetLibrary.does_directory_exist(pkg):
                unreal.EditorAssetLibrary.make_directory(pkg)
            at = unreal.AssetToolsHelpers.get_asset_tools()
            asset = at.create_asset(aname, pkg, unreal.LandscapeLayerInfoObject, None)
            if asset is None:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": "create_asset returned None for %s" % full}))
                layer_info_path = None
            else:
                unreal.EditorAssetLibrary.save_asset(full, only_if_is_dirty=False)
                layer_info_path = asset.get_path_name()
                created_asset = full
                _ledger().append({"op": "create_asset", "asset_path": full, "package_path": pkg, "created_dir": None})
    if layer_info_path is None:
        pass
    else:
        res = json.loads(_lib().landscape_paint_weight_region_json(actor, layer, x, y, w, h, wb64, layer_info_path))
        if "error" in res:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": res["error"], "created_asset": created_asset}))
        else:
            with unreal.ScopedEditorTransaction("MCP landscape_advanced paint"):
                pass
            _ledger().append({"op": "landscape_paint_weight_region", "actor_name": actor,
                              "layer_name": layer, "x": res["x"], "y": res["y"], "w": res["w"], "h": res["h"],
                              "prev_b64": res["prev_b64"], "layer_info_path": res.get("layer_info_path", layer_info_path)})
            res["status"] = "success"
            res["created_asset"] = created_asset
            res["undo_op"] = "landscape_paint_weight_region"
            res["ledger_depth"] = len(_ledger())
            res.pop("prev_b64", None)
            print("@@UMCP@@" + json.dumps(res))
'''

    # ------------------------------------------------------------------ #
    # clear_landscape_splines body (READ + best-effort maintenance; NON-ledgered).
    # ------------------------------------------------------------------ #
    _CLEAR_SPLINES_BODY = _COERCE_HELPERS + r'''
actor = PARAMS["actor_name"]
a = _find_actor(actor)
if a is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "actor not found: %s" % actor}))
else:
    comp = None
    try:
        for c in a.get_components_by_class(unreal.LandscapeSplinesComponent):
            comp = c
            break
    except Exception:
        comp = None
    if comp is None:
        try:
            comp = a.get_editor_property("splines_component")
        except Exception:
            comp = None
    n_control = None
    n_seg = None
    if comp is not None:
        for prop in ("control_points",):
            try:
                cp = comp.get_editor_property(prop)
                n_control = len(cp) if cp is not None else 0
                break
            except Exception:
                n_control = None
        for prop in ("segments",):
            try:
                sg = comp.get_editor_property(prop)
                n_seg = len(sg) if sg is not None else 0
                break
            except Exception:
                n_seg = None
    print("@@UMCP@@" + json.dumps({"status": "success", "actor_name": actor,
        "has_splines_component": comp is not None,
        "control_points": n_control, "segments": n_seg,
        "cleared": 0, "ledgered": False,
        "note": "non-ledgered maintenance; our tools create no persistent splines so nothing to clear"}))
'''

    def _read_field(actor_name, x=None, y=None, w=None, h=None):
        p = {"actor_name": actor_name}
        if None not in (x, y, w, h):
            p.update({"x": int(x), "y": int(y), "w": int(w), "h": int(h)})
        return _exec(_READ_BODY, p)

    def _write_field(actor_name, x, y, w, h, vals):
        return _exec(_WRITE_BODY, {"actor_name": actor_name, "x": int(x), "y": int(y),
                                   "w": int(w), "h": int(h), "heights_b64": _u16_to_b64(vals)})

    def _extent(info):
        mnx = int(info["min_x"]); mny = int(info["min_y"])
        mxx = int(info["max_x"]); mxy = int(info["max_y"])
        return mnx, mny, mxx, mxy

    def _scale_z(info):
        ds = info.get("draw_scale")
        if isinstance(ds, list) and len(ds) >= 3 and ds[2]:
            return float(ds[2])
        return 100.0

    def _scale_xy(info):
        ds = info.get("draw_scale")
        sx = float(ds[0]) if isinstance(ds, list) and len(ds) >= 1 and ds[0] else 100.0
        sy = float(ds[1]) if isinstance(ds, list) and len(ds) >= 2 and ds[1] else 100.0
        return sx, sy

    # ================================================================== #
    # 1) generate_terrain                                                #
    # ================================================================== #
    @mcp.tool()
    def generate_terrain(ctx, actor_name: str, seed: int = 1337, amplitude: float = 6000.0,
                         sharpness: float = 1.35, warp: float = 0.35, feature_scale: float = None,
                         octaves: int = 6, ridged: bool = True, detail: float = 0.25,
                         uplift: float = 0.0, erosion: float = 0.4, erosion_radius: int = 2,
                         talus_angle: float = 35.0, thermal_iterations: int = 4,
                         hydrology: bool = False, channel_density: float = 0.3) -> str:
        """Generate a FULL eroded terrain over the whole landscape (whole heightmap computed HOST-SIDE).

        Pipeline: seeded ridged/fBm base + domain warp -> thermal (talus) smoothing -> hydraulic
        droplet erosion -> optional hydrology drainage channels. All math runs in this MCP process;
        the editor only reads the prior whole region (captured for undo) and writes the result.

        seed:               master RNG seed (deterministic).
        amplitude:          peak relief in world cm (default 6000).
        sharpness:          gamma exponent sharpening peaks/valleys (>1 sharper; default 1.35).
        warp:               domain-warp strength 0..~1 (default 0.35).
        feature_scale:      vertices per primary feature (default min(size)/3.5).
        octaves/ridged:     fBm octaves and ridged-vs-billowy base (defaults 6 / True).
        detail:             0..1 high-frequency detail blend (default 0.25).
        uplift:             constant cm added to the whole field (default 0).
        erosion:            0..1 hydraulic intensity -> droplet count (default 0.4; 0 disables).
        erosion_radius:     droplet deposit radius hint (default 2).
        talus_angle:        thermal repose angle in degrees (default 35).
        thermal_iterations: thermal passes (default 4, capped 40; 0 disables).
        hydrology:          carve drainage channels (default False).
        channel_density:    0..1 channel aggressiveness when hydrology (default 0.3).

        Ledgered write (op 'landscape_set_height_region', EXISTING undo branch): undo re-writes the
        captured prior whole region -> restores flat/original."""
        try:
            rd = _read_field(actor_name)
            if rd.get("status") != "success":
                return json.dumps(rd, indent=2)
            info = rd["info"]; reg = rd["region"]
            w = int(reg["w"]); h = int(reg["h"])
            sz = _scale_z(info); sx, _sy = _scale_xy(info)
            P = {"seed": seed, "amplitude": amplitude, "sharpness": sharpness, "warp": warp,
                 "feature_scale": feature_scale, "octaves": octaves, "ridged": ridged,
                 "detail": detail, "uplift": uplift}
            hf, amp_h = _base_terrain(w, h, sz, sx, P)
            if thermal_iterations and thermal_iterations > 0:
                talus = math.tan(math.radians(max(1.0, min(85.0, float(talus_angle))))) * sx * 128.0 / (sz if sz else 1.0)
                _thermal_erode(hf, w, h, talus, min(40, int(thermal_iterations)))
            if erosion and erosion > 0:
                _hydraulic_erode(hf, w, h, float(erosion), int(erosion_radius), int(seed), amp_h)
            if hydrology and channel_density and channel_density > 0:
                _hydrology_channels(hf, w, h, float(channel_density), amp_h, int(seed))
            vals = [max(0, min(65535, int(round(v)))) for v in hf]
            res = _write_field(actor_name, reg["x"], reg["y"], w, h, vals)
            if isinstance(res, dict):
                res["generated"] = {"w": w, "h": h, "amplitude_cm": amplitude, "seed": seed,
                                    "thermal_iterations": int(thermal_iterations), "erosion": float(erosion),
                                    "hydrology": bool(hydrology)}
            return json.dumps(res, indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ================================================================== #
    # 2) edit_terrain_region                                             #
    # ================================================================== #
    @mcp.tool()
    def edit_terrain_region(ctx, actor_name: str, operation: str, center=None, radius: float = None,
                            falloff: float = 0.4, amount: float = 200.0, height: float = None,
                            use_height: bool = False, iterations: int = 1, step_height: float = 400.0,
                            feature_scale: float = None, seed: int = 1337) -> str:
        """Apply ONE operation to a circular region (VERTEX-space center/radius). Computed HOST-SIDE.

        operation: raise|lower|flatten|smooth|noise|ridge|terrace.
        center:    [vx,vy] vertex coords (default landscape center). Accepts list or "vx,vy".
        radius:    region radius in vertices (default min(size)/4).
        falloff:   0..0.95 inner plateau fraction; weight smoothsteps 1->0 to the radius edge.
        amount:    magnitude in world cm for raise/lower/noise/ridge (default 200).
        height:    absolute target height (world cm) for flatten when use_height (else region mean).
        use_height:True -> flatten uses `height`; else the region's mean.
        iterations:smooth passes (default 1).
        step_height: terrace band height in world cm (default 400).
        feature_scale: vertices per feature for noise/ridge (default radius/2).
        seed:      RNG seed for noise/ridge.

        Ledgered write (op 'landscape_set_height_region', EXISTING branch): undo re-writes prior region."""
        try:
            op = str(operation).lower()
            if op not in ("raise", "lower", "flatten", "smooth", "noise", "ridge", "terrace"):
                return "Error: operation must be raise|lower|flatten|smooth|noise|ridge|terrace"
            rd = _read_field(actor_name)
            if rd.get("status") != "success":
                return json.dumps(rd, indent=2)
            info = rd["info"]
            mnx, mny, mxx, mxy = _extent(info)
            sizex = mxx - mnx + 1; sizey = mxy - mny + 1
            sz = _scale_z(info)
            cen = _as_list(center)
            if cen and len(cen) >= 2:
                cx = float(cen[0]); cy = float(cen[1])
            else:
                cx = mnx + sizex / 2.0; cy = mny + sizey / 2.0
            r = float(radius) if radius else max(2.0, min(sizex, sizey) / 4.0)
            # bbox clamped to extent
            x0 = _clampi(math.floor(cx - r), mnx, mxx); x1 = _clampi(math.ceil(cx + r), mnx, mxx)
            y0 = _clampi(math.floor(cy - r), mny, mxy); y1 = _clampi(math.ceil(cy + r), mny, mxy)
            bw = x1 - x0 + 1; bh = y1 - y0 + 1
            if bw < 1 or bh < 1:
                return "Error: region is outside the landscape extent"
            rd2 = _read_field(actor_name, x0, y0, bw, bh)
            if rd2.get("status") != "success":
                return json.dumps(rd2, indent=2)
            reg = rd2["region"]; ax = int(reg["x"]); ay = int(reg["y"]); aw = int(reg["w"]); ah = int(reg["h"])
            cur = _b64_to_u16(reg["heights_b64"])
            new = list(cur)
            amt_h = float(amount) * 128.0 / (sz if sz else 1.0)
            fs = float(feature_scale) if feature_scale else max(2.0, r / 2.0)
            rng = random.Random(int(seed) & 0xFFFFFFFF)
            # precompute region mean for flatten default
            if op == "flatten":
                if use_height and height is not None:
                    tgt = 32768.0 + float(height) * 128.0 / (sz if sz else 1.0)
                else:
                    inside = []
                    for jj in range(ah):
                        for ii in range(aw):
                            gx = ax + ii; gy = ay + jj
                            if math.hypot(gx - cx, gy - cy) <= r:
                                inside.append(cur[jj * aw + ii])
                    tgt = (sum(inside) / float(len(inside))) if inside else 32768.0
            passes = max(1, int(iterations)) if op == "smooth" else 1
            for _p in range(passes):
                src = list(new)
                for jj in range(ah):
                    for ii in range(aw):
                        gx = ax + ii; gy = ay + jj
                        dist = math.hypot(gx - cx, gy - cy)
                        wgt = _smoothstep_down(dist / r, falloff) if r > 0 else 0.0
                        if wgt <= 0.0:
                            continue
                        k = jj * aw + ii
                        if op == "raise":
                            new[k] = src[k] + amt_h * wgt
                        elif op == "lower":
                            new[k] = src[k] - amt_h * wgt
                        elif op == "flatten":
                            new[k] = src[k] + (tgt - src[k]) * wgt
                        elif op == "smooth":
                            acc = 0.0; cnt = 0
                            for dj in (-1, 0, 1):
                                for di in (-1, 0, 1):
                                    a2 = ii + di; b2 = jj + dj
                                    if 0 <= a2 < aw and 0 <= b2 < ah:
                                        acc += src[b2 * aw + a2]; cnt += 1
                                avg = acc / float(cnt) if cnt else src[k]
                            new[k] = src[k] + (avg - src[k]) * wgt
                        elif op == "noise":
                            n = _vnoise(gx / fs + 0.5, gy / fs + 0.5, int(seed))
                            new[k] = src[k] + (n - 0.5) * 2.0 * amt_h * wgt
                        elif op == "ridge":
                            n = _fbm(gx / fs, gy / fs, int(seed), 4, True, 1.3)
                            new[k] = src[k] + n * amt_h * wgt
                        elif op == "terrace":
                            band = max(1.0, float(step_height) * 128.0 / (sz if sz else 1.0))
                            q = round(src[k] / band) * band
                            new[k] = src[k] + (q - src[k]) * wgt
            vals = [max(0, min(65535, int(round(v)))) for v in new]
            res = _write_field(actor_name, ax, ay, aw, ah, vals)
            if isinstance(res, dict):
                res["operation"] = op
                res["center"] = [cx, cy]
                res["radius"] = r
            return json.dumps(res, indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ================================================================== #
    # 3) regenerate_terrain_region                                       #
    # ================================================================== #
    @mcp.tool()
    def regenerate_terrain_region(ctx, actor_name: str, center=None, radius: float = None,
                                  blend: str = "feather", seed: int = 1337, amplitude: float = 6000.0,
                                  sharpness: float = 1.35, warp: float = 0.35, feature_scale: float = None,
                                  erosion: float = 0.0, talus_angle: float = 35.0,
                                  thermal_iterations: int = 2, falloff_width: float = None) -> str:
        """Regenerate procedural terrain inside a circular region and SEAMLESSLY feather-blend it into
        the surrounding existing heights (no seam at the border). Computed HOST-SIDE.

        center/radius:  region in VERTEX coords (defaults: center of landscape, min(size)/4).
        blend:          'feather' (smoothstep border blend; default) or 'poisson' (approximated as a
                        wider feather). At the border the blend weight is 0 so heights match exactly.
        falloff_width:  vertices over which the blend ramps to 0 at the border (default radius*0.4).
        seed/amplitude/sharpness/warp/feature_scale: base-terrain controls (see generate_terrain).
        erosion/talus_angle/thermal_iterations: optional local thermal/hydraulic touch-up.

        Ledgered write (op 'landscape_set_height_region', EXISTING branch): undo restores prior region."""
        try:
            rd = _read_field(actor_name)
            if rd.get("status") != "success":
                return json.dumps(rd, indent=2)
            info = rd["info"]
            mnx, mny, mxx, mxy = _extent(info)
            sizex = mxx - mnx + 1; sizey = mxy - mny + 1
            sz = _scale_z(info); sx, _sy = _scale_xy(info)
            cen = _as_list(center)
            if cen and len(cen) >= 2:
                cx = float(cen[0]); cy = float(cen[1])
            else:
                cx = mnx + sizex / 2.0; cy = mny + sizey / 2.0
            r = float(radius) if radius else max(2.0, min(sizex, sizey) / 4.0)
            fw = float(falloff_width) if falloff_width else max(1.0, r * 0.4)
            x0 = _clampi(math.floor(cx - r), mnx, mxx); x1 = _clampi(math.ceil(cx + r), mnx, mxx)
            y0 = _clampi(math.floor(cy - r), mny, mxy); y1 = _clampi(math.ceil(cy + r), mny, mxy)
            bw = x1 - x0 + 1; bh = y1 - y0 + 1
            rd2 = _read_field(actor_name, x0, y0, bw, bh)
            if rd2.get("status") != "success":
                return json.dumps(rd2, indent=2)
            reg = rd2["region"]; ax = int(reg["x"]); ay = int(reg["y"]); aw = int(reg["w"]); ah = int(reg["h"])
            cur = _b64_to_u16(reg["heights_b64"])
            # Generate a base field over the bbox in ABSOLUTE-coord space so features are stable.
            P = {"seed": seed, "amplitude": amplitude, "sharpness": sharpness, "warp": warp,
                 "feature_scale": feature_scale, "octaves": 6, "ridged": True, "detail": 0.25, "uplift": 0.0}
            # build absolute-coordinate generated field for the bbox
            fs = P["feature_scale"] if P["feature_scale"] else max(6.0, min(sizex, sizey) / 3.5)
            fs = float(fs)
            amp_h = amplitude * 128.0 / (sz if sz else 1.0)
            gen = [0.0] * (aw * ah)
            # anchor generated mean to the local existing mean so feathered interior sits naturally
            local_mean = (sum(cur) / float(len(cur))) if cur else 32768.0
            for jj in range(ah):
                for ii in range(aw):
                    gx = (ax + ii); gy = (ay + jj)
                    fx = gx / fs; fy = gy / fs
                    if warp:
                        wx = _vnoise(fx * 0.5 + 11.2, fy * 0.5 + 3.7, int(seed) + 91)
                        wy = _vnoise(fx * 0.5 + 5.1, fy * 0.5 + 9.3, int(seed) + 57)
                        fx = fx + warp * (wx - 0.5) * 2.0
                        fy = fy + warp * (wy - 0.5) * 2.0
                    n = _fbm(fx, fy, int(seed), 6, True, sharpness)
                    gen[jj * aw + ii] = local_mean + amp_h * (n - 0.5)
            if thermal_iterations and thermal_iterations > 0:
                talus = math.tan(math.radians(max(1.0, min(85.0, float(talus_angle))))) * sx * 128.0 / (sz if sz else 1.0)
                _thermal_erode(gen, aw, ah, talus, min(20, int(thermal_iterations)))
            if erosion and erosion > 0:
                _hydraulic_erode(gen, aw, ah, float(erosion), 2, int(seed), amp_h)
            wide = 1.0 if str(blend).lower() == "poisson" else 0.0
            new = list(cur)
            for jj in range(ah):
                for ii in range(aw):
                    gx = ax + ii; gy = ay + jj
                    dist = math.hypot(gx - cx, gy - cy)
                    if dist >= r:
                        continue
                    # weight 1 in the core, ramps to 0 across falloff_width at the border
                    edge = r - dist
                    w_blend = min(1.0, edge / fw) if fw > 0 else 1.0
                    if wide:
                        w_blend = w_blend * w_blend * (3.0 - 2.0 * w_blend)
                    k = jj * aw + ii
                    new[k] = cur[k] * (1.0 - w_blend) + gen[k] * w_blend
            vals = [max(0, min(65535, int(round(v)))) for v in new]
            res = _write_field(actor_name, ax, ay, aw, ah, vals)
            if isinstance(res, dict):
                res["center"] = [cx, cy]; res["radius"] = r; res["blend"] = str(blend)
            return json.dumps(res, indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ================================================================== #
    # 4) apply_terrain_process                                           #
    # ================================================================== #
    @mcp.tool()
    def apply_terrain_process(ctx, actor_name: str, process: str, snow_line: float = None,
                              sea_level: float = None, repose_angle_degrees: float = 35.0,
                              iterations: int = 3, bench_width: float = 500.0,
                              snow_depth: float = 800.0, tilt_degrees: float = 0.0,
                              seed: int = 1337) -> str:
        """Apply a geomorphic process to the whole landscape. Computed HOST-SIDE.

        process:
          glacial  -> U-valley carving: strong thermal smoothing + lower high-flow valleys.
          snow     -> accumulate a slope-limited snow cap above snow_line (uses repose_angle_degrees).
          coastal  -> flatten toward sea_level near/below it (beaches + wave-cut platform).
          stratify -> horizontal bench/terrace steps of bench_width.

        snow_line / sea_level: world-cm thresholds (default: 60% / 20% of the current height range).
        repose_angle_degrees:  talus angle for snow redistribution / glacial (default 35).
        iterations:            process passes (default 3, capped 30).
        bench_width:           terrace band height in world cm for stratify (default 500).
        snow_depth:            peak snow accumulation in world cm for snow (default 800).
        tilt_degrees:          optional linear tilt (X) applied by stratify banding phase.

        Ledgered write (op 'landscape_set_height_region', EXISTING branch): undo restores prior."""
        try:
            proc = str(process).lower()
            if proc not in ("glacial", "snow", "coastal", "stratify"):
                return "Error: process must be glacial|snow|coastal|stratify"
            rd = _read_field(actor_name)
            if rd.get("status") != "success":
                return json.dumps(rd, indent=2)
            info = rd["info"]; reg = rd["region"]
            w = int(reg["w"]); h = int(reg["h"])
            sz = _scale_z(info); sx, _sy = _scale_xy(info)
            hf = [float(v) for v in _b64_to_u16(reg["heights_b64"])]
            hmin = min(hf) if hf else 32768.0; hmax = max(hf) if hf else 32768.0
            rng = math.tan(math.radians(max(1.0, min(85.0, float(repose_angle_degrees))))) * sx * 128.0 / (sz if sz else 1.0)
            it = min(30, max(1, int(iterations)))
            if proc == "glacial":
                _thermal_erode(hf, w, h, rng * 0.6, it * 3)
                acc, down, order = _flow_accum(hf, w, h)
                mx = max(acc) if acc else 1.0
                carve = (hmax - hmin) * 0.12 + 1.0
                for k in range(w * h):
                    f = min(1.0, acc[k] / (mx * 0.25 + 1e-6))
                    hf[k] -= carve * f * 0.5
                _thermal_erode(hf, w, h, rng * 0.4, it)
            elif proc == "snow":
                sl = (32768.0 + float(snow_line) * 128.0 / (sz if sz else 1.0)) if snow_line is not None else (hmin + (hmax - hmin) * 0.6)
                dep = float(snow_depth) * 128.0 / (sz if sz else 1.0)
                span = (hmax - sl) if (hmax - sl) > 1e-6 else 1.0
                for k in range(w * h):
                    if hf[k] > sl:
                        f = min(1.0, (hf[k] - sl) / span)
                        hf[k] += dep * f
                _thermal_erode(hf, w, h, rng, it * 2)
            elif proc == "coastal":
                sea = (32768.0 + float(sea_level) * 128.0 / (sz if sz else 1.0)) if sea_level is not None else (hmin + (hmax - hmin) * 0.2)
                beach = (hmax - hmin) * 0.06 + 1.0
                for _p in range(it):
                    for k in range(w * h):
                        if hf[k] < sea:
                            hf[k] += (sea - hf[k]) * 0.5
                        elif hf[k] < sea + beach:
                            f = 1.0 - (hf[k] - sea) / beach
                            hf[k] += (sea - hf[k]) * 0.25 * f
            elif proc == "stratify":
                band = max(1.0, float(bench_width) * 128.0 / (sz if sz else 1.0))
                tilt_h = math.tan(math.radians(float(tilt_degrees))) * sx * 128.0 / (sz if sz else 1.0) if tilt_degrees else 0.0
                for j in range(h):
                    for i in range(w):
                        k = j * w + i
                        v = hf[k] + tilt_h * i
                        q = round(v / band) * band - tilt_h * i
                        hf[k] = hf[k] + (q - hf[k]) * 0.85
            vals = [max(0, min(65535, int(round(v)))) for v in hf]
            res = _write_field(actor_name, reg["x"], reg["y"], w, h, vals)
            if isinstance(res, dict):
                res["process"] = proc; res["iterations"] = it
            return json.dumps(res, indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ================================================================== #
    # 5) paint_landscape_layers                                          #
    # ================================================================== #
    @mcp.tool()
    def paint_landscape_layers(ctx, actor_name: str, layers, layer_info_path: str = None,
                               slope_smoothing: int = 0, edge_noise: float = 0.0,
                               package_path: str = "/Game/MCP_Scratch", seed: int = 1337) -> str:
        """Paint ALL layers in one pass from per-vertex slope/height/flat/noise rules. Weights are
        computed HOST-SIDE from the heightmap; each layer is painted full-extent via the weight bridge.

        layers: JSON string (or list) of {name, rule, threshold, weight?, layer_info_path?}.
                rule in slope|height|flat|noise:
                  slope  -> weight where terrain STEEPNESS (deg) >= threshold.
                  height -> weight where WORLD-Z height (cm) >= threshold.
                  flat   -> weight where steepness (deg) <= threshold.
                  noise  -> weight where a seeded value-noise field >= threshold (0..1).
                Optional per-layer 'weight' (0..255 peak, default 255).
        slope_smoothing: box-blur passes over the computed weights (default 0).
        edge_noise:      0..1 dither at layer boundaries (default 0).
        package_path:    content folder for any auto-created LayerInfo (default /Game/MCP_Scratch).

        Ledgered writes: one op 'landscape_paint_weight_region' per layer (EXISTING branch; undo
        re-paints prior). Any created LayerInfo is also ledgered op 'create_asset' (EXISTING branch)."""
        try:
            spec = layers
            if isinstance(spec, str):
                spec = json.loads(spec)
            if not isinstance(spec, list) or not spec:
                return "Error: layers must be a non-empty JSON list of {name, rule, threshold, ...}"
            rd = _read_field(actor_name)
            if rd.get("status") != "success":
                return json.dumps(rd, indent=2)
            info = rd["info"]; reg = rd["region"]
            w = int(reg["w"]); h = int(reg["h"])
            sz = _scale_z(info); sx, sy = _scale_xy(info)
            loc = rd.get("actor_loc", [0.0, 0.0, 0.0])
            hf = [float(v) for v in _b64_to_u16(reg["heights_b64"])]

            # per-vertex slope (degrees) via central differences in world units
            def _slope_deg(i, j):
                k = j * w + i
                il = hf[k - 1] if i > 0 else hf[k]; ir = hf[k + 1] if i < w - 1 else hf[k]
                jl = hf[k - w] if j > 0 else hf[k]; jr = hf[k + w] if j < h - 1 else hf[k]
                dzx = (ir - il) * (sz / 128.0) / (2.0 * sx if sx else 1.0)
                dzy = (jr - jl) * (sz / 128.0) / (2.0 * sy if sy else 1.0)
                return math.degrees(math.atan(math.hypot(dzx, dzy)))

            results = []
            for spec_layer in spec:
                name = spec_layer.get("name")
                if not name:
                    results.append({"status": "error", "message": "layer missing name"})
                    continue
                rule = str(spec_layer.get("rule", "flat")).lower()
                thr = float(spec_layer.get("threshold", 0.0))
                peak = int(spec_layer.get("weight", 255))
                li = spec_layer.get("layer_info_path", layer_info_path)
                rng = random.Random((int(seed) ^ (hash(name) & 0xFFFF)) & 0xFFFFFFFF)
                wts = [0.0] * (w * h)
                for j in range(h):
                    for i in range(w):
                        k = j * w + i
                        if rule == "height":
                            worldz = (hf[k] - 32768.0) / 128.0 * sz + float(loc[2])
                            val = 1.0 if worldz >= thr else 0.0
                        elif rule == "slope":
                            val = 1.0 if _slope_deg(i, j) >= thr else 0.0
                        elif rule == "flat":
                            val = 1.0 if _slope_deg(i, j) <= thr else 0.0
                        elif rule == "noise":
                            fs = max(4.0, min(w, h) / 6.0)
                            val = 1.0 if _vnoise(i / fs + 0.3, j / fs + 0.7, int(seed) + 7) >= thr else 0.0
                        else:
                            val = 1.0
                        if edge_noise and val > 0.0:
                            val = max(0.0, val - rng.random() * float(edge_noise))
                        wts[k] = val * peak
                for _s in range(max(0, int(slope_smoothing))):
                    src = list(wts)
                    for j in range(h):
                        for i in range(w):
                            acc = 0.0; cnt = 0
                            for dj in (-1, 0, 1):
                                for di in (-1, 0, 1):
                                    a2 = i + di; b2 = j + dj
                                    if 0 <= a2 < w and 0 <= b2 < h:
                                        acc += src[b2 * w + a2]; cnt += 1
                            wts[j * w + i] = acc / float(cnt) if cnt else src[j * w + i]
                weights = [max(0, min(255, int(round(v)))) for v in wts]
                p = {"actor_name": actor_name, "layer_name": name,
                     "x": int(reg["x"]), "y": int(reg["y"]), "w": w, "h": h,
                     "weights_b64": _u8_to_b64(weights), "package_path": package_path,
                     "layer_info_path": (li or "")}
                res = _exec(_PAINT_BODY, p)
                if isinstance(res, dict):
                    res["rule"] = rule; res["threshold"] = thr
                    res["painted_cells"] = sum(1 for v in weights if v > 0)
                results.append(res)
            return json.dumps({"status": "success", "actor_name": actor_name,
                               "layers_painted": len(results), "results": results}, indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ================================================================== #
    # 6) add_landscape_spline                                            #
    # ================================================================== #
    @mcp.tool()
    def add_landscape_spline(ctx, actor_name: str, points, width: float = 600.0,
                             side_falloff: float = 400.0, end_falloff: float = 0.0,
                             raise_terrain: bool = True, lower_terrain: bool = True, apply: bool = True,
                             layer_name: str = None, height_offset: float = 0.0,
                             point_spacing: float = None, height_smoothing: int = 1,
                             package_path: str = "/Game/MCP_Scratch") -> str:
        """Deform the heightmap along a polyline path HOST-SIDE (the engine's editor_apply_spline was
        PROVEN unreliable, so this is height-based, NOT a persistent spline object).

        points:        JSON string (or list) of [x,y,z] WORLD coords. z is the target path height (cm).
        width:         full flatten width in world cm (path centerline height wins within width/2).
        side_falloff:  world-cm blend band outside the core where terrain eases to the path height.
        end_falloff:   taper (cm) at the two path ends (default 0).
        raise_terrain/lower_terrain: gate whether the path may push terrain up / down (default both).
        apply:         if False, dry-run (compute + report affected cells, write nothing/no ledger).
        layer_name:    optional layer to paint along the path (full weight in the core).
        height_offset: cm added to every path target height.
        point_spacing: resample spacing in world cm along segments (default width/2).
        height_smoothing: smoothing passes over the resampled path heights (default 1).

        Ledgered write (op 'landscape_set_height_region', EXISTING branch) when apply; plus an optional
        op 'landscape_paint_weight_region' if layer_name. undo restores prior height (and re-paints)."""
        try:
            pts = _parse_points(points)
            if len(pts) < 2:
                return "Error: need at least 2 points"
            rd = _read_field(actor_name)
            if rd.get("status") != "success":
                return json.dumps(rd, indent=2)
            info = rd["info"]; reg = rd["region"]
            w = int(reg["w"]); h = int(reg["h"])
            mnx = int(reg["x"]); mny = int(reg["y"])
            sz = _scale_z(info); sx, sy = _scale_xy(info)
            loc = rd.get("actor_loc", [0.0, 0.0, 0.0])
            hf = [float(v) for v in _b64_to_u16(reg["heights_b64"])]

            def _to_vertex(px, py):
                vx = (px - float(loc[0])) / (sx if sx else 1.0)
                vy = (py - float(loc[1])) / (sy if sy else 1.0)
                return vx, vy

            def _to_u16z(pz):
                return 32768.0 + (pz - float(loc[2])) * 128.0 / (sz if sz else 1.0)

            # resample path in vertex space with per-sample target height
            spacing_cm = float(point_spacing) if point_spacing else max(1.0, width / 2.0)
            spacing_v = max(0.5, spacing_cm / (sx if sx else 1.0))
            samples = []  # (vx, vy, hz)
            for a in range(len(pts) - 1):
                ax, ay = _to_vertex(pts[a][0], pts[a][1])
                bx, by = _to_vertex(pts[a + 1][0], pts[a + 1][1])
                az = _to_u16z(pts[a][2] if len(pts[a]) > 2 else 0.0) + float(height_offset) * 128.0 / (sz if sz else 1.0)
                bz = _to_u16z(pts[a + 1][2] if len(pts[a + 1]) > 2 else 0.0) + float(height_offset) * 128.0 / (sz if sz else 1.0)
                seglen = math.hypot(bx - ax, by - ay)
                steps = max(1, int(seglen / spacing_v))
                for s in range(steps + 1):
                    t = s / float(steps)
                    samples.append((ax + (bx - ax) * t, ay + (by - ay) * t, az + (bz - az) * t))
            for _sm in range(max(0, int(height_smoothing))):
                sm = list(samples)
                for i in range(len(samples)):
                    lo = max(0, i - 1); hi = min(len(samples) - 1, i + 1)
                    hz = (sm[lo][2] + sm[i][2] + sm[hi][2]) / 3.0
                    samples[i] = (samples[i][0], samples[i][1], hz)

            core_v = max(0.5, (width / 2.0) / (sx if sx else 1.0))
            side_v = max(0.0, side_falloff / (sx if sx else 1.0))
            reach = core_v + side_v
            new = list(hf)
            affected = 0
            paint_w = [0] * (w * h) if layer_name else None
            # for each sample, stamp a disc of influence
            for (svx, svy, shz) in samples:
                gi0 = _clampi(math.floor(svx - reach), mnx, mnx + w - 1)
                gi1 = _clampi(math.ceil(svx + reach), mnx, mnx + w - 1)
                gj0 = _clampi(math.floor(svy - reach), mny, mny + h - 1)
                gj1 = _clampi(math.ceil(svy + reach), mny, mny + h - 1)
                for gy in range(gj0, gj1 + 1):
                    for gx in range(gi0, gi1 + 1):
                        d = math.hypot(gx - svx, gy - svy)
                        if d > reach:
                            continue
                        if d <= core_v:
                            wgt = 1.0
                        elif side_v > 0:
                            u = (d - core_v) / side_v
                            wgt = 1.0 - (u * u * (3.0 - 2.0 * u))
                        else:
                            wgt = 0.0
                        if wgt <= 0.0:
                            continue
                        k = (gy - mny) * w + (gx - mnx)
                        target = shz
                        delta = target - new[k]
                        if delta > 0 and not raise_terrain:
                            continue
                        if delta < 0 and not lower_terrain:
                            continue
                        cand = new[k] + delta * wgt
                        # keep the strongest stamp (closest to core) rather than averaging away
                        new[k] = cand
                        if paint_w is not None and wgt > 0.0:
                            pw = int(round(wgt * 255))
                            if pw > paint_w[k]:
                                paint_w[k] = pw
            for k in range(w * h):
                if abs(new[k] - hf[k]) > 0.5:
                    affected += 1
            if not apply:
                return json.dumps({"status": "success", "actor_name": actor_name, "dry_run": True,
                                   "affected_cells": affected, "samples": len(samples)}, indent=2)
            vals = [max(0, min(65535, int(round(v)))) for v in new]
            res = _write_field(actor_name, reg["x"], reg["y"], w, h, vals)
            out = {"height": res}
            if layer_name and paint_w is not None:
                p = {"actor_name": actor_name, "layer_name": layer_name,
                     "x": int(reg["x"]), "y": int(reg["y"]), "w": w, "h": h,
                     "weights_b64": _u8_to_b64(paint_w), "package_path": package_path, "layer_info_path": ""}
                out["paint"] = _exec(_PAINT_BODY, p)
            out["status"] = "success"; out["affected_cells"] = affected; out["samples"] = len(samples)
            return json.dumps(out, indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ================================================================== #
    # 7) clear_landscape_splines                                         #
    # ================================================================== #
    @mcp.tool()
    def clear_landscape_splines(ctx, actor_name: str) -> str:
        """Remove any PERSISTENT landscape spline control-points/segments on the landscape's
        LandscapeSplinesComponent, if present. NON-LEDGERED honest maintenance: removed persistent
        splines cannot be faithfully restored, so this ledgers NOTHING and never fabricates a fake
        inverse. Our own add_landscape_spline is height-based and creates NO persistent splines, so
        this normally reports a clean no-op (0 control points). Read-only in practice.

        Reports: whether a splines component exists, and its control_point / segment counts if the
        engine exposes them to Python (else null = not accessible via reflection)."""
        try:
            return json.dumps(_exec(_CLEAR_SPLINES_BODY, {"actor_name": actor_name}), indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ================================================================== #
    # 8) add_river                                                       #
    # ================================================================== #
    @mcp.tool()
    def add_river(ctx, actor_name: str, start=None, from_peak: bool = True, depth: float = 500.0,
                  width_scale: float = 2.0, valley_width: float = 0.0, tributaries: bool = False,
                  to=None, climb_penalty: float = 0.0, channel_depth: float = None, carve: bool = True,
                  water: bool = False, water_depth: float = 200.0, layer_name: str = None,
                  n_tributaries: int = 3, max_steps: int = None, seed: int = 1337,
                  package_path: str = "/Game/MCP_Scratch") -> str:
        """Carve a river along STEEPEST DESCENT, host-side. From `start` (WORLD [x,y] or [x,y,z]) or
        the highest vertex if from_peak, walk downhill cell-by-cell (steepest descent, with
        climb_penalty allowing small uphill steps to escape pits), then carve a channel.

        start:          WORLD coords [x,y] or [x,y,z]; if omitted and from_peak, the peak vertex.
        from_peak:      start at the highest vertex when start is None (default True).
        depth:          channel incision depth in world cm (default 500). channel_depth overrides.
        width_scale:    channel half-width in vertices (default 2).
        valley_width:   extra shallow valley half-width in vertices (default 0 = none).
        tributaries:    also carve n_tributaries feeder streams from high cells (default False).
        to:             optional WORLD target [x,y] to bias the walk toward (steepest descent still).
        climb_penalty:  >0 lets the walk step up to `climb_penalty` (uint16 units) to leave a basin.
        carve:          if False, dry-run (report the path, write nothing/no ledger).
        water/water_depth/layer_name: optional bank/water layer painted along the channel.
        max_steps:      safety cap on path length (default 4*(size_x+size_y)).

        Ledgered write (op 'landscape_set_height_region', EXISTING branch) when carve; plus optional
        op 'landscape_paint_weight_region' if layer_name. undo restores prior (and re-paints)."""
        try:
            rd = _read_field(actor_name)
            if rd.get("status") != "success":
                return json.dumps(rd, indent=2)
            info = rd["info"]; reg = rd["region"]
            w = int(reg["w"]); h = int(reg["h"])
            mnx = int(reg["x"]); mny = int(reg["y"])
            sz = _scale_z(info); sx, sy = _scale_xy(info)
            loc = rd.get("actor_loc", [0.0, 0.0, 0.0])
            hf = [float(v) for v in _b64_to_u16(reg["heights_b64"])]

            def _to_ij(px, py):
                vx = (px - float(loc[0])) / (sx if sx else 1.0)
                vy = (py - float(loc[1])) / (sy if sy else 1.0)
                return _clampi(round(vx) - mnx, 0, w - 1), _clampi(round(vy) - mny, 0, h - 1)

            s = _as_list(start)
            if s and len(s) >= 2:
                si, sj = _to_ij(float(s[0]), float(s[1]))
            elif from_peak:
                pk = max(range(w * h), key=lambda k: hf[k])
                si = pk % w; sj = pk // w
            else:
                si = w // 2; sj = h // 2
            tgt = _as_list(to)
            ti = tj = None
            if tgt and len(tgt) >= 2:
                ti, tj = _to_ij(float(tgt[0]), float(tgt[1]))

            climb = float(climb_penalty)
            cap = int(max_steps) if max_steps else 4 * (w + h)

            def _walk(pi, pj):
                path = [(pi, pj)]
                seen = set([(pi, pj)])
                for _step in range(cap):
                    ci, cj = path[-1]
                    ck = cj * w + ci
                    best = None; best_h = hf[ck]
                    best_score = 1e18
                    for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                        ii = ci + di; jj = cj + dj
                        if ii < 0 or ii >= w or jj < 0 or jj >= h:
                            continue
                        if (ii, jj) in seen:
                            continue
                        nk = jj * w + ii
                        dh = hf[nk] - hf[ck]
                        # steepest descent; allow small climb up to `climb`
                        if dh <= 0 or dh <= climb:
                            score = dh
                            if ti is not None:
                                score = score + 0.001 * (abs(ii - ti) + abs(jj - tj))
                            if score < best_score:
                                best_score = score; best = (ii, jj); best_h = hf[nk]
                    if best is None:
                        break
                    path.append(best); seen.add(best)
                    if ti is not None and best[0] == ti and best[1] == tj:
                        break
                    # reached the boundary -> stop
                    if best[0] in (0, w - 1) or best[1] in (0, h - 1):
                        break
                return path

            main_path = _walk(si, sj)
            paths = [main_path]
            if tributaries and n_tributaries > 0:
                rng = random.Random(int(seed) & 0xFFFFFFFF)
                on_main = set(main_path)
                # pick highest cells not on the main path as tributary sources
                cand = sorted(range(w * h), key=lambda k: hf[k], reverse=True)
                picked = 0
                for k in cand:
                    if picked >= int(n_tributaries):
                        break
                    ci = k % w; cj = k // w
                    if (ci, cj) in on_main:
                        continue
                    tp = _walk(ci, cj)
                    if len(tp) >= 3:
                        paths.append(tp)
                        for c in tp:
                            on_main.add(c)
                        picked += 1

            inc = (float(channel_depth) if channel_depth is not None else float(depth)) * 128.0 / (sz if sz else 1.0)
            half = max(0.5, float(width_scale))
            vhalf = max(0.0, float(valley_width))
            reach = half + vhalf
            new = list(hf)
            paint_w = [0] * (w * h) if (layer_name and (water or True)) else None
            carved = 0
            for path in paths:
                for (ci, cj) in path:
                    gi0 = _clampi(math.floor(ci - reach), 0, w - 1)
                    gi1 = _clampi(math.ceil(ci + reach), 0, w - 1)
                    gj0 = _clampi(math.floor(cj - reach), 0, h - 1)
                    gj1 = _clampi(math.ceil(cj + reach), 0, h - 1)
                    ch = hf[cj * w + ci]
                    for jj in range(gj0, gj1 + 1):
                        for ii in range(gi0, gi1 + 1):
                            d = math.hypot(ii - ci, jj - cj)
                            if d > reach:
                                continue
                            k = jj * w + ii
                            if d <= half:
                                cut = inc
                            elif vhalf > 0:
                                u = (d - half) / vhalf
                                cut = inc * (1.0 - (u * u * (3.0 - 2.0 * u)))
                            else:
                                cut = 0.0
                            if cut <= 0.0:
                                continue
                            # carve down to (channel-cell height minus incision), never raise
                            floor_h = ch - cut
                            if new[k] > floor_h:
                                new[k] = new[k] + (floor_h - new[k]) * (1.0 if d <= half else 0.6)
                            if paint_w is not None and d <= half:
                                if 255 > paint_w[k]:
                                    paint_w[k] = 255
            for k in range(w * h):
                if abs(new[k] - hf[k]) > 0.5:
                    carved += 1
            path_len = sum(len(p) for p in paths)
            if not carve:
                return json.dumps({"status": "success", "actor_name": actor_name, "dry_run": True,
                                   "path_cells": path_len, "start_ij": [si, sj], "affected_cells": carved,
                                   "paths": len(paths)}, indent=2)
            vals = [max(0, min(65535, int(round(v)))) for v in new]
            res = _write_field(actor_name, reg["x"], reg["y"], w, h, vals)
            out = {"height": res}
            if layer_name and paint_w is not None:
                p = {"actor_name": actor_name, "layer_name": layer_name,
                     "x": int(reg["x"]), "y": int(reg["y"]), "w": w, "h": h,
                     "weights_b64": _u8_to_b64(paint_w), "package_path": package_path, "layer_info_path": ""}
                out["paint"] = _exec(_PAINT_BODY, p)
            out["status"] = "success"; out["path_cells"] = path_len; out["affected_cells"] = carved
            out["start_ij"] = [si, sj]; out["paths"] = len(paths)
            return json.dumps(out, indent=2)
        except Exception as e:
            return "Error: %s" % e
