"""Item 2: collapse verb-family tools into per-domain enum routers.

Design: for each domain, register 7 CRUD routers ({domain}_{get,set,add,list,create,
remove,delete}) plus one {domain}_action catch-all for non-CRUD verbs. Each router takes
`entity` (which underlying tool) + `params` (that tool's args, as an object) and dispatches
to the ORIGINAL captured function -- no tool body is rewritten. The router description
carries the entity catalog (name + one-line summary) so a caller picks `entity` without a
second lookup; entity='?' lists entities, params={'describe': true} returns one entity's
arg schema.

Enabled by env UNREALMCP_ROUTERS (see the bridge). Backward compatible: off -> individual tools.
"""
import inspect
import json

CRUD = ("get", "set", "add", "list", "create", "remove", "delete")


def _entity_and_router(name):
    """(router_verb, entity) for a tool name.
    CRUD verb -> ('{verb}', name-without-verb); else -> ('action', full name)."""
    verb = name.split("_", 1)[0]
    if verb in CRUD and "_" in name:
        return verb, name.split("_", 1)[1]
    return "action", name


def build_router_groups(captured, resolve_domain, requested):
    """captured: list of (fn, name, description). -> {(domain, rverb): {entity: rec}}."""
    groups = {}
    for fn, name, description in captured:
        name = name or fn.__name__  # bare @mcp.tool() -> name is None; real name is fn.__name__
        co = getattr(fn, "__code__", None)
        src = co.co_filename if co is not None else ""
        try:
            domain = resolve_domain(src, name)
        except Exception:
            domain = "misc"
        if requested is not None and domain not in requested:
            continue
        rverb, entity = _entity_and_router(name)
        summary = (description or (fn.__doc__ or "")).strip().split("\n", 1)[0][:140]
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            sig = None
        groups.setdefault((domain, rverb), {})[entity] = {
            "fn": fn, "name": name, "summary": summary, "sig": sig,
        }
    return groups


def _param_schema(sig):
    out = []
    if sig is None:
        return out
    for i, p in enumerate(sig.parameters.values()):
        if i == 0 and p.name in ("ctx", "context"):
            continue  # drop the leading ctx convention
        if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        out.append({
            "name": p.name,
            "required": p.default is inspect._empty,
            "default": None if p.default is inspect._empty else repr(p.default),
        })
    return out


def make_router(domain, rverb, entries):
    """Return (router_fn, description). entries: {entity: rec}."""
    entity_list = sorted(entries)
    header = (f"{domain}_{rverb} router: choose `entity`, pass that tool's arguments in "
              f"`params` (an object). entity='?' lists entities; add params={{\"__describe__\": true}} "
              f"to get one entity's argument schema before calling.")
    catalog = "\n".join(f"  {e} — {entries[e]['summary']}" for e in entity_list)
    description = header + "\nEntities:\n" + catalog

    def router(entity: str = None, params: dict = None) -> str:
        p = dict(params) if isinstance(params, dict) else {}
        if entity in (None, "", "?", "list", "__list__"):
            return json.dumps({"router": f"{domain}_{rverb}", "entities": entity_list}, indent=2)
        rec = entries.get(entity)
        if rec is None:
            return json.dumps({"error": f"unknown entity '{entity}' for {domain}_{rverb}",
                               "valid_entities": entity_list}, indent=2)
        if p.pop("__describe__", False) is True:
            return json.dumps({"entity": entity, "maps_to": rec["name"],
                               "summary": rec["summary"], "params": _param_schema(rec["sig"])}, indent=2)
        return rec["fn"](None, **p)

    router.__name__ = f"{domain}_{rverb}"
    router.__doc__ = description
    return router, description
