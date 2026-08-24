"""UserTools :: Landscape / terrain authoring (CORE)  (spec: docs/spec/editor.md)

Landscape has NO reflected Python authoring surface in UE 5.8 (ALandscapeProxy::Import and the
heightmap/weightmap edit-data path are C++-only), so these tools COMPOSE the C++ edit-data bridge
in MCPReflection_Landscape.cpp (UMCPReflectionLibrary.*_json statics, auto-bound to Python) and do
the procedural math HOST-SIDE (like scatter_foliage_in_box computes positions in Python). This is
the CORE round: create / sculpt / paint / info / validate. The ADVANCED procedural suite
(generate_terrain, erosion, hydrology, rivers, splines, apply_terrain_process) is a documented
FOLLOW-UP that builds on this SAME bridge -- deliberately NOT drafted here (scope control).

Mirrors editor_level.py / foliage_write.py conventions VERBATIM: base64-injected PARAMS, the
Output-Log auto-capture wrapper, the @@UMCP@@ marker, the session-aware per-agent undo ledger.

Bridge handlers used (all #if WITH_EDITOR, JSON returns):
  - create_landscape_json(name, transform_json, config_json, height_b64)  -> spawn + Import
  - landscape_get_info_json(actor_name)                                   -> resolution/config/scale/layers
  - landscape_get_height_region_json(actor_name, x, y, w, h)              -> base64 uint16 (row-major W*H)
  - landscape_set_height_region_json(actor_name, x, y, w, h, b64, prior)  -> write heights
  - landscape_paint_weight_region_json(actor_name, layer, x,y,w,h, b64, layer_info_path) -> paint (returns prev)
  - validate_landscape_json(actor_name)                                   -> health check

WIRE FORMAT: heights cross as base64 of little-endian uint16 (struct '<..H', row-major W*H); weights
as base64 uint8. Height encoding: uint16 H; LocalZ=(H-32768)/128; WorldZ=LocalZ*ActorScaleZ; flat=32768.

Reversibility (per-session ledger; the ONE unified undo lives in editor_level.py):
  - create_landscape       -> ledger op "spawn_actor" {actor_name}  (REUSES the existing spawn_actor
                              undo branch -> destroy_actor; NO new editor_level.undo branch needed).
  - sculpt_landscape       -> ledger op "landscape_set_height_region" {actor_name,x,y,w,h,prev_b64};
                              inverse re-writes prev_b64 via landscape_set_height_region_json (NEW branch).
  - paint_landscape_layer  -> ledger op "landscape_paint_weight_region"
                              {actor_name,layer_name,x,y,w,h,prev_b64,layer_info_path}; inverse re-paints
                              prev_b64 (NEW branch). If it created the LayerInfo asset, that is ALSO ledgered
                              as op "create_asset" FIRST (LIFO: undo re-paints, then deletes the asset).
  (See the module docstring TAIL for the exact editor_level.undo branches the coordinator must fold in.)

WARNING: use a NON-World-Partition SCRATCH level for create_landscape (the simple registration path).
Landscape edit-data writes mutate textures directly and are NOT reliably UE-transaction-undoable, hence
the capture-prior ledger (not the transaction buffer) is the real inverse.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# Valid landscape section config values (FLandscapeConfig::SubsectionSizeQuadsValues / NumSectionValues).
_SUBSECTION_SIZES = [7, 15, 31, 63, 127, 255]
_NUM_SECTIONS = [1, 2]
_MAX_COMPONENTS = 32

# --- Output Log auto-capture (verbatim from editor_level.py) ------------------
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


def _choose_component_size(width, height, quads_per_section, sections_per_component):
    """Host-side port of FLandscapeImportHelper::ChooseBestComponentSizeForImport. Given a requested
    vertex resolution width x height, return (quads_per_section, sections_per_component, ccx, ccy) that
    exactly tiles (or else grows the section size to encompass). Mirrors the engine algorithm."""
    # Exact match: largest section size down, prefer fewest sections.
    for ss in reversed(_SUBSECTION_SIZES):
        for ns in _NUM_SECTIONS:
            span = ss * ns
            fits_x = (((width - 1) % span) == 0) and (((width - 1) // span) <= _MAX_COMPONENTS)
            fits_y = (((height - 1) % span) == 0) and (((height - 1) // span) <= _MAX_COMPONENTS)
            if fits_x and fits_y:
                return ss, ns, (width - 1) // span, (height - 1) // span
    # No exact match: keep requested/first section count, grow section size until it fits.
    ns = sections_per_component if sections_per_component in _NUM_SECTIONS else _NUM_SECTIONS[0]
    for ss in _SUBSECTION_SIZES:
        ccx = max(1, -(-(width - 1) // (ss * ns)))   # ceil-div
        ccy = max(1, -(-(height - 1) // (ss * ns)))
        if ccx <= _MAX_COMPONENTS and ccy <= _MAX_COMPONENTS:
            return ss, ns, ccx, ccy
    # Fallback: smallest valid single-component landscape.
    return _SUBSECTION_SIZES[0], 1, 1, 1


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

    # Shared Unreal-side helpers (prepended to bodies). Verbatim ledger from editor_level.py; plus
    # landscape helpers (base64<->uint16/uint8 packing, bridge wrappers). NO ''' / NO backslashes inside.
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
'''

    # ------------------------------------------------------------------ #
    # create_landscape                                                    #
    # ------------------------------------------------------------------ #
    _CREATE_BODY = _COERCE_HELPERS + r'''
name = PARAMS["name"]
transform_json = PARAMS["transform_json"]
config_json = PARAMS["config_json"]
height_b64 = PARAMS.get("height_b64", "")
res = _lib().create_landscape_json(name, transform_json, config_json, height_b64)
out = json.loads(res)
if out.get("status") == "success":
    _ledger().append({"op": "spawn_actor", "actor_name": out["actor_name"]})
    out["undo_op"] = "spawn_actor"
    out["ledger_depth"] = len(_ledger())
print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def create_landscape(ctx, name: str, location: list = None, rotation: list = None,
                         scale: list = None, component_count_x: int = 1, component_count_y: int = 1,
                         quads_per_section: int = 63, sections_per_component: int = 1,
                         requested_size_x: int = None, requested_size_y: int = None,
                         heights: list = None, heightfield: str = "flat",
                         amplitude_cm: float = 5000.0) -> str:
        """Create an ALandscape and Import a flat or simple procedural heightfield (CORE terrain create).

        name:            actor label.
        location/rotation/scale: world transform (defaults [0,0,0]/[0,0,0]/[100,100,100]).
        component_count_x/y:    landscape size in components (1..32 each). Default 1x1.
        quads_per_section:      one of 7,15,31,63,127,255 (default 63).
        sections_per_component: 1 or 2 (default 1). Vertices = ccx*spc*qps + 1 (per axis).
        requested_size_x/y:     OPTIONAL desired vertex resolution; if given, the section config is snapped
                                host-side (port of ChooseBestComponentSizeForImport) and the count/section
                                args above are overridden.
        heights:                OPTIONAL explicit flat row-major list of uint16 (len must == SizeX*SizeY).
        heightfield:            procedural preset when `heights` is None: 'flat' (default, all 32768),
                                'hill' (radial cosine bump of amplitude_cm), 'ramp' (linear X ramp).
        amplitude_cm:           peak height (cm) for the 'hill'/'ramp' presets.

        Ledgered write (op 'spawn_actor', REUSES the existing undo branch): `undo` destroys the landscape
        actor. Use a NON-World-Partition scratch level."""
        try:
            qps = int(quads_per_section)
            spc = int(sections_per_component)
            ccx = int(component_count_x)
            ccy = int(component_count_y)
            if requested_size_x and requested_size_y:
                qps, spc, ccx, ccy = _choose_component_size(int(requested_size_x), int(requested_size_y), qps, spc)
            if qps not in _SUBSECTION_SIZES:
                return "Error: quads_per_section must be one of %s" % _SUBSECTION_SIZES
            if spc not in _NUM_SECTIONS:
                return "Error: sections_per_component must be 1 or 2"
            if not (1 <= ccx <= _MAX_COMPONENTS and 1 <= ccy <= _MAX_COMPONENTS):
                return "Error: component counts must be in 1..%d" % _MAX_COMPONENTS
            qpc = spc * qps
            size_x = ccx * qpc + 1
            size_y = ccy * qpc + 1
            nverts = size_x * size_y

            # Build the height array host-side (or leave empty for flat -> C++ fills 32768).
            height_b64 = ""
            if heights is not None:
                if len(heights) != nverts:
                    return "Error: heights has %d samples but SizeX*SizeY=%d (%dx%d)" % (len(heights), nverts, size_x, size_y)
                vals = [max(0, min(65535, int(v))) for v in heights]
                height_b64 = _pack_u16_host(vals)
            elif heightfield and heightfield != "flat":
                vals = _procedural_field(heightfield, size_x, size_y, float(amplitude_cm), scale)
                if vals is None:
                    return "Error: unknown heightfield preset '%s' (use flat|hill|ramp)" % heightfield
                height_b64 = _pack_u16_host(vals)

            transform_json = json.dumps({
                "location": [float(v) for v in (location or [0.0, 0.0, 0.0])],
                "rotation": [float(v) for v in (rotation or [0.0, 0.0, 0.0])],
                "scale": [float(v) for v in (scale or [100.0, 100.0, 100.0])],
            })
            config_json = json.dumps({
                "quads_per_section": qps, "sections_per_component": spc,
                "component_count_x": ccx, "component_count_y": ccy,
            })
            params = {"name": name, "transform_json": transform_json,
                      "config_json": config_json, "height_b64": height_b64}
            result = _exec(_CREATE_BODY, params)
            result["computed"] = {"size_x": size_x, "size_y": size_y, "num_verts": nverts,
                                  "quads_per_section": qps, "sections_per_component": spc,
                                  "component_count_x": ccx, "component_count_y": ccy}
            return json.dumps(result, indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ------------------------------------------------------------------ #
    # sculpt_landscape (raise/lower/flatten/smooth a region)              #
    # ------------------------------------------------------------------ #
    _SCULPT_BODY = _COERCE_HELPERS + r'''
actor = PARAMS["actor_name"]
mode = PARAMS["mode"]
x = int(PARAMS["x"]); y = int(PARAMS["y"]); w = int(PARAMS["w"]); h = int(PARAMS["h"])
amount_cm = float(PARAMS.get("amount_cm", 100.0))
target_cm = PARAMS.get("target_cm", None)
strength = float(PARAMS.get("strength", 1.0))
falloff = PARAMS.get("falloff", "none")

info = json.loads(_lib().landscape_get_info_json(actor))
if "error" in info:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": info["error"]}))
else:
    scale_z = 100.0
    ds = info.get("draw_scale")
    if isinstance(ds, list) and len(ds) >= 3 and ds[2]:
        scale_z = float(ds[2])
    # Read prior region (also tells us the actual clamped x,y,w,h).
    rd = json.loads(_lib().landscape_get_height_region_json(actor, x, y, w, h))
    if "error" in rd:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": rd["error"]}))
    else:
        ax, ay, aw, ah = rd["x"], rd["y"], rd["w"], rd["h"]
        prev_b64 = rd["heights_b64"]
        cur = _b64_to_u16(prev_b64)
        # world-cm -> uint16 delta: dH = cm * 128 / scale_z
        amt_h = amount_cm * 128.0 / (scale_z if scale_z else 1.0)
        def _fall(i, j):
            if falloff != "linear":
                return 1.0
            cx = (aw - 1) / 2.0; cy = (ah - 1) / 2.0
            rr = math.sqrt(cx * cx + cy * cy) or 1.0
            d = math.sqrt((i - cx) ** 2 + (j - cy) ** 2)
            return max(0.0, 1.0 - d / rr)
        new = list(cur)
        if mode in ("raise", "lower"):
            sgn = 1.0 if mode == "raise" else -1.0
            for j in range(ah):
                for i in range(aw):
                    k = j * aw + i
                    new[k] = int(round(cur[k] + sgn * amt_h * _fall(i, j)))
        elif mode == "flatten":
            if target_cm is None:
                tgt = sum(cur) / float(len(cur)) if cur else 32768.0
            else:
                tgt = float(target_cm) * 128.0 / (scale_z if scale_z else 1.0) + 32768.0
            for j in range(ah):
                for i in range(aw):
                    k = j * aw + i
                    f = _fall(i, j) * strength
                    new[k] = int(round(cur[k] + (tgt - cur[k]) * f))
        elif mode == "smooth":
            for j in range(ah):
                for i in range(aw):
                    acc = 0; cnt = 0
                    for dj in (-1, 0, 1):
                        for di in (-1, 0, 1):
                            ii = i + di; jj = j + dj
                            if 0 <= ii < aw and 0 <= jj < ah:
                                acc += cur[jj * aw + ii]; cnt += 1
                    avg = acc / float(cnt) if cnt else cur[j * aw + i]
                    k = j * aw + i
                    f = _fall(i, j) * strength
                    new[k] = int(round(cur[k] + (avg - cur[k]) * f))
        else:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "unknown mode '%s' (raise|lower|flatten|smooth)" % mode}))
            new = None
        if new is not None:
            new = [max(0, min(65535, v)) for v in new]
            wr = json.loads(_lib().landscape_set_height_region_json(actor, ax, ay, aw, ah, _u16_to_b64(new), False))
            if "error" in wr:
                print("@@UMCP@@" + json.dumps({"status": "error", "message": wr["error"]}))
            else:
                with unreal.ScopedEditorTransaction("MCP sculpt_landscape"):
                    pass
                _ledger().append({"op": "landscape_set_height_region", "actor_name": actor,
                                  "x": ax, "y": ay, "w": aw, "h": ah, "prev_b64": prev_b64})
                hmin = min(new); hmax = max(new)
                print("@@UMCP@@" + json.dumps({"status": "success", "actor_name": actor, "mode": mode,
                    "x": ax, "y": ay, "w": aw, "h": ah, "cells": len(new),
                    "height_min_u16": hmin, "height_max_u16": hmax,
                    "undo_op": "landscape_set_height_region", "ledger_depth": len(_ledger())}))
'''

    @mcp.tool()
    def sculpt_landscape(ctx, actor_name: str, mode: str, x: int, y: int, w: int, h: int,
                         amount_cm: float = 100.0, target_cm: float = None,
                         strength: float = 1.0, falloff: str = "none") -> str:
        """Sculpt a rectangular height region of a landscape (raise/lower/flatten/smooth). The delta is
        computed HOST-SIDE (Python) from the read-back region; the prior region is captured for undo.

        actor_name: landscape actor label or unique name.
        mode:       'raise' | 'lower' | 'flatten' | 'smooth'.
        x,y,w,h:    region in VERTEX coordinates (clamped to the landscape extent; the actual clamped
                    region is reported and used).
        amount_cm:  raise/lower magnitude in world cm (converted to uint16 via 128/scale_z).
        target_cm:  flatten target height in world cm; if None, flatten uses the region's mean height.
        strength:   0..1 blend for flatten/smooth (default 1.0).
        falloff:    'none' (uniform, default) or 'linear' (radial falloff from the region center).

        Ledgered write (op 'landscape_set_height_region'): `undo` re-writes the captured prior region."""
        try:
            if mode not in ("raise", "lower", "flatten", "smooth"):
                return "Error: mode must be raise|lower|flatten|smooth"
            params = {"actor_name": actor_name, "mode": mode, "x": int(x), "y": int(y),
                      "w": int(w), "h": int(h), "amount_cm": float(amount_cm),
                      "target_cm": (None if target_cm is None else float(target_cm)),
                      "strength": float(strength), "falloff": falloff}
            return json.dumps(_exec(_SCULPT_BODY, params), indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ------------------------------------------------------------------ #
    # paint_landscape_layer                                               #
    # ------------------------------------------------------------------ #
    _PAINT_BODY = _COERCE_HELPERS + r'''
actor = PARAMS["actor_name"]
layer = PARAMS["layer_name"]
x = int(PARAMS["x"]); y = int(PARAMS["y"]); w = int(PARAMS["w"]); h = int(PARAMS["h"])
weight = int(PARAMS.get("weight", 255))
pkg = PARAMS.get("package_path", "/Game/MCP_Scratch")

info = json.loads(_lib().landscape_get_info_json(actor))
if "error" in info:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": info["error"]}))
else:
    existing = None
    for L in info.get("layers", []):
        if L.get("name") == layer:
            existing = L
            break
    layer_info_path = ""
    created_asset = None
    if existing and existing.get("has_layer_info"):
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
        weights = [weight] * (w * h)
        wb64 = _u8_to_b64(weights)
        res = json.loads(_lib().landscape_paint_weight_region_json(actor, layer, x, y, w, h, wb64, layer_info_path))
        if "error" in res:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": res["error"], "created_asset": created_asset}))
        else:
            with unreal.ScopedEditorTransaction("MCP paint_landscape_layer"):
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

    @mcp.tool()
    def paint_landscape_layer(ctx, actor_name: str, layer_name: str, x: int, y: int, w: int, h: int,
                              weight: int = 255, package_path: str = "/Game/MCP_Scratch") -> str:
        """Paint a uniform weight region for a landscape target layer. If the layer's LayerInfo asset is
        absent it is created host-side (ULandscapeLayerInfoObject, factory None) under package_path and
        registered as a target layer by the bridge. The weight array is computed HOST-SIDE.

        actor_name:   landscape actor label or unique name.
        layer_name:   target layer name (created if absent).
        x,y,w,h:      region in VERTEX coordinates (clamped to extent; must not require clamping -- the
                      bridge rejects a clamped region so the uint8 stride stays consistent).
        weight:       uniform paint weight 0..255 (default 255 = fully this layer).
        package_path: content folder for a created LayerInfo asset (default /Game/MCP_Scratch).

        Ledgered write (op 'landscape_paint_weight_region'): the bridge returns the prior weights, which
        `undo` re-paints. If a LayerInfo asset was created, it is ALSO ledgered (op 'create_asset', deleted
        on undo after the re-paint). Painting is the highest-risk write -- weightmap allocation for a fresh
        target layer; verify live."""
        try:
            params = {"actor_name": actor_name, "layer_name": layer_name, "x": int(x), "y": int(y),
                      "w": int(w), "h": int(h), "weight": int(weight), "package_path": package_path}
            return json.dumps(_exec(_PAINT_BODY, params), indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ------------------------------------------------------------------ #
    # get_landscape_info  (READ)                                          #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _COERCE_HELPERS + r'''
actor = PARAMS["actor_name"]
print("@@UMCP@@" + _lib().landscape_get_info_json(actor))
'''

    @mcp.tool()
    def get_landscape_info(ctx, actor_name: str) -> str:
        """READ a landscape's resolution, section/component config, draw scale, height range, and target
        layers. Read-only (not ledgered).

        actor_name: landscape actor label or unique name."""
        try:
            return json.dumps(_exec(_INFO_BODY, {"actor_name": actor_name}), indent=2)
        except Exception as e:
            return "Error: %s" % e

    # ------------------------------------------------------------------ #
    # validate_landscape  (READ)                                          #
    # ------------------------------------------------------------------ #
    _VALIDATE_BODY = _COERCE_HELPERS + r'''
actor = PARAMS["actor_name"]
print("@@UMCP@@" + _lib().validate_landscape_json(actor))
'''

    @mcp.tool()
    def validate_landscape(ctx, actor_name: str) -> str:
        """READ / health-check a landscape: LandscapeInfo present, valid extent, section-config sanity,
        target layers without a LayerInfo asset (cannot be painted), GUID validity. 'valid' is False on
        any hard issue. Read-only (not ledgered).

        actor_name: landscape actor label or unique name."""
        try:
            return json.dumps(_exec(_VALIDATE_BODY, {"actor_name": actor_name}), indent=2)
        except Exception as e:
            return "Error: %s" % e


# ---- host-side (outer-process) helpers used by create_landscape ----------------------------------
def _pack_u16_host(vals):
    import struct as _s
    raw = _s.pack("<%dH" % len(vals), *[max(0, min(65535, int(v))) for v in vals])
    return base64.b64encode(raw).decode("ascii")


def _procedural_field(preset, size_x, size_y, amplitude_cm, scale):
    """Compute a simple flat-list uint16 heightfield host-side. scale[2] (Z) maps cm -> uint16 (128/scale_z)."""
    import math as _m
    scale_z = 100.0
    if isinstance(scale, (list, tuple)) and len(scale) >= 3 and scale[2]:
        scale_z = float(scale[2])
    amp_h = amplitude_cm * 128.0 / (scale_z if scale_z else 1.0)
    mid = 32768.0
    vals = []
    if preset == "hill":
        cx = (size_x - 1) / 2.0
        cy = (size_y - 1) / 2.0
        rr = _m.sqrt(cx * cx + cy * cy) or 1.0
        for j in range(size_y):
            for i in range(size_x):
                d = _m.sqrt((i - cx) ** 2 + (j - cy) ** 2) / rr
                bump = 0.5 * (1.0 + _m.cos(_m.pi * min(1.0, d)))  # 1 at center -> 0 at edge
                vals.append(int(round(mid + amp_h * bump)))
        return [max(0, min(65535, v)) for v in vals]
    if preset == "ramp":
        for j in range(size_y):
            for i in range(size_x):
                t = i / float(size_x - 1) if size_x > 1 else 0.0
                vals.append(int(round(mid + amp_h * t)))
        return [max(0, min(65535, v)) for v in vals]
    return None
