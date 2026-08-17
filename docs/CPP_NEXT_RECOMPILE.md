# CPP_NEXT_RECOMPILE — C++ #15 (EQS writer) + C++ #16 (GAS tag ops)

**Date:** 2026-08-17. Two clean-room handler batches spliced into `Source/UnrealMCP/{Public,Private}/MCPReflectionLibrary.{h,cpp}`.
**Build.cs:** NO CHANGE (both confirmed). **New #includes:** NONE (both confirmed). **Export-macro/engine-source patch:** NONE expected.
Mac authored + reviewed + spliced; NOT compiled (no engine headers on Mac). Build on Windows (the build machine), then sync back a GREEN/RED report + any corrected `.cpp`.

## What was added (7 static handlers on UMCPReflectionLibrary)

### C++ #15 — EQS authoring WRITER (5 handlers) — inverse of `GetEnvQueryConfigJson`
`AddEnvQueryOption`, `RemoveEnvQueryOption`, `AddEnvQueryTest`, `RemoveEnvQueryTest`, `SetEnvQueryNodeProperty`.
Writes the PROTECTED EQS structure (`UEnvQuery::Options` / `UEnvQueryOption::Generator`+`Tests` / node config FProperties)
via the SAME FProperty-reflection idiom as the reader + the C++ #13 socket writer (`FindFProperty` / `FArrayProperty` +
`FScriptArrayHelper` / `FObjectPropertyBase`; `Modify`→`PreEditChange`→append→`PostEditChange`→`MarkPackageDirty`).
Fully reflective — element UClasses come from each property's `PropertyClass`, so no `UEnvQueryOption/Generator/Test`
include and no new module. `AIModule` already linked (C++ #11); `EnvironmentQuery/EnvQuery.h` already included.

### C++ #16 — GAS gameplay-tag ops (2 handlers) — same INI path as C++ #6
`RenameGameplayTag` (via `IGameplayTagsEditorModule::RenameTagInINI` → writes a redirector),
`AddGameplayTagSource` (via `IGameplayTagsEditorModule::AddNewGameplayTagSource`). `GameplayTags` + `GameplayTagsEditor`
already linked (C++ #6); `GameplayTagsManager.h` + `GameplayTagsEditorModule.h` already included.

## VERIFY items (search `// VERIFY vs engine source` in the added code)

### #15 (medium risk — confirm first)
- **Class resolution (EqsResolveClass):** `UClass::TryFindTypeSlow<UClass>(const FString&)` (bare names) and
  `LoadObject<UClass>(nullptr, "/Script/AIModule.<Class>")` (native paths). Fallbacks `FindFirstObject<UClass>` /
  `FindObject<UClass>(nullptr,path)` are noted in-code — swap if the primary doesn't resolve on 5.8.
- **Cross-namespace helper call:** `SetEnvQueryNodeProperty` calls `EqsPropertyToJson(...)` (the C++ #12 reader helper,
  defined in an earlier anonymous namespace in the SAME .cpp). Should resolve (same TU, earlier definition). If the
  build complains it's not visible, either move it to a shared file-scope static or duplicate the tiny helper into #15's
  anon namespace.
- Property names `UEnvQuery::Options` / `UEnvQueryOption::Generator` / `UEnvQueryOption::Tests` — the reader already
  depends on these, so low risk, but confirm no 5.8 rename.
- `FProperty::ImportText_Direct(const TCHAR*, void*, UObject*, int32, FOutputDevice*)`, `FPropertyChangedEvent(FProperty*)`,
  `UObject::PreEditChange(FProperty*)` overload — low risk.

### #16 (low risk — mirrors C++ #6, but signatures are version-sensitive)
- `IGameplayTagsEditorModule::Get().RenameTagInINI(const FString&, const FString&) -> bool` — confirm signature/return
  (may take extra optional args or return void).
- `IGameplayTagsEditorModule::Get().AddNewGameplayTagSource(const FString&, const FString& RootDir=FString()) -> bool` —
  confirm overload. Documented fallback: `UGameplayTagsManager::Get().FindOrAddTagSource(FName, EGameplayTagSourceType::TagList)`.
- `UGameplayTagsManager::FindTagSource(FName) -> const FGameplayTagSource*` — best-effort telemetry only; drop the two
  `registered` lines if not public in 5.8 (handler still works).

## Build steps (Windows)
1. Replace `Source/UnrealMCP/Public/MCPReflectionLibrary.h` and `Source/UnrealMCP/Private/MCPReflectionLibrary.cpp` with the synced copies.
2. Build the `UnrealMCP` module (editor target). No Build.cs regen expected.
3. If GREEN: sync back a short GREEN note. If RED: apply the VERIFY-item fixes above, sync back the corrected `.cpp` + a note of what changed (coordinator will diff + adopt, same as C++ #12–#14).
4. After GREEN + reload, the coordinator wires Python (eqs_write.py's 5 `_blocked` refusals → real calls; gas_write.py's 2 `_blocked` refusals → real calls), folds undo ops, and verifies live.

## Python wiring (coordinator does after GREEN)
- eqs_write.py: `add_eqs_option`/`remove_eqs_option`/`add_eqs_test`/`remove_eqs_test`/`set_eqs_node_property` → hasattr-guarded calls; ledger inverses (add↔remove; set uses returned `prev`).
- gas_write.py: `rename_gameplay_tag` (ledger self-inverse rename(new,old)) / `add_gameplay_tag_source` (UNledgered — no engine "remove source" API).
