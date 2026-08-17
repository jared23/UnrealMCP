# UnrealMCP — Feature-Parity Ledger (vs. `docs/spec/`)

**Audited 2026-08-16** by 6 read-only agents against every feature named in `docs/spec/*.md`
(the extracted spec — our checklist of "every tool feature we want"). Per-cluster detail with
per-feature checkboxes lives in `p1_*`–`p6_*.md`. This file is the roll-up.

## The honest headline
Against **~902 spec'd features**, we currently have:

| Status | Count | % |
|---|---:|---:|
| ✅ DONE (implemented, incl. parameterized-folds) | **302** | 33% |
| ⚠️ PARTIAL (capability present, sub-features missing) | 56 | 6% |
| ⛔ BLOCKED (not exposed to stock Python / no asset to validate) | 42 | 5% |
| ❓ UNCERTAIN (needs live probe) | 3 | <1% |
| ❌ MISSING (no equivalent built) | **499** | 55% |
| **TOTAL** | **902** | |

> **Progress since audit** — Wave G1 (2026-08-17): **+22 DONE.** Sequencer editing layer (+12: key/channel edit, section/track props) and Control-Rig controls+hierarchy depth (+10: control value/offset/shape, animation channels, sockets, curves, reparent, rename, batch build). All reversible + verified through the real `undo`. Deferred (no faithful inverse w/o export_text): sequencer `remove_section`/`remove_track`.
>
> Wave G2 (2026-08-17): **+24** — Curves 3→13/13 (**complete**), Enhanced Input 6→20/21 (`list_input_keys` full-registry deferred: no `EKeys` Python binding). Input undo VERIFIED faithful; curves undo verified except an empty-table `set_curve_table` case now FIXED in editor_level (re-verify pending editor). Wave G3-A: EQS 2→4/11 (create_env_query + validate); the 5 option/test/node-property authoring tools are **BLOCKED** — Options/Generator/Tests are protected C++ UPROPERTYs refused by Python AND the C++ read-only reflection lib (would need a dedicated C++ writer);  `run_env_query` needs PIE.
>
> Waves G4-G7 + C++ (2026-08-17): **G4** GAS 8→19 (attribute-sets/effects/abilities/cues via BP-subclass+CDO). **G5** sequencer 29→47 (bindings/playback/folders/marked-frames/subsequence; 5 deferred not-reachable). **G6** anim 7→24 (BlendSpaces, montage slots/segments, AnimBP asset+validate; AnimGraph state-machines BLOCKED=C++). **C++ #15** EQS writer GREEN (add/set live; remove/undo quarantined pending #17 reader-guard for crash mode #3). **C++ #16** GAS tags 19→21 (rename_gameplay_tag + add_gameplay_tag_source). **G7** BehaviorTree completion in flight. WORKFLOW: Windows now authors C++ (has engine source); StateTree found PYTHON-reachable (grind, no C++); AnimBP state-machines staged as own C++ round.

> Note on the "369 commands validated live" count: that counts *tools shipped* (incl. foundation
> tools, folded readers, and per-op undo handlers), not distinct spec features. Measured against the
> feature spec, those 369 tools deliver ~197 full + ~58 partial features. Both numbers are true; they
> measure different things. The earlier "coverage essentially complete" claim was **wrong** — it was
> true for *read coverage across categories*, not for the *feature spec*.

## Per-category (DONE / PARTIAL / BLOCKED / UNCERTAIN / MISSING / TOTAL)

