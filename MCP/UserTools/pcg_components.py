"""UserTools :: PCG component / generation runtime tools  (spec: docs/spec/pcg.md — Wave 4)

Unlike Waves 1-3 (asset-only), these tools operate on a UPCGComponent that lives on an ACTOR in the
editor LEVEL (a PCGVolume, or any actor carrying a UPCGComponent). Pure-Python over the reflected
UPCGComponent / UPCGBlueprintHelpers / APCGPartitionActor surface (UE 5.8, PCG plugin loaded). NO
C++ / NO engine build.

SAFETY: these tools NEVER save the level (the transient world must never be written to disk). The
only ledgered tool (pcg_set_component_graph) mutates the component in memory and records ONE inverse
op on the per-session agent ledger (builtins._UMCP_LEDGERS[session]) WITHOUT saving; the coordinator
folds it into editor_level.undo. Generate/Cleanup/Flush/Refresh are NON-LEDGERED runtime operations
whose natural inverse is the paired Cleanup/Generate call (documented per tool) — no editor_level
fold is needed for them.

Reflection facts probed live (2026-08-19, TestMCPSetup, transient level /Temp/Untitled_0.Untitled):
  - unreal.PCGComponent / PCGVolume / PCGBlueprintHelpers / PCGPartitionActor / PCGGraphInterface /
    PCGGraphInstance are all bound. unreal.PCGSubsystem is NOT bound by name (so subsystem-only legs
    are routed through PCGBlueprintHelpers / the component API instead).
  - The UPCGComponent on an actor is reachable via actor.get_components_by_class(unreal.PCGComponent)
    (robust for any actor) OR, on a PCGVolume, the reflected `pcg_component` property. Its get_name()
    display is 'PCG Component'.
  - UPCGComponent methods (snake_case, BlueprintCallable, signatures from __doc__):
      set_graph(graph: PCGGraphInterface) -> None ; get_graph() -> PCGGraph (top of instance stack)
      generate(force: bool) -> None ; generate_local(force: bool) -> None
      cleanup(remove_components: bool) -> None ; cleanup_local(remove_components: bool) -> None
      clear_pcg_link(template_actor=None) -> Actor  [moves generated resources under a NEW actor and
          returns it; returns None when there is nothing generated -> no actor spawned]
      get_generated_graph_output() -> PCGDataCollection  [a STRUCT, not a UObject: fields tagged_data
          (array) + cancel_execution_on_empty (bool)]
      refresh_pcg_runtime_component(flush_cache=False) -> None
      notify_properties_changed_from_blueprint() -> None ; regenerate_in_editor (bool R/W)
  - UPCGComponent reflected PROPERTIES (get_editor_property): is_component_partitioned (bool R/W),
    generated (bool), seed (int), generation_trigger (PCGComponentGenerationTrigger enum),
    graph_instance (UPCGGraphInstance), dirty_generated (bool R/O).
  - Generation in-editor is ASYNCHRONOUS / DEFERRED (the docstrings say "Will be delayed"); the
    `generated` flag does NOT flip synchronously in the same call, and an empty graph generates
    nothing. So these tools report the call succeeded + the (possibly still-stale) state readback,
    they do NOT block waiting for generation to finish.
  - PCGBlueprintHelpers.flush_pcg_cache() -> bool (static, no args; == `pcg.FlushCache`).
    PCGBlueprintHelpers.refresh_pcg_runtime_component(component, flush_cache=False) -> None (static;
    the reachable way to refresh a specific component). PCGBlueprintHelpers.get_component(context)
    takes a PCGContext (not an actor) so it is NOT used for actor->component lookup.
  - APCGPartitionActor: bp_get_pcg_grid_size() -> int64, get_local_component(orig), runtime_grid.
    None exist in a transient (non-World-Partition) level -> list/get report empty honestly.

REUSE: _wrap/_LOG_HEAD/_LOG_TAILER/_query/_exec scaffold + base64 PARAMS + per-session _ledger()
copied from pcg_compute.py / pcg_write.py. No ''' and no backslashes in any snippet body; all data
crosses as base64 PARAMS; reserved locals untouched.

NEW pcg_* ledger op handed to the coordinator (op -> inverse):
  - pcg_set_component_graph{actor_path,actor_label,actor_name,prior_graph,had_prior}
        -> re-find the component-on-actor (by actor_path, fallback actor_name/label) and
           set_graph(load(prior_graph)) — restores the prior graph (or clears if prior was None).
           BEST-EFFORT in a transient never-saved level: reliable within the live session while the
           actor still exists; if the transient level is discarded / the actor destroyed, the fold
           silently no-ops (see UNDO REPORT).
  All generate/cleanup/local/link/flush/refresh/all-components tools are NON-LEDGERED runtime ops.
"""
import json
import base64
import textwrap
import os

