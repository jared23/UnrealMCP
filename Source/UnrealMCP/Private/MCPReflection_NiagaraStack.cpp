// UnrealMCP — NIAGARA STACK-ISSUE list + auto-fix (C++ #50, 2026-08-19/20).
//
// fix_niagara_stack_issue: the last-deferred Niagara verb. Niagara "stack issues" are FStackIssue
// objects on UNiagaraStackEntry entries of the editor's UNiagaraStackViewModel(s). Each issue may carry
// FStackIssueFixes whose FStackIssueFixDelegate (a simple DECLARE_DELEGATE) applies the fix -- the same
// thing the "Fix issue" button does. Reached via TObjectIterator once the caller opens the system editor
// (asset editors open headless -- proven by the CR-preview work). We do NOT build a FNiagaraSystemViewModel
// ourselves (its Cleanup() is not NIAGARAEDITOR_API-exported -> unsafe to own the lifecycle); the editor
// owns it. After applying fixes we RefreshChildren() the stack roots (recomputes issues synchronously) and
// report the before/after issue counts so the fix is self-verifying.
//
// Handlers (block #50): GetNiagaraStackIssuesJson(SystemPath) [READ] and
// FixNiagaraStackIssueJson(SystemPath, IssueIdentifier, FixIndex, bFixAll) [repair op -> NON-LEDGERED;
// a fix delegate does arbitrary graph surgery, not generically invertible].
// LINKAGE: Niagara + NiagaraEditor (already deps, #5); all API is NIAGARAEDITOR_API. #if WITH_EDITOR.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "CoreMinimal.h"
#include "UObject/UObjectIterator.h"
#include "UObject/UObjectGlobals.h"

#include "NiagaraSystem.h"
#include "ViewModels/NiagaraSystemViewModel.h"
#include "ViewModels/Stack/NiagaraStackViewModel.h"
#include "ViewModels/Stack/NiagaraStackEntry.h"

namespace
{
    FString MCPNStk_Serialize(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPNStk_Err(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("error"), Message);
        return MCPNStk_Serialize(Root);
    }

#if WITH_EDITOR
    UNiagaraSystem* MCPNStk_LoadSystem(const FString& Path)
    {
        if (Path.IsEmpty()) { return nullptr; }
        return LoadObject<UNiagaraSystem>(nullptr, *Path);
    }

    FString MCPNStk_SeverityStr(EStackIssueSeverity S)
    {
        switch (S)
        {
        case EStackIssueSeverity::Error:   return TEXT("error");
        case EStackIssueSeverity::Warning: return TEXT("warning");
        case EStackIssueSeverity::Info:    return TEXT("info");
        default: return TEXT("none");
        }
    }

    // BFS-collect an entry and all its unfiltered descendants.
    void MCPNStk_CollectEntries(UNiagaraStackEntry* Root, TArray<UNiagaraStackEntry*>& Out)
    {
        if (!Root) { return; }
        TArray<UNiagaraStackEntry*> Work;
        Work.Add(Root);
        while (Work.Num() > 0)
        {
            UNiagaraStackEntry* E = Work.Pop();
            if (!E || !IsValid(E) || Out.Contains(E)) { continue; }
            Out.Add(E);
            TArray<UNiagaraStackEntry*> Kids;
            E->GetUnfilteredChildren(Kids);
            Work.Append(Kids);
        }
    }

    // Every stack view-model root whose system matches (system stack + per-emitter stacks).
    void MCPNStk_CollectSystemRoots(UNiagaraSystem* System, TArray<UNiagaraStackEntry*>& OutRoots, int32& OutVMCount)
    {
        OutVMCount = 0;
        for (TObjectIterator<UNiagaraStackViewModel> It; It; ++It)
        {
            UNiagaraStackViewModel* VM = *It;
            if (!VM || !IsValid(VM)) { continue; }
            UNiagaraStackEntry* Root = VM->GetRootEntry();
            if (!Root) { continue; }
            TSharedPtr<FNiagaraSystemViewModel> SysVM = Root->GetSystemViewModelPtr();
            if (!SysVM.IsValid()) { continue; }
            if (&SysVM->GetSystem() != System) { continue; }
            ++OutVMCount;
            OutRoots.AddUnique(Root);
        }
    }

    void MCPNStk_RootsToEntries(const TArray<UNiagaraStackEntry*>& Roots, TArray<UNiagaraStackEntry*>& Out)
    {
        for (UNiagaraStackEntry* R : Roots) { MCPNStk_CollectEntries(R, Out); }
    }

    // Count total + fixable issues across a set of entries.
    void MCPNStk_CountIssues(const TArray<UNiagaraStackEntry*>& Entries, int32& OutTotal, int32& OutFixable)
    {
        OutTotal = 0; OutFixable = 0;
        for (UNiagaraStackEntry* E : Entries)
        {
            if (!E) { continue; }
            for (const UNiagaraStackEntry::FStackIssue& Iss : E->GetIssues())
            {
                ++OutTotal;
                if (Iss.GetFixes().Num() > 0) { ++OutFixable; }
            }
        }
    }
#endif // WITH_EDITOR
}

