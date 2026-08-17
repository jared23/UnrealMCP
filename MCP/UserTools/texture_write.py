"""UserTools :: Texture asset-settings (WRITE)  (spec: docs/spec/editor.md)

Clean-room, REVERSIBLE Texture2D asset-settings authoring over Unreal's public Python API
(UE 5.8). The mutating counterpart to texture_read.py. Query convention (@@UMCP@@<json>),
base64 PARAMS injection, Output-Log auto-capture, and the per-session undo ledger are copied
VERBATIM from the gold-standard editor_level.py.

What this exposes (all NON-MODAL, verified live vs TestMCPSetup UE 5.8.1 on a scratch duplicate):
  - set_texture_compression_settings (compression_settings enum: TC_DEFAULT/TC_NORMALMAP/TC_MASKS/...)
  - set_texture_lod_group            (lod_group enum: TEXTUREGROUP_WORLD/UI/CHARACTER/...)
  - set_texture_srgb                 (srgb bool)
  - set_texture_address              (address_x / address_y enum: TA_WRAP/TA_CLAMP/TA_MIRROR)
  - set_texture_filter               (filter enum: TF_NEAREST/TF_BILINEAR/TF_TRILINEAR/TF_DEFAULT)
  - set_texture_mip_gen_settings     (mip_gen_settings enum: TMGS_FROM_TEXTURE_GROUP/TMGS_NO_MIPMAPS/...)
  - set_texture_lod_bias             (lod_bias int)
  - set_texture_max_size             (max_texture_size int; 0 = no cap)

Reversibility model (IMPORTANT — learned live 2026-08-16):
  set_editor_property on a texture triggers PostEditChangeProperty, which ENFORCES texture
  constraints. Setting compression_settings=TC_NORMALMAP/TC_MASKS silently forces srgb -> False;
  restoring compression_settings ALONE does NOT bring srgb back. So a single-property inverse is
  NOT faithful for compression/lod_group. Instead EVERY write captures a FULL prior snapshot of
  all managed settings fields and the inverse re-applies that whole snapshot (twice, to converge
  any single-step cascade). This is uniformly faithful regardless of which field cascaded.
  The forward call reports both the REQUESTED value and the APPLIED (read-back) value so a
  constraint-driven no-op (e.g. srgb=True refused on a masks texture) is visible, not hidden.

ONE generic ledger op is pushed per call:
  op "set_texture_property" {asset_path, changed:[prop...], requested:{...}, applied:{...},
                             prior:{field:{kind,value,enum_type}}}
  inverse: for each field in `prior`, resolve value (enum member-name via getattr / bool / int)
           and set_editor_property; run the loop TWICE; then EditorAssetLibrary.save_asset.
  -> FAITHFUL full restore of every managed field. Schema reported in the module footer for the
     coordinator to fold into editor_level.undo (this module registers NO `undo` tool).

DEFERRED / not shipped (no clean/faithful Python route in 5.8, refused-not-faked):
  - power_of_two_mode, virtual_texture_streaming, flip_green_channel, compression_no_alpha,
    never_stream, mip_load_options: settable but out of the requested scope; can be added later
    under the same generic op if wanted. (No blocker found; simply not in this batch's scope.)
  - Source pixels / import re-settings (Texture2D.Source is a protected UPROPERTY; see texture_read.py).
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

# --- Output Log auto-capture (copied verbatim from editor_level.py) ----------
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

# NOTE: the plugin's execute_python wraps incoming code in triple-SINGLE-quotes before exec, so
# snippet bodies must contain NO triple-single-quote and NO stray backslashes. All data is passed as
# base64. Never assign a snippet variable named sys/unreal/traceback/output_file/error_file/
# original_stdout/original_stderr/success/user_code/code_obj (the wrapper's own names).


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

    # Shared Unreal-side helpers (prepended to the write body). No triple-single-quote / no backslash.
    #   _ledger()            -> per-session undo stack (copied verbatim from editor_level.py).
    #   _es(v)               -> enum repr -> short member name ("<TextureAddress.TA_WRAP: 0>" -> "TA_WRAP").
    #   _resolve_enum(cls,n) -> case-insensitive member lookup -> (member, canonical_name) or (None,None).
    #   FIELDS               -> the managed settings fields captured in every prior snapshot.
    _TEX_HELPERS = r'''
import unreal, json, builtins
def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]
def _es(v):
    if v is None:
        return None
    s = str(v)
    if "." in s and ":" in s:
        return s.split(".")[-1].split(":")[0].strip()
    return s
def _resolve_enum(cls, name):
    if cls is None or name is None:
        return None, None
    base = str(name).strip()
    for cand in (base, base.upper()):
        m = getattr(cls, cand, None)
        if isinstance(m, cls):
            return m, cand
    def _norm(s):
        return s.upper().replace("_", "")
    target = _norm(base)
    for a in dir(cls):
        m = getattr(cls, a, None)
        if isinstance(m, cls) and _norm(a) == target:
            return m, a
    return None, None
FIELDS = [
    ("compression_settings", "enum", "TextureCompressionSettings"),
    ("lod_group",            "enum", "TextureGroup"),
    ("srgb",                 "bool", None),
    ("address_x",            "enum", "TextureAddress"),
    ("address_y",            "enum", "TextureAddress"),
    ("filter",               "enum", "TextureFilter"),
    ("mip_gen_settings",     "enum", "TextureMipGenSettings"),
    ("lod_bias",             "int",  None),
    ("max_texture_size",     "int",  None),
]
def _snapshot(tex):
    snap = {}
    for fname, kind, etype in FIELDS:
        try:
            raw = tex.get_editor_property(fname)
        except Exception:
            continue
        if kind == "enum":
            snap[fname] = {"kind": "enum", "value": _es(raw), "enum_type": etype}
        elif kind == "bool":
            snap[fname] = {"kind": "bool", "value": bool(raw), "enum_type": None}
        else:
            snap[fname] = {"kind": "int", "value": int(raw), "enum_type": None}
    return snap
'''

    # ------------------------------------------------------------------ #
    # Shared write body: apply a list of property sets to one Texture2D   #
    # ------------------------------------------------------------------ #
    # PARAMS["sets"] = [{"prop": name, "kind": "enum"/"bool"/"int",
    #                    "enum_type": <classname or null>, "value": <input>}]
    _SET_TEX_BODY = _TEX_HELPERS + r'''
path = PARAMS["path"]
sets = PARAMS.get("sets") or []
EAL = unreal.EditorAssetLibrary
tex = EAL.load_asset(path)
if tex is None:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "could not load asset: %s" % path}))
elif not isinstance(tex, unreal.Texture2D):
    print("@@UMCP@@" + json.dumps({"status": "error",
        "message": "asset is not a Texture2D (got %s): %s" % (tex.get_class().get_name(), path)}))
elif not sets:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "no property to set (all args were None)"}))
else:
    prepared = []
    err = None
    for s in sets:
        prop = s.get("prop"); kind = s.get("kind")
        try:
            tex.get_editor_property(prop)
        except Exception as e:
            err = "cannot read property %s: %s" % (prop, str(e)); break
        if kind == "enum":
            cls = getattr(unreal, s.get("enum_type"), None)
            if cls is None:
                err = "unknown enum type %s for %s" % (s.get("enum_type"), prop); break
            member, canon = _resolve_enum(cls, s.get("value"))
            if member is None:
                valid = [a for a in dir(cls) if isinstance(getattr(cls, a, None), cls)]
                err = "invalid %s value %r; valid: %s" % (prop, s.get("value"), ", ".join(sorted(valid))); break
            prepared.append({"prop": prop, "kind": "enum", "newv": member,
                             "requested": canon})
        elif kind == "bool":
            v = s.get("value")
            if isinstance(v, str):
                v = v.strip().lower() in ("true", "1", "yes", "on")
            prepared.append({"prop": prop, "kind": "bool", "newv": bool(v), "requested": bool(v)})
        elif kind == "int":
            try:
                iv = int(s.get("value"))
            except Exception:
                err = "%s expects an integer, got %r" % (prop, s.get("value")); break
            if prop == "max_texture_size" and iv < 0:
                err = "max_texture_size must be >= 0 (0 = no cap)"; break
            prepared.append({"prop": prop, "kind": "int", "newv": iv, "requested": iv})
        else:
            err = "unknown kind %r for %s" % (kind, prop); break
    if err is not None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": err}))
    else:
        prior_snapshot = _snapshot(tex)
        with unreal.ScopedEditorTransaction("MCP set_texture_property"):
            for p in prepared:
                tex.set_editor_property(p["prop"], p["newv"])
        try:
            EAL.save_asset(path, only_if_is_dirty=False)
        except Exception:
            pass
        changed = [p["prop"] for p in prepared]
        requested = {}
        applied = {}
        for p in prepared:
            requested[p["prop"]] = (p["requested"] if p["kind"] != "bool" else bool(p["requested"]))
            raw = tex.get_editor_property(p["prop"])
            if p["kind"] == "enum":
                applied[p["prop"]] = _es(raw)
            elif p["kind"] == "bool":
                applied[p["prop"]] = bool(raw)
            else:
                applied[p["prop"]] = int(raw)
        constrained = {k: {"requested": requested[k], "applied": applied[k]}
                       for k in changed if requested[k] != applied[k]}
        _ledger().append({"op": "set_texture_property", "asset_path": path,
                          "changed": changed, "requested": requested, "applied": applied,
                          "prior": prior_snapshot})
        result = {"status": "success", "path": tex.get_path_name(), "name": tex.get_name(),
                  "changed": changed, "requested": requested, "applied": applied,
                  "ledger_depth": len(_ledger())}
        if constrained:
            result["constrained_note"] = ("some requested values were adjusted by texture "
                "constraints (e.g. masks/normalmap force srgb off); 'applied' is the real state")
            result["constrained"] = constrained
        print("@@UMCP@@" + json.dumps(result))
'''

    def _do_set(sets, path):
        return json.dumps(_exec(_SET_TEX_BODY, {"path": path, "sets": sets}), indent=2)

    # ------------------------------------------------------------------ #
    @mcp.tool()
    def set_texture_compression_settings(ctx, path: str, compression_settings: str) -> str:
        """Set a Texture2D's compression_settings (TextureCompressionSettings enum). Reversible.

        path:                 Texture2D asset path, e.g.
                              '/Game/LevelPrototyping/Textures/T_GridChecker_A.T_GridChecker_A'.
        compression_settings: enum member name (case-insensitive), e.g. 'TC_Default', 'TC_Normalmap',
                              'TC_Masks', 'TC_Grayscale', 'TC_HDR', 'TC_BC7', 'TC_Alpha'.

        NOTE: changing compression can CASCADE — masks/normalmap compression forces srgb off. The
        response reports 'applied' (real read-back) vs 'requested', and any cascade is fully captured
        in the ledger snapshot so `undo` restores every affected field faithfully. Saved on write.

        Ledgered op 'set_texture_property' (full prior snapshot; inverse re-applies the snapshot)."""
        try:
            return _do_set([{"prop": "compression_settings", "kind": "enum",
                             "enum_type": "TextureCompressionSettings", "value": compression_settings}], path)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_texture_lod_group(ctx, path: str, lod_group: str) -> str:
        """Set a Texture2D's lod_group (TextureGroup enum). Reversible.

        path:      Texture2D asset path.
        lod_group: enum member name (case-insensitive), e.g. 'TEXTUREGROUP_World', 'TEXTUREGROUP_UI',
                   'TEXTUREGROUP_Character', 'TEXTUREGROUP_Effects', 'TEXTUREGROUP_WorldNormalMap'.

        The lod group drives streaming/mip behavior when mip_gen_settings=TMGS_FROM_TEXTURE_GROUP.
        Saved on write. Ledgered op 'set_texture_property' (full prior snapshot; faithful undo)."""
        try:
            return _do_set([{"prop": "lod_group", "kind": "enum",
                             "enum_type": "TextureGroup", "value": lod_group}], path)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_texture_srgb(ctx, path: str, srgb: bool) -> str:
        """Set a Texture2D's srgb flag (bool). Reversible.

        path: Texture2D asset path.
        srgb: True to treat the texture as sRGB-encoded (color), False for linear (data/masks/normals).

        CONSTRAINT: some compression settings force srgb (masks/normalmap force it OFF). If the
        requested value is refused by the texture, the response 'applied' shows the real state and
        'constrained' flags the difference — this is engine behavior, not a failure. Saved on write.

        Ledgered op 'set_texture_property' (full prior snapshot; faithful undo)."""
        try:
            return _do_set([{"prop": "srgb", "kind": "bool", "enum_type": None, "value": srgb}], path)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_texture_address(ctx, path: str, address_x: str = None, address_y: str = None) -> str:
        """Set a Texture2D's UV addressing mode (TextureAddress enum) on X and/or Y. Reversible.

        path:      Texture2D asset path.
        address_x: enum member name for the U axis: 'TA_Wrap', 'TA_Clamp', 'TA_Mirror' (optional).
        address_y: enum member name for the V axis (optional). At least one of x/y is required.

        Both axes are set in one atomic transaction and one ledger op. Saved on write.
        Ledgered op 'set_texture_property' (full prior snapshot; faithful undo)."""
        sets = []
        if address_x is not None:
            sets.append({"prop": "address_x", "kind": "enum", "enum_type": "TextureAddress", "value": address_x})
        if address_y is not None:
            sets.append({"prop": "address_y", "kind": "enum", "enum_type": "TextureAddress", "value": address_y})
        if not sets:
            return json.dumps({"status": "error", "message": "provide address_x and/or address_y"}, indent=2)
        try:
            return _do_set(sets, path)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_texture_filter(ctx, path: str, filter: str) -> str:
        """Set a Texture2D's sampler filter (TextureFilter enum). Reversible.

        path:   Texture2D asset path.
        filter: enum member name (case-insensitive): 'TF_Nearest', 'TF_Bilinear', 'TF_Trilinear',
                'TF_Default' (use the texture group's default).

        Saved on write. Ledgered op 'set_texture_property' (full prior snapshot; faithful undo)."""
        try:
            return _do_set([{"prop": "filter", "kind": "enum",
                             "enum_type": "TextureFilter", "value": filter}], path)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_texture_mip_gen_settings(ctx, path: str, mip_gen_settings: str) -> str:
        """Set a Texture2D's mip generation settings (TextureMipGenSettings enum). Reversible.

        path:             Texture2D asset path.
        mip_gen_settings: enum member name (case-insensitive), e.g. 'TMGS_FromTextureGroup',
                          'TMGS_NoMipmaps', 'TMGS_LeaveExistingMips', 'TMGS_Sharpen0'..'TMGS_Sharpen10',
                          'TMGS_Blur1'..'TMGS_Blur5', 'TMGS_Angular'.

        Saved on write. Ledgered op 'set_texture_property' (full prior snapshot; faithful undo)."""
        try:
            return _do_set([{"prop": "mip_gen_settings", "kind": "enum",
                             "enum_type": "TextureMipGenSettings", "value": mip_gen_settings}], path)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_texture_lod_bias(ctx, path: str, lod_bias: int) -> str:
        """Set a Texture2D's lod_bias (integer mip bias). Reversible.

        path:     Texture2D asset path.
        lod_bias: integer; higher drops more of the top mips (lower resolution). 0 = no bias.

        Saved on write. Ledgered op 'set_texture_property' (full prior snapshot; faithful undo)."""
        try:
            return _do_set([{"prop": "lod_bias", "kind": "int", "enum_type": None, "value": lod_bias}], path)
        except Exception as e:
            return f"Error: {e}"

    @mcp.tool()
    def set_texture_max_size(ctx, path: str, max_texture_size: int) -> str:
        """Set a Texture2D's max_texture_size (integer cap on the built texture's largest dimension). Reversible.

        path:             Texture2D asset path.
        max_texture_size: integer >= 0; 0 means no cap (use the source size). A positive value caps the
                          built texture to that many pixels on its longest edge.

        Saved on write. Ledgered op 'set_texture_property' (full prior snapshot; faithful undo)."""
        try:
            return _do_set([{"prop": "max_texture_size", "kind": "int",
                             "enum_type": None, "value": max_texture_size}], path)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------------------------------- #
    # UNDO OP SCHEMA (for the coordinator to fold into editor_level.undo — NOT registered here).   #
    # This module registers NO `undo` tool (editor_level.py owns the ONE unified `undo`).          #
    #                                                                                              #
    #   op "set_texture_property" {asset_path, changed:[prop...], requested:{...}, applied:{...},   #
    #                              prior:{field:{"kind":"enum"/"bool"/"int","value":...,            #
    #                                            "enum_type":<classname or null>}}}                 #
    #   inverse:                                                                                    #
    #     tex = unreal.EditorAssetLibrary.load_asset(asset_path)                                    #
    #     for _pass in range(2):   # two passes converge any single-step constraint cascade         #
    #         for fname, meta in entry["prior"].items():                                            #
    #             k = meta["kind"]; v = meta["value"]                                               #
    #             if k == "enum":                                                                   #
    #                 cls = getattr(unreal, meta["enum_type"], None)                                #
    #                 nv = getattr(cls, v, None) if cls else None                                   #
    #                 if nv is not None: tex.set_editor_property(fname, nv)                          #
    #             elif k == "bool": tex.set_editor_property(fname, bool(v))                          #
    #             elif k == "int":  tex.set_editor_property(fname, int(v))                           #
    #     unreal.EditorAssetLibrary.save_asset(asset_path, only_if_is_dirty=False)                  #
    #   -> FAITHFUL: restores the exact prior state of every managed settings field (which was a    #
    #      self-consistent state), so cascades (compression->srgb) are undone correctly.            #
    # ------------------------------------------------------------------------------------------- #