MARKER = "@@UMCP@@"
LOG_MARKER = "@@UMCP_LOG@@"

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
    return _LOG_HEAD + textwrap.indent(code, "    ") + _LOG_TAILER


# Shared component/level helpers (prepended to every body). No ''' / no backslashes.
_PCGC_HELPERS = r'''
import unreal, json, builtins
warnings = None
try:
    import warnings as _wmod
    _wmod.simplefilter("ignore")
except Exception:
    pass

def _ledger():
    sid = PARAMS.get("_session", "default")
    root = getattr(builtins, "_UMCP_LEDGERS", None)
    if root is None:
        root = {}; builtins._UMCP_LEDGERS = root
    if sid not in root:
        root[sid] = []
    return root[sid]

def _norm_path(p):
    return p.split(".")[0] if p else p

def _world_ok():
    # SAFETY gate: refuse to touch the level unless it is the transient world.
    w = unreal.EditorLevelLibrary.get_editor_world()
    wp = w.get_path_name() if w else None
    return (bool(wp and wp.startswith("/Temp/")), wp)

def _all_actors():
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    return list(eas.get_all_level_actors() or [])

def _resolve_actor(ident):
    if not ident:
        return None
    key = str(ident); kl = key.lower()
    acts = _all_actors()
    for a in acts:
        try:
            if a.get_path_name() == key:
                return a
        except Exception:
            pass
    for a in acts:
        try:
            if a.get_name() == key:
                return a
        except Exception:
            pass
    for a in acts:
        try:
            if a.get_actor_label() == key:
                return a
        except Exception:
            pass
    for a in acts:
        try:
            if a.get_actor_label().lower() == kl:
                return a
        except Exception:
            pass
    return None

def _actor_ident(a):
    d = {}
    try:
        d["actor_path"] = a.get_path_name()
    except Exception:
        d["actor_path"] = None
    try:
        d["actor_label"] = a.get_actor_label()
    except Exception:
        d["actor_label"] = None
    try:
        d["actor_name"] = a.get_name()
    except Exception:
        d["actor_name"] = None
    return d

def _find_pcg_component(actor):
    if actor is None:
        return None
    try:
        comps = actor.get_components_by_class(unreal.PCGComponent)
        if comps:
            lst = list(comps)
            if len(lst) > 0:
                return lst[0]
    except Exception:
        pass
    try:
        c = actor.get_editor_property("pcg_component")
        if isinstance(c, unreal.PCGComponent):
            return c
    except Exception:
        pass
    return None

def _graph_path_of(comp):
    try:
        g = comp.get_graph()
        return _norm_path(g.get_path_name()) if g else None
    except Exception:
        return None

def _load_graph_iface(p):
    # returns (obj_or_None, err). obj None + err None means "no/clear graph" (valid).
    if not p:
        return None, None
    a = unreal.EditorAssetLibrary.load_asset(p)
    if a is None:
        return None, "graph asset not found: %s" % p
    ok = False
    iface = getattr(unreal, "PCGGraphInterface", None)
    if iface is not None:
        try:
            ok = isinstance(a, iface)
        except Exception:
            ok = False
    if not ok:
        try:
            gi = getattr(unreal, "PCGGraphInstance", None)
            ok = isinstance(a, unreal.PCGGraph) or (gi is not None and isinstance(a, gi))
        except Exception:
            ok = False
    if not ok:
        cn = "?"
        try:
            cn = a.get_class().get_name()
        except Exception:
            pass
        return None, "asset is not a PCGGraph/PCGGraphInstance (class=%s): %s" % (cn, p)
    return a, None

def _resolve_comp_or_err(ident):
    # common entry: (comp, actor, err_payload_or_None)
    ok, wp = _world_ok()
    if not ok:
        return None, None, {"status": "error", "message": "editor world is not a transient /Temp/ level (got %s); component tools refuse to run" % wp}
    actor = _resolve_actor(ident)
    if actor is None:
        return None, None, {"status": "error", "message": "actor not found in level: %s" % ident}
    comp = _find_pcg_component(actor)
    if comp is None:
        return None, actor, {"status": "error", "message": "no UPCGComponent on actor: %s" % ident, "actor": _actor_ident(actor)}
    return comp, actor, None

def _output_summary(comp):
    try:
        dc = comp.get_generated_graph_output()
    except Exception as _e:
        return {"error": "get_generated_graph_output failed: %s" % _e}
    if dc is None:
        return {"tagged_data_count": 0, "is_null": True}
    rec = {"is_null": False}
    try:
        td = dc.get_editor_property("tagged_data")
        lst = list(td) if td is not None else []
        rec["tagged_data_count"] = len(lst)
        summ = []
        for i, e in enumerate(lst):
            if i >= 25:
                summ.append("...(%d more)" % (len(lst) - 25)); break
            item = {}
            try:
                data = e.get_editor_property("data")
                item["data_class"] = data.get_class().get_name() if data else None
            except Exception:
                item["data_class"] = None
            try:
                tags = e.get_editor_property("tags")
                item["tags"] = [str(t) for t in list(tags)] if tags is not None else []
            except Exception:
                item["tags"] = None
            summ.append(item)
        rec["tagged_data"] = summ
    except Exception as _e:
        rec["tagged_data_count"] = None
        rec["tagged_data_err"] = str(_e)
    try:
        rec["cancel_execution_on_empty"] = bool(dc.get_editor_property("cancel_execution_on_empty"))
    except Exception:
        pass
    return rec
'''


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

    # ------------------------------------------------------------------ #
    # 1. pcg_set_component_graph — assign a graph to a component (LEDGERED) #
    # ------------------------------------------------------------------ #
    _SET_GRAPH_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