| Category | ✅ | ⚠️ | ⛔ | ❓ | ❌ | Total | % done |
|---|---:|---:|---:|---:|---:|---:|---:|
| editor | 35 | 2 | 0 | 0 | 17 | 54 | 65% |
| datatables | 9 | 0 | 0 | 0 | 1 | 10 | 90% |
| objects | 3 | 0 | 0 | 0 | 0 | 3 | 100% |
| structs | 5 | 0 | 0 | 0 | 1 | 6 | 83% |
| assets | 10 | 0 | 0 | 0 | 6 | 16 | 63% |
| sequencer | 47 | 8 | 7 | 0 | 2 | 64 | 73% |
| niagara | 16 | 2 | 0 | 0 | 34 | 52 | 31% |
| control rig | 25 | 1 | 0 | 0 | 45 | 71 | 35% |
| blueprints | 11 | 8 | 0 | 0 | 36 | 55 | 20% |
| statetree | 10 | 4 | 0 | 0 | 28 | 42 | 24% |
| materials | 9 | 2 | 0 | 0 | 40 | 51 | 18% |
| gas | 21 | 0 | 1 | 0 | 3 | 25 | 84% |
| behavior trees | 17 | 3 | 3 | 0 | 1 | 24 | 71% |
| anim | 24 | 2 | 16 | 1 | 2 | 45 | 53% |
| input | 20 | 1 | 0 | 0 | 0 | 21 | 95% |
| world | 6 | 3 | 0 | 0 | 46 | 55 | 11% |
| pcg | 6 | 2 | 0 | 0 | 87 | 95 | 6% |
| curves | 13 | 0 | 0 | 0 | 0 | 13 | 100% |
| widgets | 3 | 3 | 0 | 1 | 45 | 52 | 6% |
| eqs | 4 | 1 | 6 | 0 | 0 | 11 | 36% |
| profiling | 2 | 0 | 0 | 0 | 9 | 11 | 18% |
| dataassets | 2 | 1 | 0 | 1 | 1 | 5 | 40% |
| console | 1 | 2 | 0 | 0 | 1 | 4 | 25% |
| core | 1 | 0 | 0 | 0 | 6 | 7 | 14% |
| projectsettings | 1 | 0 | 0 | 0 | 2 | 3 | 33% |
| mutable | 1 | 6 | 9 | 0 | 20 | 36 | 3% |
| audio | 0 | 5 | 0 | 0 | 35 | 40 | 0% |
| asset_ops | 0 | 0 | 0 | 0 | 5 | 5 | 0% |
| debug | 0 | 0 | 0 | 0 | 17 | 17 | 0% |
| procedural vegetation | 0 | 0 | 0 | 0 | 9 | 9 | 0% |
| **TOTAL** | **302** | **56** | **42** | **3** | **499** | **902** | **33%** |

## What the 618 "missing" actually break down into (feasibility tiers)

**Tier 1 — buildable now via our proven no-build Python batch loop** (the real near-term backlog):
sequencer fine-grained keys/sections/bindings/playback; curve tables + atlas + key-delete/import;
input triggers/modifiers; StateTree/EQS/GAS/BT authoring writes; material-instance param management;
widget layout/slot/animation; blueprint variable/function *details* + class-defaults; asset_ops bulk
(move/delete/fixup-redirectors/replace-refs); projectsettings write; datatable/dataasset create.

**Tier 2 — needs C++ editor-graph authoring** (our sync → Windows-build loop; bigger lifts):
Blueprint graph-node authoring (large), AnimBP state machines, MetaSound (entire category), deeper
Material / Niagara / PCG graph authoring, Control-Rig functions + modular rigs, Sound Cue graph editing.

**Tier 3 — genuinely blocked / low-ROI / not stock-feasible** (park unless specifically wanted):
Movie Render Queue (`render_sequence`), BP/BT visual debugger (17), UE Insights `.utrace` suite (9),
landscape sculpt/paint, Mutable authoring (0 CustomizableObject assets in project), multi-editor infra,
procedural-vegetation (newer-engine feature, no code).

## Recommended priority (given the project's locomotion / RL / animation focus)
1. **Control Rig depth** (21% → high) — rig creation, control shapes/values/channels, RigVM functions.
   Most relevant to the user's work; mostly Tier-1/Tier-2 on the RigVM APIs we already drive.
2. **Animation + Sequencer depth** — anim curves/notify completeness + BlendSpace; sequencer keys/tracks.
3. **Broad Tier-1 sweep** — knock out the cheap high-count wins (sequencer, input, curves, GAS/EQS/BT/ST writes).
4. **Consciously defer Tier-3** and record it, so the count never pretends to cover it.
