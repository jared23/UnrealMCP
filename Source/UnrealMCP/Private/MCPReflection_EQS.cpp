// UnrealMCP reflection — Environment Query System (EQS) RUNTIME executor (C++ #25 2026-08-19).
//
// run_env_query: execute a UEnvQuery in a LIVE (PIE/game) world against a querier actor and return the
// SCORED result items. EQS is only processed by a running world (the editor/non-PIE world does not tick
// queries), so this is a PIE-gated runtime handler mirroring the C++ #23 BT-runtime pattern
// (MCPReflection_BTRuntime.cpp): Python resolves the querier actor in the PIE world and passes it in, and
// this handler runs the query synchronously via UEnvQueryManager::RunInstantQuery.
//
// The EQS AUTHORING writers (add/remove option, add/remove test, set node property) already live in
// MCPReflectionLibrary.cpp (the "C++ #15" block) and are declared in MCPReflectionLibrary.h — do NOT
// duplicate them here (member re-definition = link error). This TU adds ONLY RunEnvQueryJson.
//
// Member DEFINITION for UMCPReflectionLibrary; the UFUNCTION decl goes in MCPReflectionLibrary.h (coordinator
// merges). Anon-namespace helpers prefixed MCPEqs_ to stay unique in the unity build. AIModule is already a
// Build.cs dep and every type used here (UEnvQueryManager / FEnvQueryRequest / FEnvQueryResult) is
// AIMODULE_API-exported — NO Build.cs change, NO export patch.

#include "MCPReflectionLibrary.h"

#include "Dom/JsonObject.h"
#include "Dom/JsonValue.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"

#include "Engine/World.h"
#include "GameFramework/Actor.h"

#include "EnvironmentQuery/EnvQuery.h"
#include "EnvironmentQuery/EnvQueryTypes.h"
#include "EnvironmentQuery/EnvQueryManager.h"

namespace
{
    FString MCPEqs_SerializeJson(const TSharedRef<FJsonObject>& Root)
    {
        FString Out;
        TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
        FJsonSerializer::Serialize(Root, Writer);
        return Out;
    }

    FString MCPEqs_Error(const FString& Message)
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("error"));
        Root->SetStringField(TEXT("error"), Message);
        return MCPEqs_SerializeJson(Root);
    }

    TSharedRef<FJsonObject> MCPEqs_Ok()
    {
        TSharedRef<FJsonObject> Root = MakeShared<FJsonObject>();
        Root->SetStringField(TEXT("status"), TEXT("success"));
        return Root;
    }

    // Map a run-mode string (case-insensitive, punctuation-tolerant) to EEnvQueryRunMode. Default AllMatching
    // (return every scored item — the most useful for inspection). Also reports the canonical name it chose.
    EEnvQueryRunMode::Type MCPEqs_ParseRunMode(const FString& In, FString& OutCanonical)
    {
        FString S = In.ToLower();
        S.ReplaceInline(TEXT("_"), TEXT(""));
        S.ReplaceInline(TEXT(" "), TEXT(""));
        S.ReplaceInline(TEXT("-"), TEXT(""));
        S.ReplaceInline(TEXT("%"), TEXT(""));
        S.ReplaceInline(TEXT("pct"), TEXT(""));
        S.ReplaceInline(TEXT("percent"), TEXT(""));
        if (S == TEXT("single") || S == TEXT("singleresult") || S == TEXT("best") || S == TEXT("singlebest"))
        {
            OutCanonical = TEXT("SingleResult");
            return EEnvQueryRunMode::SingleResult;
        }
        if (S == TEXT("randombest5") || S == TEXT("random5") || S == TEXT("best5"))
        {
            OutCanonical = TEXT("RandomBest5Pct");
            return EEnvQueryRunMode::RandomBest5Pct;
        }
        if (S == TEXT("randombest25") || S == TEXT("random25") || S == TEXT("best25"))
        {
            OutCanonical = TEXT("RandomBest25Pct");
            return EEnvQueryRunMode::RandomBest25Pct;
        }
        OutCanonical = TEXT("AllMatching");
        return EEnvQueryRunMode::AllMatching;
    }

    FString MCPEqs_StatusToString(EEnvQueryStatus::Type Status)
    {
        switch (Status)
        {
        case EEnvQueryStatus::Processing:   return TEXT("Processing");
        case EEnvQueryStatus::Success:      return TEXT("Success");
        case EEnvQueryStatus::Failed:       return TEXT("Failed");
        case EEnvQueryStatus::Aborted:      return TEXT("Aborted");
        case EEnvQueryStatus::OwnerLost:    return TEXT("OwnerLost");
        case EEnvQueryStatus::MissingParam: return TEXT("MissingParam");
        default:                            return TEXT("Unknown");
        }
    }
}