gp = PARAMS.get("graph_path")
comp, actor, err = _resolve_comp_or_err(ident)
if err is not None:
    print("@@UMCP@@" + json.dumps(err))
else:
    giface, gerr = _load_graph_iface(gp)
    if gerr is not None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": gerr}))
    else:
        prior = _graph_path_of(comp)
        had_prior = prior is not None
        try:
            comp.set_graph(giface)
        except Exception as _e:
            print("@@UMCP@@" + json.dumps({"status": "error", "message": "set_graph failed: %s" % _e}))
            comp = None
        if comp is not None:
            newp = _graph_path_of(comp)
            ident_d = _actor_ident(actor)
            _ledger().append({"op": "pcg_set_component_graph",
                              "actor_path": ident_d.get("actor_path"),
                              "actor_label": ident_d.get("actor_label"),
                              "actor_name": ident_d.get("actor_name"),
                              "prior_graph": prior, "had_prior": had_prior})
            res = {"status": "success", "actor": ident_d,
                   "component": comp.get_name(),
                   "prior_graph": prior, "new_graph": newp, "had_prior": had_prior,
                   "saved": False,
                   "undo_op": "pcg_set_component_graph", "ledger_depth": len(_ledger())}
            print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def pcg_set_component_graph(ctx, actor: str, graph_path: str = None) -> str:
        """Assign a PCGGraph (or PCGGraphInstance) asset to the UPCGComponent on a level actor
        (UPCGComponent.set_graph). Captures the prior graph. LEDGERED write (op
        'pcg_set_component_graph' -> `undo` re-finds the component-on-actor and restores the prior
        graph; clears if there was none). The LEVEL is NOT saved.

        actor:      actor identity in the current (transient) level — its label (e.g. 'PCGVolume'),
                    internal name (e.g. 'PCGVolume_0'), or full path name. Must carry a UPCGComponent.
        graph_path: content path to a PCGGraph / PCGGraphInstance to assign. Pass null/empty to CLEAR
                    the component's graph (set_graph(None)).

        NOTE: assigning a graph does not mutate the graph asset; it mutates the in-memory component.
        In a transient never-saved level the undo fold's re-resolution is best-effort (see module
        docstring)."""
        try:
            return json.dumps(_exec(_SET_GRAPH_BODY,
                {"actor": actor, "graph_path": graph_path}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 2. pcg_get_component_info — read graph/partition/generated (READ)     #
    # ------------------------------------------------------------------ #
    _GET_INFO_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
comp, actor, err = _resolve_comp_or_err(ident)
if err is not None:
    print("@@UMCP@@" + json.dumps(err))
else:
    res = {"status": "success", "actor": _actor_ident(actor), "component": comp.get_name()}
    res["graph"] = _graph_path_of(comp)
    try:
        gi = comp.get_editor_property("graph_instance")
        res["graph_instance"] = gi.get_name() if gi else None
    except Exception:
        res["graph_instance"] = None
    for p in ("is_component_partitioned", "generated", "dirty_generated", "seed",
              "regenerate_in_editor"):
        try:
            v = comp.get_editor_property(p)
            res[p] = bool(v) if p in ("is_component_partitioned", "generated", "dirty_generated", "regenerate_in_editor") else v
        except Exception as _e:
            res[p + "_err"] = str(_e)[:80]
    try:
        res["generation_trigger"] = str(comp.get_editor_property("generation_trigger"))
    except Exception:
        res["generation_trigger"] = None
    print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def pcg_get_component_info(ctx, actor: str) -> str:
        """Read a level actor's UPCGComponent state: assigned graph, graph_instance, partitioned flag,
        generated / dirty_generated flags, seed, generation_trigger, regenerate_in_editor. Read-only,
        no ledger.

        actor: actor identity (label / name / path name) in the current level."""
        try:
            return json.dumps(_exec(_GET_INFO_BODY, {"actor": actor}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 3. pcg_generate_component — Generate (NON-LEDGERED runtime)           #
    # ------------------------------------------------------------------ #
    _GENERATE_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
force = bool(PARAMS.get("force"))
comp, actor, err = _resolve_comp_or_err(ident)
if err is not None:
    print("@@UMCP@@" + json.dumps(err))
else:
    gen_before = None
    try:
        gen_before = bool(comp.get_editor_property("generated"))
    except Exception:
        pass
    try:
        comp.generate(force)
    except Exception as _e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "generate failed: %s" % _e}))
        comp = None
    if comp is not None:
        gen_after = None
        try:
            gen_after = bool(comp.get_editor_property("generated"))
        except Exception:
            pass
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": _actor_ident(actor),
            "component": comp.get_name(), "force": force,
            "generated_before": gen_before, "generated_after": gen_after,
            "async_note": "in-editor generation is deferred/async; 'generated' may not flip synchronously and an empty graph generates nothing",
            "ledgered": False, "natural_inverse": "pcg_cleanup_component"}, default=str))
