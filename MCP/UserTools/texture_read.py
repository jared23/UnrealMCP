"""UserTools :: Texture / Read  (spec: docs/spec/editor.md)

Clean-room, READ-ONLY texture introspection over Unreal's public Python API
(AssetRegistry + reflection, UE 5.8). Follows the editor_level.py / editor_discovery.py
gold-standard conventions verbatim: snippets print @@UMCP@@<json> on one line; params are
injected as base64 JSON via _exec; _query wraps every snippet with Output-Log auto-capture
and surfaces new Warning/Error lines as result["_log_warnings"]. Session id is injected as
PARAMS["_session"] for parity, though this module is PURE READ (no ledger, no writes, no
factories/creation-params, no modals).

Implemented:
  - list_textures     (read-only) enumerate Texture2D / TextureCube / TextureRenderTarget2D
                       via AssetRegistry (class_paths API); counts + paths, path/kind filterable.
  - get_texture_info  (read-only) full introspection of a Texture2D asset: size, compression,
                       lod_group, srgb, mip_gen_settings, address_x/y, never_stream, streaming
                       method, resident GPU-memory estimate, source disk/memory size, + more.
  - get_texture_stats (read-only) roll-up over Texture2D under a path: count + total imported
                       megapixels + source-memory bytes, grouped by format(compression) or lod_group.

Honest limitations learned live against TestMCPSetup (UE 5.8.1):
  - Texture2D.ImportedSize (FIntPoint), Texture2D.Source (FTextureSource) and SourceFilePath
    are PROTECTED UPROPERTYs -> NOT readable via Python reflection. So the true SOURCE
    width/height and the on-disk PIXEL FORMAT (PF_DXT1/PF_B8G8R8A8/...) and exact MIP COUNT are
    not directly reachable from stock Python; there is no MCPReflectionLibrary texture handler
    for them either. We report the BUILT/current size (blueprint_get_size_x/y), the user-facing
    compression_settings enum, and the source disk/memory byte sizes, and honestly null the
    unreachable fields with a note.
  - blueprint_get_memory_size() returns the CURRENTLY-RESIDENT GPU memory (streamed textures may
    have only low mips resident in-editor), so it is an editor-session estimate, not the full
    packaged footprint. blueprint_get_texture_source_disk_and_memory_size() -> (disk, source_mem)
    is stable source data and is reported alongside.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) -----------
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec,
# so snippet bodies must contain NO ''' and NO stray backslashes. All data is passed as base64.


def register_tools(mcp, utils):
    send_command = utils["send_command"]
    session = (utils.get("session") if isinstance(utils, dict) else None) or ("s" + str(os.getpid()))

    def _query(code):
        resp = send_command("execute_python", {"code": _wrap(code)})
        if not isinstance(resp, dict) or resp.get("status") != "success":
            raise RuntimeError(f"execute_python did not succeed: {resp}")
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
        raise RuntimeError(f"no {MARKER} payload in output:\n{out}")

    def _exec(body, params):
        params = dict(params or {})
        params.setdefault("_session", session)
        b64 = base64.b64encode(json.dumps(params).encode("utf-8")).decode("ascii")
        header = ('import base64 as _b64, json as _json\n'
                  'PARAMS = _json.loads(_b64.b64decode("%s").decode("utf-8"))\n' % b64)
        return _query(header + body)

    # Shared Unreal-side helper: turn an enum repr into its short member name.
    # str(enum) -> "<TextureCompressionSettings.TC_MASKS: 2>" -> "TC_MASKS".
    _ENUM_HELPER = r'''
import unreal, json, warnings
warnings.simplefilter("ignore")
def _try(fn, d=None):
    try: return fn()
    except Exception: return d
def _enum_short(v):
    if v is None:
        return None
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
KINDS = {"texture2d": "Texture2D", "texturecube": "TextureCube",
         "texturerendertarget2d": "TextureRenderTarget2D"}
'''

    # ------------------------------------------------------------------ #
    # list_textures — enumerate texture assets via AssetRegistry          #
    # ------------------------------------------------------------------ #
    _LIST_BODY = _ENUM_HELPER + r'''
path = PARAMS.get("path") or "/Game"
kind = (PARAMS.get("kind") or "Texture2D")
name_filter = (PARAMS.get("name_filter") or "").lower()
max_results = int(PARAMS.get("max_results") or 500)
ar = unreal.AssetRegistryHelpers.get_asset_registry()

if str(kind).lower() == "all":
    want = ["Texture2D", "TextureCube", "TextureRenderTarget2D"]
else:
    canon = KINDS.get(str(kind).lower())
    want = [canon] if canon else [kind]

by_kind = {}
all_paths = []
truncated = False
# Count every kind fully (so by_kind is complete); only the flat 'textures' path list is capped.
for cls in want:
    far = unreal.ARFilter(
        class_paths=[unreal.TopLevelAssetPath("/Script/Engine", cls)],
        recursive_paths=True, package_paths=[path])
    assets = ar.get_assets(far) or []
    kcount = 0
    for d in assets:
        nm = str(d.get_editor_property("asset_name"))
        if name_filter and name_filter not in nm.lower():
            continue
        kcount += 1
        if len(all_paths) >= max_results:
            truncated = True
            continue
        p = str(d.get_editor_property("package_name")) + "." + nm
        all_paths.append({"path": p, "kind": cls})
    by_kind[cls] = {"count": kcount}

out = {"status": "success", "path": path, "kind": kind,
       "name_filter": PARAMS.get("name_filter"),
       "total": sum(v["count"] for v in by_kind.values()),
       "by_kind": by_kind, "returned": len(all_paths),
       "truncated": truncated, "textures": all_paths}
print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def list_textures(ctx, path: str = "/Game", kind: str = "Texture2D",
                      name_filter: str = None, max_results: int = 500) -> str:
        """Enumerate texture assets via the AssetRegistry (no assets are loaded). Read-only.

        path:        content path to scan recursively (default '/Game'; use '/Engine' for
                     engine textures, or a deeper path to narrow).
        kind:        'Texture2D' (default), 'TextureCube', 'TextureRenderTarget2D', or 'all'
                     (enumerate all three; response 'by_kind' reports the per-class counts).
        name_filter: case-insensitive substring on the asset name.
        max_results: cap the returned path list (default 500); 'truncated' flags if hit.

        Returns per-kind counts plus a flat list of {path, kind}. Cheap: it reads asset
        descriptors from the registry rather than loading the texture objects."""
        params = {"path": path, "kind": kind, "name_filter": name_filter,
                  "max_results": max_results}
        try:
            return json.dumps(_exec(_LIST_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_texture_info — full introspection of a Texture2D asset           #
    # ------------------------------------------------------------------ #
    _INFO_BODY = _ENUM_HELPER + r'''
path = PARAMS["path"]
t = unreal.EditorAssetLibrary.load_asset(path)
if t is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % path}))
elif not isinstance(t, unreal.Texture2D):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a Texture2D (got %s): %s" % (t.get_class().get_name(), path),
        "hint": "get_texture_info targets Texture2D; use list_textures for other kinds"}))
else:
    info = {"status": "success", "path": t.get_path_name(),
            "name": t.get_name(), "class": t.get_class().get_name()}
    # Built/current dimensions (Texture2D.ImportedSize is PROTECTED -> unreadable; these come
    # from the built texture and equal the imported size unless downscaled by lod_bias/max size).
    # Prefer blueprint_get_built_texture_size (stable built platform-data top-mip size); fall
    # back to blueprint_get_size_x/y (which can briefly UNDER-report right after a cold load
    # while the texture streams its top mip in).
    bts = _try(lambda: t.blueprint_get_built_texture_size())
    bw = bh = None
    if bts is not None:
        bw = _try(lambda: int(bts.x)); bh = _try(lambda: int(bts.y))
        info["built_texture_size"] = [_try(lambda: bts.x), _try(lambda: bts.y), _try(lambda: bts.z)]
    info["width"] = bw if bw else _try(lambda: t.blueprint_get_size_x())
    info["height"] = bh if bh else _try(lambda: t.blueprint_get_size_y())
    info["resident_size_xy"] = [_try(lambda: t.blueprint_get_size_x()), _try(lambda: t.blueprint_get_size_y())]
    info["size_source"] = "blueprint_get_built_texture_size (built top-mip size; ImportedSize UPROPERTY is protected in Python; resident_size_xy is the currently-streamed size which may be smaller until fully streamed)"
    # Reflected UPROPERTY settings (all readable via get_editor_property).
    info["compression_settings"] = _enum_short(_try(lambda: t.get_editor_property("compression_settings")))
    info["lod_group"] = _enum_short(_try(lambda: t.get_editor_property("lod_group")))
    info["srgb"] = _try(lambda: t.get_editor_property("srgb"))
    info["mip_gen_settings"] = _enum_short(_try(lambda: t.get_editor_property("mip_gen_settings")))
    info["address_x"] = _enum_short(_try(lambda: t.get_editor_property("address_x")))
    info["address_y"] = _enum_short(_try(lambda: t.get_editor_property("address_y")))
    info["filter"] = _enum_short(_try(lambda: t.get_editor_property("filter")))
    info["never_stream"] = _try(lambda: t.get_editor_property("never_stream"))
    info["lod_bias"] = _try(lambda: t.get_editor_property("lod_bias"))
    info["max_texture_size"] = _try(lambda: t.get_editor_property("max_texture_size"))
    info["num_cinematic_mip_levels"] = _try(lambda: t.get_editor_property("num_cinematic_mip_levels"))
    info["mip_load_options"] = _enum_short(_try(lambda: t.get_editor_property("mip_load_options")))
    info["virtual_texture_streaming"] = _try(lambda: t.get_editor_property("virtual_texture_streaming"))
    info["flip_green_channel"] = _try(lambda: t.get_editor_property("flip_green_channel"))
    info["power_of_two_mode"] = _enum_short(_try(lambda: t.get_editor_property("power_of_two_mode")))
    info["compression_no_alpha"] = _try(lambda: t.get_editor_property("compression_no_alpha"))
    info["streaming_method"] = _enum_short(_try(lambda: t.get_texture_streaming_method()))
    # Memory / resource sizes.
    resident = _try(lambda: t.blueprint_get_memory_size())
    dm = _try(lambda: t.blueprint_get_texture_source_disk_and_memory_size())
    src_disk = src_mem = None
    if isinstance(dm, (tuple, list)) and len(dm) >= 2:
        src_disk, src_mem = int(dm[0]), int(dm[1])
    info["memory"] = {
        "resident_gpu_bytes": resident,
        "resident_gpu_note": "blueprint_get_memory_size() = CURRENTLY-RESIDENT GPU memory in this editor session (streamed textures may have only low mips resident) -> estimate, not packaged footprint",
        "source_disk_bytes": src_disk,
        "source_memory_bytes": src_mem,
        "source_size_note": "blueprint_get_texture_source_disk_and_memory_size() -> (compressed on-disk source bytes, uncompressed source bytes)",
    }
    info["source_id"] = _try(lambda: str(t.blueprint_get_texture_source_id_string()))
    # Default (fallback) state for fields that stock Python reflection cannot reach.
    # Texture2D.Source (FTextureSource) and ImportedSize are protected UPROPERTYs and PlatformData
    # is not exposed, so pixel_format / num_mips / true source dims are null WITHOUT the C++ helper.
    info["pixel_format"] = None
    info["pixel_format_value"] = None
    info["num_mips"] = None
    info["imported_width"] = None
    info["imported_height"] = None
    info["has_source"] = None
    info["source_width"] = None
    info["source_height"] = None
    info["source_num_mips"] = None
    info["source_format"] = None
    info["format_source"] = "python_reflection_only"
    info["unreachable_note"] = ("pixel_format (PF_*), num_mips and true source width/height are NOT "
        "reachable from stock UE 5.8 Python: Texture2D.Source (FTextureSource) and ImportedSize are "
        "protected UPROPERTYs and PlatformData is not exposed. compression_settings is the user-facing "
        "setting that (with srgb) drives the packaged pixel format. width/height above are the built "
        "texture size. When the native MCPReflectionLibrary.get_texture_info_json C++ handler is present, "
        "these fields are populated with REAL data and format_source becomes 'MCPReflectionLibrary'.")
    # ---- REAL pixel format / mips / source dims via native C++ handler (if compiled in) ----
    # MCPReflectionLibrary.get_texture_info_json reaches PlatformData + the protected FTextureSource.
    # Shape: {"texture","width","height","num_mips","pixel_format","pixel_format_value",
    #   "imported_width","imported_height","has_source","source_width","source_height",
    #   "source_num_mips","source_format"}.
    if hasattr(unreal.MCPReflectionLibrary, "get_texture_info_json"):
        tjs = _try(lambda: unreal.MCPReflectionLibrary.get_texture_info_json(t))
        td = _try(lambda: json.loads(tjs)) if tjs else None
        if isinstance(td, dict):
            info["pixel_format"] = td.get("pixel_format")
            info["pixel_format_value"] = td.get("pixel_format_value")
            info["num_mips"] = td.get("num_mips")
            info["imported_width"] = td.get("imported_width")
            info["imported_height"] = td.get("imported_height")
            info["has_source"] = td.get("has_source")
            info["source_width"] = td.get("source_width")
            info["source_height"] = td.get("source_height")
            info["source_num_mips"] = td.get("source_num_mips")
            info["source_format"] = td.get("source_format")
            info["format_source"] = "MCPReflectionLibrary"
            info["unreachable_note"] = ("REAL pixel_format (PF_*), num_mips, imported_* and source_* via "
                "native MCPReflectionLibrary.get_texture_info_json (reaches PlatformData + the protected "
                "FTextureSource / ImportedSize). width/height above remain the built top-mip size; "
                "imported_width/height are the authored import dims; source_* are the on-disk source.")
    print("@@UMCP@@" + json.dumps(info))
'''

    @mcp.tool()
    def get_texture_info(ctx, path: str) -> str:
        """Full read-only introspection of a Texture2D asset.

        path: the Texture2D asset path, e.g.
              '/Game/LevelPrototyping/Textures/T_GridChecker_A.T_GridChecker_A'.

        Returns: built width/height (+built_texture_size), compression_settings, lod_group,
        srgb, mip_gen_settings, address_x/y, filter, never_stream, lod_bias, max_texture_size,
        mip_load_options, virtual_texture_streaming, flip_green_channel, power_of_two_mode,
        streaming_method, and a 'memory' block (currently-resident GPU-memory estimate + source
        disk/memory byte sizes) plus the source id.

        When the native MCPReflectionLibrary.get_texture_info_json C++ handler is present, REAL
        pixel_format (PF_*), pixel_format_value, num_mips, imported_width/height and source_* (width,
        height, num_mips, format, has_source) are populated and format_source='MCPReflectionLibrary'.
        If that helper is absent, those fields are null and format_source='python_reflection_only'
        (Source/ImportedSize UPROPERTYs are protected, PlatformData is not exposed from stock Python).
        Errors cleanly if the asset is missing or is not a Texture2D."""
        try:
            return json.dumps(_exec(_INFO_BODY, {"path": path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # get_texture_stats — roll-up over Texture2D under a path             #
    # ------------------------------------------------------------------ #
    _STATS_BODY = _ENUM_HELPER + r'''
path = PARAMS.get("path") or "/Game"
group_by = (PARAMS.get("group_by") or "format").lower()
max_load = int(PARAMS.get("max_load") or 500)
ar = unreal.AssetRegistryHelpers.get_asset_registry()
far = unreal.ARFilter(
    class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "Texture2D")],
    recursive_paths=True, package_paths=[path])
assets = ar.get_assets(far) or []
total_found = len(assets)
loaded = 0
skipped_load = 0
truncated = total_found > max_load
groups = {}
tot_px = 0
tot_src_mem = 0
tot_resident = 0
for d in assets[:max_load]:
    p = str(d.get_editor_property("package_name")) + "." + str(d.get_editor_property("asset_name"))
    t = _try(lambda p=p: unreal.EditorAssetLibrary.load_asset(p))
    if t is None or not isinstance(t, unreal.Texture2D):
        skipped_load += 1
        continue
    loaded += 1
    bts = _try(lambda: t.blueprint_get_built_texture_size())
    if bts is not None:
        w = _try(lambda: int(bts.x)) or 0
        h = _try(lambda: int(bts.y)) or 0
    else:
        w = _try(lambda: t.blueprint_get_size_x()) or 0
        h = _try(lambda: t.blueprint_get_size_y()) or 0
    px = int(w) * int(h)
    if group_by == "lod_group":
        key = _enum_short(_try(lambda: t.get_editor_property("lod_group"))) or "unknown"
    else:
        key = _enum_short(_try(lambda: t.get_editor_property("compression_settings"))) or "unknown"
    dm = _try(lambda: t.blueprint_get_texture_source_disk_and_memory_size())
    src_mem = int(dm[1]) if (isinstance(dm, (tuple, list)) and len(dm) >= 2) else 0
    resident = _try(lambda: t.blueprint_get_memory_size()) or 0
    g = groups.setdefault(key, {"count": 0, "pixels": 0, "megapixels": 0.0,
                                "source_memory_bytes": 0, "resident_gpu_bytes": 0})
    g["count"] += 1
    g["pixels"] += px
    g["source_memory_bytes"] += src_mem
    g["resident_gpu_bytes"] += int(resident)
    tot_px += px
    tot_src_mem += src_mem
    tot_resident += int(resident)
for k, g in groups.items():
    g["megapixels"] = round(g["pixels"] / 1000000.0, 3)
out = {"status": "success", "path": path, "group_by": ("lod_group" if group_by == "lod_group" else "format(compression_settings)"),
       "total_found": total_found, "loaded": loaded, "skipped_load": skipped_load,
       "truncated": truncated, "max_load": max_load,
       "totals": {"count": loaded, "megapixels": round(tot_px / 1000000.0, 3),
                  "source_memory_bytes": tot_src_mem, "resident_gpu_bytes": tot_resident},
       "groups": groups,
       "note": "megapixels = sum of built top-mip width*height per texture (blueprint_get_built_texture_size; stable). source_memory_bytes = sum of uncompressed source sizes. resident_gpu_bytes = sum of CURRENTLY-RESIDENT GPU memory (editor-session estimate; streamed textures under-report until warmed). Each Texture2D is loaded to read its props."}
print("@@UMCP@@" + json.dumps(out))
'''

    @mcp.tool()
    def get_texture_stats(ctx, path: str = "/Game", group_by: str = "format",
                          max_load: int = 500) -> str:
        """Roll-up statistics over the Texture2D assets under a content path. Read-only.

        path:     content path to scan recursively (default '/Game'). NOTE: this LOADS each
                  Texture2D to read its props, so keep the path bounded (avoid '/Engine' whole,
                  which has thousands); 'max_load' caps how many are loaded and 'truncated' flags it.
        group_by: 'format' (group by compression_settings, default) or 'lod_group'.
        max_load: max textures to load (default 500).

        Returns per-group {count, pixels, megapixels, source_memory_bytes, resident_gpu_bytes}
        plus totals. megapixels (sum of built width*height) is the reliable metric; the memory
        figures carry the same caveats as get_texture_info (resident GPU = current editor-session
        estimate; source_memory = uncompressed source bytes)."""
        params = {"path": path, "group_by": group_by, "max_load": max_load}
        try:
            return json.dumps(_exec(_STATS_BODY, params), indent=2)
        except Exception as e:
            return f"Error: {e}"