// ---- run_env_query: execute an EQS query in a live world, return scored items -------------------
// Query   : the UEnvQuery asset (Python loads it via EditorAssetLibrary.load_asset — assets are global).
// Querier : the owner/querier actor RESOLVED BY PYTHON in the live PIE world (contexts like
//           EnvQueryContext_Querier resolve against it; it also provides the world for the query manager).
// RunMode : "single" | "randombest5" | "randombest25" | "all" (default all). Punctuation-tolerant.
// MaxItems: cap the number of returned items (<=0 = all). The full item_count is always reported.
FString UMCPReflectionLibrary::RunEnvQueryJson(UEnvQuery* Query, AActor* Querier, const FString& RunMode, int32 MaxItems)
{
#if WITH_EDITOR
    if (!Query) { return MCPEqs_Error(TEXT("null query")); }
    if (!Querier) { return MCPEqs_Error(TEXT("null querier actor (run_env_query needs a querier resolved in the running world)")); }

    UWorld* World = Querier->GetWorld();
    if (!World) { return MCPEqs_Error(TEXT("could not resolve world from querier actor")); }
    if (!World->IsGameWorld())
    {
        return MCPEqs_Error(TEXT("querier is not in a PIE/Game world — run_env_query requires an active PIE session (the editor world does not process EQS)"));
    }

    UEnvQueryManager* EQMgr = UEnvQueryManager::GetCurrent(World);
    if (!EQMgr) { return MCPEqs_Error(TEXT("no UEnvQueryManager for the querier's world")); }

    FString RunModeCanonical;
    const EEnvQueryRunMode::Type Mode = MCPEqs_ParseRunMode(RunMode, RunModeCanonical);

    // Build + run synchronously. RunInstantQuery ticks the query to completion in-line and returns the result.
    FEnvQueryRequest Request(Query, Querier);
    Request.SetWorldOverride(World);

    TSharedPtr<FEnvQueryResult> Result = EQMgr->RunInstantQuery(Request, Mode);
    if (!Result.IsValid())
    {
        return MCPEqs_Error(TEXT("RunInstantQuery returned no result (query failed to start — check the query has an option with a generator)"));
    }

    TSharedRef<FJsonObject> Root = MCPEqs_Ok();
    Root->SetStringField(TEXT("query"), Query->GetPathName());
    Root->SetStringField(TEXT("querier"), Querier->GetName());
    Root->SetStringField(TEXT("run_mode"), RunModeCanonical);
    Root->SetStringField(TEXT("result_status"), MCPEqs_StatusToString(Result->GetRawStatus()));
    Root->SetBoolField(TEXT("finished"), Result->IsFinished());
    Root->SetBoolField(TEXT("success"), Result->IsSuccessful());
    Root->SetNumberField(TEXT("option_index"), Result->OptionIndex);
    if (Result->ItemType.Get())
    {
        Root->SetStringField(TEXT("item_type"), Result->ItemType->GetName());
    }

    const int32 Total = Result->Items.Num();
    Root->SetNumberField(TEXT("item_count"), Total);
    const int32 Limit = (MaxItems > 0) ? FMath::Min(MaxItems, Total) : Total;
    Root->SetNumberField(TEXT("returned_count"), Limit);

    TArray<TSharedPtr<FJsonValue>> ItemsJson;
    ItemsJson.Reserve(Limit);
    for (int32 i = 0; i < Limit; ++i)
    {
        TSharedRef<FJsonObject> It = MakeShared<FJsonObject>();
        It->SetNumberField(TEXT("index"), i);
        It->SetNumberField(TEXT("score"), Result->GetItemScore(i)); // normalized best-first score

        // GetItemAsLocation / GetItemAsActor are both item-type-guarded (return ZeroVector / nullptr on a type
        // mismatch), so they are safe to call unconditionally. Actor items derive from the vector base, so an
        // actor query yields BOTH an actor and a location; a point query yields a location and a null actor.
        const FVector Loc = Result->GetItemAsLocation(i);
        TSharedRef<FJsonObject> LocObj = MakeShared<FJsonObject>();
        LocObj->SetNumberField(TEXT("x"), Loc.X);
        LocObj->SetNumberField(TEXT("y"), Loc.Y);
        LocObj->SetNumberField(TEXT("z"), Loc.Z);
        It->SetObjectField(TEXT("location"), LocObj);

        if (AActor* ItemActor = Result->GetItemAsActor(i))
        {
            It->SetStringField(TEXT("actor"), ItemActor->GetName());
            It->SetStringField(TEXT("actor_path"), ItemActor->GetPathName());
        }
        ItemsJson.Add(MakeShared<FJsonValueObject>(It));
    }
    Root->SetArrayField(TEXT("items"), ItemsJson);

    return MCPEqs_SerializeJson(Root);
#else
    return MCPEqs_Error(TEXT("editor-only"));
#endif
}