'''

    @mcp.tool()
    def pcg_generate_component(ctx, actor: str, force: bool = False) -> str:
        """Trigger generation on a level actor's UPCGComponent (UPCGComponent.generate(force)).
        NON-LEDGERED runtime op — its natural inverse is pcg_cleanup_component (Generate <-> Cleanup),
        so no editor_level undo fold is needed. The LEVEL is NOT saved.

        actor: actor identity (label / name / path name).
        force: pass True to force regeneration even if not dirty.

        NOTE: in-editor generation is deferred/asynchronous — the call returns immediately and the
        'generated' flag may not flip within this call; an empty graph produces nothing."""
        try:
            return json.dumps(_exec(_GENERATE_BODY, {"actor": actor, "force": force}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 4. pcg_cleanup_component — Cleanup (NON-LEDGERED runtime)             #
    # ------------------------------------------------------------------ #
    _CLEANUP_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
remove_components = bool(PARAMS.get("remove_components"))
comp, actor, err = _resolve_comp_or_err(ident)
if err is not None:
    print("@@UMCP@@" + json.dumps(err))
else:
    gen_before = None
    try:
        gen_before = bool(comp.get_editor_property("generated"))
    except Exception:
        pass
    try:
        comp.cleanup(remove_components)
    except Exception as _e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "cleanup failed: %s" % _e}))
        comp = None
    if comp is not None:
        gen_after = None
        try:
            gen_after = bool(comp.get_editor_property("generated"))
        except Exception:
            pass
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": _actor_ident(actor),
            "component": comp.get_name(), "remove_components": remove_components,
            "generated_before": gen_before, "generated_after": gen_after,
            "ledgered": False, "natural_inverse": "pcg_generate_component"}, default=str))
'''

    @mcp.tool()
    def pcg_cleanup_component(ctx, actor: str, remove_components: bool = True) -> str:
        """Clean up a level actor's UPCGComponent generated output (UPCGComponent.cleanup(
        remove_components)). NON-LEDGERED runtime op — the inverse of pcg_generate_component
        (Cleanup <-> Generate), so no editor_level undo fold is needed. The LEVEL is NOT saved.

        actor:             actor identity (label / name / path name).
        remove_components: True (default) also removes the managed components that generation spawned."""
        try:
            return json.dumps(_exec(_CLEANUP_BODY,
                {"actor": actor, "remove_components": remove_components}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 5. pcg_generate_local — GenerateLocal (NON-LEDGERED, PARTIAL)         #
    # ------------------------------------------------------------------ #
    _GENERATE_LOCAL_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
force = bool(PARAMS.get("force"))
comp, actor, err = _resolve_comp_or_err(ident)
if err is not None:
    print("@@UMCP@@" + json.dumps(err))
else:
    try:
        part = bool(comp.get_editor_property("is_component_partitioned"))
    except Exception:
        part = None
    try:
        comp.generate_local(force)
    except Exception as _e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "generate_local failed: %s" % _e}))
        comp = None
    if comp is not None:
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": _actor_ident(actor),
            "component": comp.get_name(), "force": force, "is_component_partitioned": part,
            "partial_note": "GenerateLocal is the local (non-replicated, delayed) path; its partitioned-dispatch effect is only meaningful when is_component_partitioned is True, which requires a saved World-Partition level (false in a transient level). The call itself is reachable and does not error.",
            "ledgered": False, "natural_inverse": "pcg_cleanup_local"}, default=str))