// =====================================================================================================
// GetNiagaraStackIssuesJson — list all stack issues for an OPEN Niagara system editor.
// =====================================================================================================
FString UMCPReflectionLibrary::GetNiagaraStackIssuesJson(const FString& SystemPath)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNStk_LoadSystem(SystemPath);
    if (!System) { return MCPNStk_Err(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }

    TArray<UNiagaraStackEntry*> Roots;
    int32 VMCount = 0;
    MCPNStk_CollectSystemRoots(System, Roots, VMCount);
    TArray<UNiagaraStackEntry*> Entries;
    MCPNStk_RootsToEntries(Roots, Entries);

    TArray<TSharedPtr<FJsonValue>> IssueArr;
    int32 FixableCount = 0, ErrCount = 0, WarnCount = 0, InfoCount = 0;
    for (UNiagaraStackEntry* E : Entries)
    {
        if (!E) { continue; }
        for (const UNiagaraStackEntry::FStackIssue& Iss : E->GetIssues())
        {
            const EStackIssueSeverity Sev = Iss.GetSeverity();
            if (Sev == EStackIssueSeverity::Error) { ++ErrCount; }
            else if (Sev == EStackIssueSeverity::Warning) { ++WarnCount; }
            else { ++InfoCount; }

            TSharedRef<FJsonObject> IObj = MakeShared<FJsonObject>();
            IObj->SetStringField(TEXT("entry"), E->GetDisplayName().ToString());
            IObj->SetStringField(TEXT("entry_class"), E->GetClass()->GetName());
            IObj->SetStringField(TEXT("entry_key"), E->GetStackEditorDataKey());
            IObj->SetStringField(TEXT("severity"), MCPNStk_SeverityStr(Sev));
            IObj->SetStringField(TEXT("short_description"), Iss.GetShortDescription().ToString());
            IObj->SetStringField(TEXT("long_description"), Iss.GetLongDescription().ToString());
            IObj->SetStringField(TEXT("identifier"), Iss.GetUniqueIdentifier());
            IObj->SetBoolField(TEXT("can_be_dismissed"), Iss.GetCanBeDismissed());

            const TArray<UNiagaraStackEntry::FStackIssueFix>& Fixes = Iss.GetFixes();
            TArray<TSharedPtr<FJsonValue>> FixArr;
            for (const UNiagaraStackEntry::FStackIssueFix& Fx : Fixes)
            {
                TSharedRef<FJsonObject> FObj = MakeShared<FJsonObject>();
                FObj->SetStringField(TEXT("description"), Fx.GetDescription().ToString());
                FObj->SetStringField(TEXT("style"), Fx.GetStyle() == UNiagaraStackEntry::EStackIssueFixStyle::Fix ? TEXT("fix") : TEXT("link"));
                FObj->SetBoolField(TEXT("is_valid"), Fx.IsValid());
                FixArr.Add(MakeShared<FJsonValueObject>(FObj));
            }
            if (Fixes.Num() > 0) { ++FixableCount; }
            IObj->SetNumberField(TEXT("fix_count"), Fixes.Num());
            IObj->SetArrayField(TEXT("fixes"), FixArr);
            IssueArr.Add(MakeShared<FJsonValueObject>(IObj));
        }
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetNumberField(TEXT("stack_view_models"), VMCount);
    Root->SetNumberField(TEXT("entries_scanned"), Entries.Num());
    Root->SetNumberField(TEXT("issue_count"), IssueArr.Num());
    Root->SetNumberField(TEXT("fixable_issue_count"), FixableCount);
    Root->SetNumberField(TEXT("error_count"), ErrCount);
    Root->SetNumberField(TEXT("warning_count"), WarnCount);
    Root->SetNumberField(TEXT("info_count"), InfoCount);
    if (VMCount == 0)
    {
        Root->SetStringField(TEXT("note"), TEXT("no stack view-model found for this system -- open its editor first "
            "(open_editor_for_assets)."));
    }
    Root->SetArrayField(TEXT("issues"), IssueArr);
    return MCPNStk_Serialize(Root);
#else
    return MCPNStk_Err(TEXT("editor-only"));
#endif
}

// =====================================================================================================
// FixNiagaraStackIssueJson — execute the fix delegate(s), refresh, report before/after issue counts.
// =====================================================================================================
FString UMCPReflectionLibrary::FixNiagaraStackIssueJson(const FString& SystemPath, const FString& IssueIdentifier,
    int32 FixIndex, bool bFixAll)
{
#if WITH_EDITOR
    UNiagaraSystem* System = MCPNStk_LoadSystem(SystemPath);
    if (!System) { return MCPNStk_Err(FString::Printf(TEXT("could not load NiagaraSystem '%s'"), *SystemPath)); }

    TArray<UNiagaraStackEntry*> Roots;
    int32 VMCount = 0;
    MCPNStk_CollectSystemRoots(System, Roots, VMCount);
    if (VMCount == 0)
    {
        return MCPNStk_Err(TEXT("no stack view-model found -- open the system editor first (open_editor_for_assets)"));
    }
    TArray<UNiagaraStackEntry*> Entries;
    MCPNStk_RootsToEntries(Roots, Entries);

    int32 IssuesBefore = 0, FixableBefore = 0;
    MCPNStk_CountIssues(Entries, IssuesBefore, FixableBefore);

    TArray<TSharedPtr<FJsonValue>> Applied;
    int32 Attempted = 0, Failed = 0;
    for (UNiagaraStackEntry* E : Entries)
    {
        if (!E) { continue; }
        // Copy the issues (executing a fix can rebuild the entry's issue array under us).
        TArray<UNiagaraStackEntry::FStackIssue> Issues = E->GetIssues();
        for (const UNiagaraStackEntry::FStackIssue& Iss : Issues)
        {
            const bool bMatch = bFixAll || (!IssueIdentifier.IsEmpty() && Iss.GetUniqueIdentifier() == IssueIdentifier);
            if (!bMatch) { continue; }
            const TArray<UNiagaraStackEntry::FStackIssueFix>& Fixes = Iss.GetFixes();
            if (Fixes.Num() == 0) { continue; }

            TArray<int32> Which;
            if (bFixAll)
            {
                for (int32 i = 0; i < Fixes.Num(); ++i)
                {
                    if (Fixes[i].IsValid() && Fixes[i].GetStyle() == UNiagaraStackEntry::EStackIssueFixStyle::Fix) { Which.Add(i); }
                }
            }
            else
            {
                const int32 Idx = (FixIndex >= 0 && FixIndex < Fixes.Num()) ? FixIndex : 0;
                if (Fixes[Idx].IsValid()) { Which.Add(Idx); }
            }

            for (int32 i : Which)
            {
                ++Attempted;
                bool bOk = false;
                try
                {
                    bOk = Fixes[i].GetFixDelegate().ExecuteIfBound();
                }
                catch (...)
                {
                    bOk = false;
                }
                if (!bOk) { ++Failed; }
                TSharedRef<FJsonObject> AObj = MakeShared<FJsonObject>();
                AObj->SetStringField(TEXT("entry"), E->GetDisplayName().ToString());
                AObj->SetStringField(TEXT("identifier"), Iss.GetUniqueIdentifier());
                AObj->SetStringField(TEXT("short_description"), Iss.GetShortDescription().ToString());
                AObj->SetStringField(TEXT("fix_description"), Fixes[i].GetDescription().ToString());
                AObj->SetBoolField(TEXT("executed"), bOk);
                Applied.Add(MakeShared<FJsonValueObject>(AObj));
            }
            if (!bFixAll) { break; }
        }
    }

    // Refresh so the stack recomputes issues, then recount (self-verification).
    int32 IssuesAfter = IssuesBefore, FixableAfter = FixableBefore;
    if (Attempted > 0)
    {
        System->MarkPackageDirty();
        for (UNiagaraStackEntry* R : Roots)
        {
            if (R && IsValid(R)) { R->RefreshChildren(); }
        }
        TArray<UNiagaraStackEntry*> Entries2;
        MCPNStk_RootsToEntries(Roots, Entries2);
        MCPNStk_CountIssues(Entries2, IssuesAfter, FixableAfter);
    }

    TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
    Root->SetStringField(TEXT("status"), TEXT("success"));
    Root->SetStringField(TEXT("system"), System->GetName());
    Root->SetNumberField(TEXT("fixes_attempted"), Attempted);
    Root->SetNumberField(TEXT("fixes_failed"), Failed);
    Root->SetNumberField(TEXT("issues_before"), IssuesBefore);
    Root->SetNumberField(TEXT("issues_after"), IssuesAfter);
    Root->SetNumberField(TEXT("fixable_before"), FixableBefore);
    Root->SetNumberField(TEXT("fixable_after"), FixableAfter);
    Root->SetBoolField(TEXT("dirty"), Attempted > 0);
    Root->SetArrayField(TEXT("applied"), Applied);
    if (Attempted == 0)
    {
        Root->SetStringField(TEXT("note"), IssueIdentifier.IsEmpty() && !bFixAll
            ? TEXT("no issue targeted -- pass issue_identifier (from get_niagara_stack_issues) or fix_all=true")
            : TEXT("no matching fixable issue found"));
    }
    return MCPNStk_Serialize(Root);
#else
    return MCPNStk_Err(TEXT("editor-only"));
#endif
}