'''

    @mcp.tool()
    def pcg_generate_local(ctx, actor: str, force: bool = False) -> str:
        """Trigger LOCAL generation on a level actor's UPCGComponent (UPCGComponent.generate_local(
        force)) — the non-replicated, delayed generation path. NON-LEDGERED runtime op (inverse
        pcg_cleanup_local). The LEVEL is NOT saved. PARTIAL: the local/partitioned dispatch is only
        meaningful for a partitioned component (needs a saved World-Partition level); the call is
        reachable and does not error in a transient level, but has no partition to dispatch to.

        actor / force: as pcg_generate_component."""
        try:
            return json.dumps(_exec(_GENERATE_LOCAL_BODY, {"actor": actor, "force": force}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 6. pcg_cleanup_local — CleanupLocal (NON-LEDGERED, PARTIAL)           #
    # ------------------------------------------------------------------ #
    _CLEANUP_LOCAL_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
remove_components = bool(PARAMS.get("remove_components"))
comp, actor, err = _resolve_comp_or_err(ident)
if err is not None:
    print("@@UMCP@@" + json.dumps(err))
else:
    try:
        comp.cleanup_local(remove_components)
    except Exception as _e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "cleanup_local failed: %s" % _e}))
        comp = None
    if comp is not None:
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": _actor_ident(actor),
            "component": comp.get_name(), "remove_components": remove_components,
            "partial_note": "CleanupLocal is the local (non-replicated, delayed) cleanup path; meaningful for a partitioned component (needs a saved World-Partition level). Reachable and non-erroring in a transient level.",
            "ledgered": False, "natural_inverse": "pcg_generate_local"}, default=str))
'''

    @mcp.tool()
    def pcg_cleanup_local(ctx, actor: str, remove_components: bool = True) -> str:
        """Clean up a level actor's UPCGComponent from a LOCAL standpoint (UPCGComponent.cleanup_local(
        remove_components)) — the non-replicated, delayed cleanup path. NON-LEDGERED runtime op
        (inverse pcg_generate_local). The LEVEL is NOT saved. PARTIAL: meaningful for a partitioned
        component (needs a saved World-Partition level); reachable/non-erroring in a transient level.

        actor / remove_components: as pcg_cleanup_component."""
        try:
            return json.dumps(_exec(_CLEANUP_LOCAL_BODY,
                {"actor": actor, "remove_components": remove_components}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 7. pcg_get_generated_output — read generated data summary (READ)      #
    # ------------------------------------------------------------------ #
    _GET_OUTPUT_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
comp, actor, err = _resolve_comp_or_err(ident)
if err is not None:
    print("@@UMCP@@" + json.dumps(err))
else:
    res = {"status": "success", "actor": _actor_ident(actor), "component": comp.get_name()}
    try:
        res["generated"] = bool(comp.get_editor_property("generated"))
    except Exception:
        res["generated"] = None
    res["output"] = _output_summary(comp)
    print("@@UMCP@@" + json.dumps(res, default=str))
'''

    @mcp.tool()
    def pcg_get_generated_output(ctx, actor: str) -> str:
        """Read a summary of a level actor's UPCGComponent generated output
        (UPCGComponent.get_generated_graph_output() -> PCGDataCollection). Read-only, no ledger.

        Returns the tagged_data count and per-entry {data_class, tags} (capped at 25), plus the
        component's 'generated' flag. Empty when nothing has been generated (an empty graph, or
        generation still pending — in-editor generation is async).

        actor: actor identity (label / name / path name)."""
        try:
            return json.dumps(_exec(_GET_OUTPUT_BODY, {"actor": actor}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 8. pcg_clear_pcg_link — ClearPCGLink (NON-LEDGERED)                   #
    # ------------------------------------------------------------------ #
    _CLEAR_LINK_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
comp, actor, err = _resolve_comp_or_err(ident)
if err is not None:
    print("@@UMCP@@" + json.dumps(err))
else:
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    before = set()
    for a in _all_actors():
        try:
            before.add(a.get_path_name())
        except Exception:
            pass
    newact = None
    try:
        newact = comp.clear_pcg_link(None)
    except Exception as _e:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "clear_pcg_link failed: %s" % _e}))
        comp = None
    if comp is not None:
        spawned = []
        for a in _all_actors():
            try:
                pn = a.get_path_name()
                if pn not in before:
                    spawned.append(pn)
            except Exception:
                pass
        rp = None
        if isinstance(newact, unreal.Actor):
            try:
                rp = newact.get_path_name()
            except Exception:
                rp = "<actor>"
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": _actor_ident(actor),
            "component": comp.get_name(), "returned_actor": rp, "spawned_actors": spawned,
            "note": "ClearPCGLink moves any generated resources under a NEW actor (returned) and unlinks them from this component; returns None and spawns nothing when there is nothing generated. Idempotent when clean.",
            "ledgered": False}, default=str))
'''

    @mcp.tool()
    def pcg_clear_pcg_link(ctx, actor: str) -> str:
        """Clear the PCG link on a level actor's UPCGComponent (UPCGComponent.clear_pcg_link) — moves
        any generated resources under a NEW standalone actor and unlinks them from the component.
        NON-LEDGERED / idempotent: when the component has nothing generated it returns None and
        spawns no actor. The LEVEL is NOT saved.

        actor: actor identity (label / name / path name).

        Reports 'returned_actor' and any 'spawned_actors' (path names) so the caller can manage the
        newly created actor. Callers on the transient level should destroy the returned actor when
        done to keep the level clean."""
        try:
            return json.dumps(_exec(_CLEAR_LINK_BODY, {"actor": actor}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 9. pcg_is_partitioned — read is_component_partitioned (READ)          #
    # ------------------------------------------------------------------ #
    _IS_PART_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
comp, actor, err = _resolve_comp_or_err(ident)
if err is not None:
    print("@@UMCP@@" + json.dumps(err))
else:
    val = None; e2 = None
    try:
        val = bool(comp.get_editor_property("is_component_partitioned"))
    except Exception as _e:
        e2 = str(_e)
    print("@@UMCP@@" + json.dumps({"status": "success", "actor": _actor_ident(actor),
        "component": comp.get_name(), "is_component_partitioned": val,
        "read_error": e2,
        "note": "typically False outside a saved World-Partition level; True dispatches generation to grid-local components."}, default=str))
'''

    @mcp.tool()
    def pcg_is_partitioned(ctx, actor: str) -> str:
        """Read the bIsComponentPartitioned flag of a level actor's UPCGComponent (read via
        get_editor_property('is_component_partitioned')). Read-only, no ledger. Typically False in a
        transient (non-World-Partition) level — still a valid read.

        actor: actor identity (label / name / path name)."""
        try:
            return json.dumps(_exec(_IS_PART_BODY, {"actor": actor}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 10. pcg_flush_cache — PCGBlueprintHelpers.FlushPCGCache (NON-LEDGERED) #
    # ------------------------------------------------------------------ #
    _FLUSH_BODY = _PCGC_HELPERS + r'''
H = getattr(unreal, "PCGBlueprintHelpers", None)
if H is None or not hasattr(H, "flush_pcg_cache"):
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "PCGBlueprintHelpers.flush_pcg_cache not present"}))
else:
    ok = None; e2 = None
    try:
        ok = bool(H.flush_pcg_cache())
    except Exception as _e:
        e2 = str(_e)
    if e2 is not None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "flush_pcg_cache failed: %s" % e2}))
    else:
        print("@@UMCP@@" + json.dumps({"status": "success", "flushed": ok,
            "note": "global PCG cache flush (== pcg.FlushCache); no target component. NON-LEDGERED (a cache flush has no meaningful inverse).",
            "ledgered": False}, default=str))
'''

    @mcp.tool()
    def pcg_flush_cache(ctx) -> str:
        """Flush the global PCG cache (PCGBlueprintHelpers.flush_pcg_cache() -> bool; equivalent to the
        `pcg.FlushCache` console command). NON-LEDGERED global op — a cache flush has no meaningful
        inverse. Returns whether the flush succeeded. No level save."""
        try:
            return json.dumps(_exec(_FLUSH_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 11. pcg_refresh_runtime_component — refresh a component (NON-LEDGERED) #
    # ------------------------------------------------------------------ #
    _REFRESH_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
flush_cache = bool(PARAMS.get("flush_cache"))
comp, actor, err = _resolve_comp_or_err(ident)
if err is not None:
    print("@@UMCP@@" + json.dumps(err))
else:
    via = None; e2 = None
    H = getattr(unreal, "PCGBlueprintHelpers", None)
    # Prefer the static helper (takes the component); fall back to the component method.
    if H is not None and hasattr(H, "refresh_pcg_runtime_component"):
        try:
            H.refresh_pcg_runtime_component(comp, flush_cache); via = "PCGBlueprintHelpers.refresh_pcg_runtime_component"
        except Exception as _e:
            e2 = str(_e)
    if via is None:
        try:
            comp.refresh_pcg_runtime_component(flush_cache); via = "UPCGComponent.refresh_pcg_runtime_component"
        except Exception as _e:
            e2 = str(_e)
    if via is None:
        print("@@UMCP@@" + json.dumps({"status": "error", "message": "refresh failed: %s" % e2}))
    else:
        print("@@UMCP@@" + json.dumps({"status": "success", "actor": _actor_ident(actor),
            "component": comp.get_name(), "flush_cache": flush_cache, "via": via,
            "note": "refreshes a component set to Generate-At-Runtime when parameters changed; can also flush the cache. NON-LEDGERED (a notify/refresh has no inverse).",
            "ledgered": False}, default=str))
'''

    @mcp.tool()
    def pcg_refresh_runtime_component(ctx, actor: str, flush_cache: bool = False) -> str:
        """Refresh a level actor's runtime UPCGComponent (PCGBlueprintHelpers.refresh_pcg_runtime_
        component(component, flush_cache), falling back to the component method). Used to re-evaluate
        a Generate-At-Runtime component after parameters changed; optionally also flushes the cache.
        NON-LEDGERED (a refresh/notify has no inverse). No level save.

        actor:       actor identity (label / name / path name).
        flush_cache: also flush the PCG cache as part of the refresh."""
        try:
            return json.dumps(_exec(_REFRESH_BODY,
                {"actor": actor, "flush_cache": flush_cache}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 12. pcg_generate_all_components — iterate the level (NON-LEDGERED)    #
    # ------------------------------------------------------------------ #
    _GEN_ALL_BODY = _PCGC_HELPERS + r'''
force = bool(PARAMS.get("force"))
ok, wp = _world_ok()
if not ok:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "editor world is not a transient /Temp/ level (got %s)" % wp}))
else:
    results = []
    seen = set()
    for a in _all_actors():
        try:
            comps = a.get_components_by_class(unreal.PCGComponent)
        except Exception:
            comps = None
        for c in (list(comps) if comps else []):
            try:
                key = c.get_path_name()
            except Exception:
                key = None
            if key in seen:
                continue
            seen.add(key)
            rec = {"actor": a.get_actor_label(), "actor_name": a.get_name(), "component": c.get_name()}
            try:
                c.generate(force); rec["ok"] = True
            except Exception as _e:
                rec["ok"] = False; rec["error"] = str(_e)[:120]
            results.append(rec)
    print("@@UMCP@@" + json.dumps({"status": "success", "count": len(results), "components": results,
        "force": force,
        "note": "emulated: iterates every UPCGComponent found on level actors and calls generate() on each. In-editor generation is async. NON-LEDGERED (inverse: pcg_cleanup_all_components).",
        "ledgered": False}, default=str))
'''

    @mcp.tool()
    def pcg_generate_all_components(ctx, force: bool = False) -> str:
        """Generate EVERY UPCGComponent in the current level (emulated: enumerate all level actors,
        call generate(force) on each component found). NON-LEDGERED runtime op — inverse is
        pcg_cleanup_all_components. No level save.

        force: force regeneration on each. Returns a per-component ok/error list.

        NOTE: in-editor generation is async; keep level fixtures small (this iterates whatever PCG
        components exist)."""
        try:
            return json.dumps(_exec(_GEN_ALL_BODY, {"force": force}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 13. pcg_cleanup_all_components — iterate the level (NON-LEDGERED)     #
    # ------------------------------------------------------------------ #
    _CLEAN_ALL_BODY = _PCGC_HELPERS + r'''
remove_components = bool(PARAMS.get("remove_components"))
ok, wp = _world_ok()
if not ok:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "editor world is not a transient /Temp/ level (got %s)" % wp}))
else:
    results = []
    seen = set()
    for a in _all_actors():
        try:
            comps = a.get_components_by_class(unreal.PCGComponent)
        except Exception:
            comps = None
        for c in (list(comps) if comps else []):
            try:
                key = c.get_path_name()
            except Exception:
                key = None
            if key in seen:
                continue
            seen.add(key)
            rec = {"actor": a.get_actor_label(), "actor_name": a.get_name(), "component": c.get_name()}
            try:
                c.cleanup(remove_components); rec["ok"] = True
            except Exception as _e:
                rec["ok"] = False; rec["error"] = str(_e)[:120]
            results.append(rec)
    print("@@UMCP@@" + json.dumps({"status": "success", "count": len(results), "components": results,
        "remove_components": remove_components,
        "note": "emulated: iterates every UPCGComponent found on level actors and calls cleanup() on each. NON-LEDGERED (inverse: pcg_generate_all_components).",
        "ledgered": False}, default=str))
'''

    @mcp.tool()
    def pcg_cleanup_all_components(ctx, remove_components: bool = True) -> str:
        """Clean up EVERY UPCGComponent in the current level (emulated: enumerate all level actors,
        call cleanup(remove_components) on each component found). NON-LEDGERED runtime op — inverse is
        pcg_generate_all_components. No level save.

        remove_components: also remove the managed components each generation spawned. Returns a
        per-component ok/error list."""
        try:
            return json.dumps(_exec(_CLEAN_ALL_BODY, {"remove_components": remove_components}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 14. pcg_list_partition_actors — enumerate APCGPartitionActor (READ)   #
    # ------------------------------------------------------------------ #
    _LIST_PA_BODY = _PCGC_HELPERS + r'''
ok, wp = _world_ok()
if not ok:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "editor world is not a transient /Temp/ level (got %s)" % wp}))
else:
    PA = getattr(unreal, "PCGPartitionActor", None)
    out = []
    if PA is not None:
        for a in _all_actors():
            try:
                if isinstance(a, PA):
                    rec = {"actor": a.get_actor_label(), "actor_name": a.get_name(),
                           "actor_path": a.get_path_name()}
                    try:
                        rec["grid_size"] = a.bp_get_pcg_grid_size()
                    except Exception:
                        rec["grid_size"] = None
                    out.append(rec)
            except Exception:
                pass
    print("@@UMCP@@" + json.dumps({"status": "success", "count": len(out), "partition_actors": out,
        "has_class": PA is not None,
        "note": "APCGPartitionActor instances only exist in a SAVED World-Partition level with partitioned PCG components; a transient level has none (count 0 is expected/honest)."}, default=str))
'''

    @mcp.tool()
    def pcg_list_partition_actors(ctx) -> str:
        """Enumerate APCGPartitionActor instances in the current level. Read-only, no ledger. Each
        entry: {actor, actor_name, actor_path, grid_size}. Partition actors only exist in a SAVED
        World-Partition level with partitioned PCG components — expect count 0 (reported honestly) in
        a transient level."""
        try:
            return json.dumps(_exec(_LIST_PA_BODY, {}), indent=2)
        except Exception as e:
            return f"Error: {e}"

    # ------------------------------------------------------------------ #
    # 15. pcg_get_partition_actor_info — read a partition actor (READ)      #
    # ------------------------------------------------------------------ #
    _GET_PA_BODY = _PCGC_HELPERS + r'''
ident = PARAMS.get("actor")
ok, wp = _world_ok()
if not ok:
    print("@@UMCP@@" + json.dumps({"status": "error", "message": "editor world is not a transient /Temp/ level (got %s)" % wp}))
else:
    PA = getattr(unreal, "PCGPartitionActor", None)
    if PA is None:
        print("@@UMCP@@" + json.dumps({"status": "blocked", "message": "unreal.PCGPartitionActor not bound on this build"}))
    else:
        actor = _resolve_actor(ident)
        if actor is None:
            # honest report: none exist in a transient level
            any_pa = [a for a in _all_actors() if isinstance(a, PA)]
            print("@@UMCP@@" + json.dumps({"status": "blocked",
                "message": "partition actor not found: %s" % ident,
                "partition_actors_in_level": len(any_pa),
                "reason": "APCGPartitionActor instances require a saved World-Partition level with partitioned PCG components; none exist in a transient level (BLOCKED here by environment, not by API reachability)."}))
        elif not isinstance(actor, PA):
            print("@@UMCP@@" + json.dumps({"status": "error",
                "message": "actor is not an APCGPartitionActor: %s (class=%s)" % (ident, actor.get_class().get_name())}))
        else:
            rec = {"status": "success", "actor": _actor_ident(actor)}
            try:
                rec["grid_size"] = actor.bp_get_pcg_grid_size()
            except Exception as _e:
                rec["grid_size_err"] = str(_e)[:80]
            try:
                rg = actor.get_editor_property("runtime_grid")
                rec["runtime_grid"] = str(rg)
            except Exception:
                rec["runtime_grid"] = None
            print("@@UMCP@@" + json.dumps(rec, default=str))
'''

    @mcp.tool()
    def pcg_get_partition_actor_info(ctx, actor: str) -> str:
        """Read an APCGPartitionActor: its grid size (bp_get_pcg_grid_size) and runtime_grid.
        Read-only, no ledger.

        actor: partition-actor identity (label / name / path name).

        PARTIAL/BLOCKED by environment: APCGPartitionActor instances only exist in a SAVED
        World-Partition level with partitioned PCG components. In a transient level there are none, so
        this returns status 'blocked' with an honest reason (the API is reachable; the environment
        lacks a partition actor to read)."""
        try:
            return json.dumps(_exec(_GET_PA_BODY, {"actor": actor}), indent=2)
        except Exception as e:
            return f"Error: {e}"
