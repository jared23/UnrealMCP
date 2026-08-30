#include "MCPCommandHandlers.h"

#include "ActorEditorUtils.h"
#include "Editor.h"
#include "EngineUtils.h"
#include "MCPFileLogger.h"
#include "HAL/PlatformFilemanager.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/Guid.h"
#include "Misc/Base64.h"          // MCP_TOOLING_GAPS #6: base64 the user code so backslashes can't wedge the wrapper
#include "MCPConstants.h"
#include "Kismet/GameplayStatics.h"
#include "Kismet/KismetSystemLibrary.h"
#include "Engine/Blueprint.h"
#include "Engine/BlueprintGeneratedClass.h"


//
// FMCPGetSceneInfoHandler
//
TSharedPtr<FJsonObject> FMCPGetSceneInfoHandler::Execute(const TSharedPtr<FJsonObject> &Params, FSocket *ClientSocket)
{
    MCP_LOG_INFO("Handling get_scene_info command");

    UWorld *World = GEditor->GetEditorWorldContext().World();
    TSharedPtr<FJsonObject> Result = MakeShared<FJsonObject>();
    TArray<TSharedPtr<FJsonValue>> ActorsArray;

    int32 ActorCount = 0;
    int32 TotalActorCount = 0;
    bool bLimitReached = false;

    // First count the total number of actors
    for (TActorIterator<AActor> CountIt(World); CountIt; ++CountIt)
    {
        TotalActorCount++;
    }

    // Then collect actor info up to the limit
    for (TActorIterator<AActor> It(World); It; ++It)
    {
        AActor *Actor = *It;
        TSharedPtr<FJsonObject> ActorInfo = MakeShared<FJsonObject>();
        ActorInfo->SetStringField("name", Actor->GetName());
        ActorInfo->SetStringField("type", Actor->GetClass()->GetName());

        // Add the actor label (user-facing friendly name)
        ActorInfo->SetStringField("label", Actor->GetActorLabel());

        // Add location
        FVector Location = Actor->GetActorLocation();
        TArray<TSharedPtr<FJsonValue>> LocationArray;
        LocationArray.Add(MakeShared<FJsonValueNumber>(Location.X));
        LocationArray.Add(MakeShared<FJsonValueNumber>(Location.Y));
        LocationArray.Add(MakeShared<FJsonValueNumber>(Location.Z));
        ActorInfo->SetArrayField("location", LocationArray);

        ActorsArray.Add(MakeShared<FJsonValueObject>(ActorInfo));
        ActorCount++;
        if (ActorCount >= MCPConstants::MAX_ACTORS_IN_SCENE_INFO)
        {
            bLimitReached = true;
            MCP_LOG_WARNING("Actor limit reached (%d). Only returning %d of %d actors.",
                            MCPConstants::MAX_ACTORS_IN_SCENE_INFO, ActorCount, TotalActorCount);
            break; // Limit for performance
        }
    }

    Result->SetStringField("level", World->GetName());
    Result->SetNumberField("actor_count", TotalActorCount);
    Result->SetNumberField("returned_actor_count", ActorCount);
    Result->SetBoolField("limit_reached", bLimitReached);
    Result->SetArrayField("actors", ActorsArray);

    MCP_LOG_INFO("Sending get_scene_info response with %d/%d actors", ActorCount, TotalActorCount);

    return CreateSuccessResponse(Result);
}

//
// FMCPCreateObjectHandler
//
TSharedPtr<FJsonObject> FMCPCreateObjectHandler::Execute(const TSharedPtr<FJsonObject> &Params, FSocket *ClientSocket)
{
    UWorld *World = GEditor->GetEditorWorldContext().World();

    FString Type;
    if (!Params->TryGetStringField(FStringView(TEXT("type")), Type))
    {
        MCP_LOG_WARNING("Missing 'type' field in create_object command");
        return CreateErrorResponse("Missing 'type' field");
    }

    // Get location
    const TArray<TSharedPtr<FJsonValue>> *LocationArrayPtr = nullptr;
    if (!Params->TryGetArrayField(FStringView(TEXT("location")), LocationArrayPtr) || !LocationArrayPtr || LocationArrayPtr->Num() != 3)
    {
        MCP_LOG_WARNING("Invalid 'location' field in create_object command");
        return CreateErrorResponse("Invalid 'location' field");
    }

    FVector Location(
        (*LocationArrayPtr)[0]->AsNumber(),
        (*LocationArrayPtr)[1]->AsNumber(),
        (*LocationArrayPtr)[2]->AsNumber());

    // Convert type to lowercase for case-insensitive comparison
    FString TypeLower = Type.ToLower();

    if (Type == "StaticMeshActor")
    {
        // Get mesh path if specified
        FString MeshPath;
        Params->TryGetStringField(FStringView(TEXT("mesh")), MeshPath);

        // Get label if specified
        FString Label;
        Params->TryGetStringField(FStringView(TEXT("label")), Label);

        // Create the actor
        TPair<AStaticMeshActor *, bool> Result = CreateStaticMeshActor(World, Location, MeshPath, Label);

        if (Result.Value)
        {
            TSharedPtr<FJsonObject> ResultObj = MakeShared<FJsonObject>();
            ResultObj->SetStringField("name", Result.Key->GetName());
            ResultObj->SetStringField("label", Result.Key->GetActorLabel());
            return CreateSuccessResponse(ResultObj);
        }
        else
        {
            return CreateErrorResponse("Failed to create StaticMeshActor");
        }
    }
    else if (TypeLower == "cube")
    {
        // Create a cube actor
        FString Label;
        Params->TryGetStringField(FStringView(TEXT("label")), Label);
        TPair<AStaticMeshActor *, bool> Result = CreateCubeActor(World, Location, Label);

        if (Result.Value)
        {
            TSharedPtr<FJsonObject> ResultObj = MakeShared<FJsonObject>();
            ResultObj->SetStringField("name", Result.Key->GetName());
            ResultObj->SetStringField("label", Result.Key->GetActorLabel());
            return CreateSuccessResponse(ResultObj);
        }
        else
        {
            return CreateErrorResponse("Failed to create cube");
        }
    }
    else
    {
        MCP_LOG_WARNING("Unsupported actor type: %s", *Type);
        return CreateErrorResponse(FString::Printf(TEXT("Unsupported actor type: %s"), *Type));
    }
}

TPair<AStaticMeshActor *, bool> FMCPCreateObjectHandler::CreateStaticMeshActor(UWorld *World, const FVector &Location, const FString &MeshPath, const FString &Label)
{
    if (!World)
    {
        return TPair<AStaticMeshActor *, bool>(nullptr, false);
    }

    // Create the actor
    FActorSpawnParameters SpawnParams;
    SpawnParams.Name = NAME_None; // Auto-generate a name
    SpawnParams.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AdjustIfPossibleButAlwaysSpawn;

    AStaticMeshActor *NewActor = World->SpawnActor<AStaticMeshActor>(Location, FRotator::ZeroRotator, SpawnParams);
    if (NewActor)
    {
        MCP_LOG_INFO("Created StaticMeshActor at location (%f, %f, %f)", Location.X, Location.Y, Location.Z);

        // Set mesh if specified
        if (!MeshPath.IsEmpty())
        {
            UStaticMesh *Mesh = LoadObject<UStaticMesh>(nullptr, *MeshPath);
            if (Mesh)
            {
                NewActor->GetStaticMeshComponent()->SetStaticMesh(Mesh);
                MCP_LOG_INFO("Set mesh to %s", *MeshPath);
            }
            else
            {
                MCP_LOG_WARNING("Failed to load mesh %s", *MeshPath);
            }
        }

        // Set a descriptive label
        if (!Label.IsEmpty())
        {
            NewActor->SetActorLabel(Label);
            MCP_LOG_INFO("Set custom label to %s", *Label);
        }
        else
        {
            NewActor->SetActorLabel(FString::Printf(TEXT("MCP_StaticMesh_%d"), FMath::RandRange(1000, 9999)));
        }

        return TPair<AStaticMeshActor *, bool>(NewActor, true);
    }
    else
    {
        MCP_LOG_ERROR("Failed to create StaticMeshActor");
        return TPair<AStaticMeshActor *, bool>(nullptr, false);
    }
}

TPair<AStaticMeshActor *, bool> FMCPCreateObjectHandler::CreateCubeActor(UWorld *World, const FVector &Location, const FString &Label)
{
    if (!World)
    {
        return TPair<AStaticMeshActor *, bool>(nullptr, false);
    }

    // Create a StaticMeshActor with a cube mesh
    FActorSpawnParameters SpawnParams;
    SpawnParams.Name = NAME_None;
    SpawnParams.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AdjustIfPossibleButAlwaysSpawn;

    AStaticMeshActor *NewActor = World->SpawnActor<AStaticMeshActor>(Location, FRotator::ZeroRotator, SpawnParams);
    if (NewActor)
    {
        MCP_LOG_INFO("Created Cube at location (%f, %f, %f)", Location.X, Location.Y, Location.Z);

        // Set cube mesh
        UStaticMesh *CubeMesh = LoadObject<UStaticMesh>(nullptr, TEXT("/Engine/BasicShapes/Cube.Cube"));
        if (CubeMesh)
        {
            NewActor->GetStaticMeshComponent()->SetStaticMesh(CubeMesh);
            MCP_LOG_INFO("Set cube mesh");

            // Set a descriptive label
            if (!Label.IsEmpty())
            {
                NewActor->SetActorLabel(Label);
                MCP_LOG_INFO("Set custom label to %s", *Label);
            }
            else
            {
                NewActor->SetActorLabel(FString::Printf(TEXT("MCP_Cube_%d"), FMath::RandRange(1000, 9999)));
            }

            return TPair<AStaticMeshActor *, bool>(NewActor, true);
        }
        else
        {
            MCP_LOG_WARNING("Failed to load cube mesh");
            World->DestroyActor(NewActor);
            return TPair<AStaticMeshActor *, bool>(nullptr, false);
        }
    }
    else
    {
        MCP_LOG_ERROR("Failed to create Cube");
        return TPair<AStaticMeshActor *, bool>(nullptr, false);
    }
}

//
// FMCPModifyObjectHandler
//
TSharedPtr<FJsonObject> FMCPModifyObjectHandler::Execute(const TSharedPtr<FJsonObject> &Params, FSocket *ClientSocket)
{
    UWorld *World = GEditor->GetEditorWorldContext().World();

    FString ActorName;
    if (!Params->TryGetStringField(FStringView(TEXT("name")), ActorName))
    {
        MCP_LOG_WARNING("Missing 'name' field in modify_object command");
        return CreateErrorResponse("Missing 'name' field");
    }

    AActor *Actor = nullptr;
    for (TActorIterator<AActor> It(World); It; ++It)
    {
        if (It->GetName() == ActorName)
        {
            Actor = *It;
            break;
        }
    }

    if (!Actor)
    {
        MCP_LOG_WARNING("Actor not found: %s", *ActorName);
        return CreateErrorResponse(FString::Printf(TEXT("Actor not found: %s"), *ActorName));
    }

    bool bModified = false;

    // Check for location update
    const TArray<TSharedPtr<FJsonValue>> *LocationArrayPtr = nullptr;
    if (Params->TryGetArrayField(FStringView(TEXT("location")), LocationArrayPtr) && LocationArrayPtr && LocationArrayPtr->Num() == 3)
    {
        FVector NewLocation(
            (*LocationArrayPtr)[0]->AsNumber(),
            (*LocationArrayPtr)[1]->AsNumber(),
            (*LocationArrayPtr)[2]->AsNumber());

        Actor->SetActorLocation(NewLocation);
        MCP_LOG_INFO("Updated location of %s to (%f, %f, %f)", *ActorName, NewLocation.X, NewLocation.Y, NewLocation.Z);
        bModified = true;
    }

    // Check for rotation update
    const TArray<TSharedPtr<FJsonValue>> *RotationArrayPtr = nullptr;
    if (Params->TryGetArrayField(FStringView(TEXT("rotation")), RotationArrayPtr) && RotationArrayPtr && RotationArrayPtr->Num() == 3)
    {
        FRotator NewRotation(
            (*RotationArrayPtr)[0]->AsNumber(),
            (*RotationArrayPtr)[1]->AsNumber(),
            (*RotationArrayPtr)[2]->AsNumber());

        Actor->SetActorRotation(NewRotation);
        MCP_LOG_INFO("Updated rotation of %s to (%f, %f, %f)", *ActorName, NewRotation.Pitch, NewRotation.Yaw, NewRotation.Roll);
        bModified = true;
    }

    // Check for scale update
    const TArray<TSharedPtr<FJsonValue>> *ScaleArrayPtr = nullptr;
    if (Params->TryGetArrayField(FStringView(TEXT("scale")), ScaleArrayPtr) && ScaleArrayPtr && ScaleArrayPtr->Num() == 3)
    {
        FVector NewScale(
            (*ScaleArrayPtr)[0]->AsNumber(),
            (*ScaleArrayPtr)[1]->AsNumber(),
            (*ScaleArrayPtr)[2]->AsNumber());

        Actor->SetActorScale3D(NewScale);
        MCP_LOG_INFO("Updated scale of %s to (%f, %f, %f)", *ActorName, NewScale.X, NewScale.Y, NewScale.Z);
        bModified = true;
    }

    if (bModified)
    {
        // Create a result object with the actor name
        TSharedPtr<FJsonObject> Result = MakeShared<FJsonObject>();
        Result->SetStringField("name", Actor->GetName());

        // Return success with the result object
        return CreateSuccessResponse(Result);
    }
    else
    {
        MCP_LOG_WARNING("No modifications specified for %s", *ActorName);
        TSharedPtr<FJsonObject> Response = MakeShared<FJsonObject>();
        Response->SetStringField("status", "warning");
        Response->SetStringField("message", "No modifications specified");
        return Response;
    }
}

//
// FMCPDeleteObjectHandler
//
TSharedPtr<FJsonObject> FMCPDeleteObjectHandler::Execute(const TSharedPtr<FJsonObject> &Params, FSocket *ClientSocket)
{
    UWorld *World = GEditor->GetEditorWorldContext().World();

    FString ActorName;
    if (!Params->TryGetStringField(FStringView(TEXT("name")), ActorName))
    {
        MCP_LOG_WARNING("Missing 'name' field in delete_object command");
        return CreateErrorResponse("Missing 'name' field");
    }

    AActor *Actor = nullptr;
    for (TActorIterator<AActor> It(World); It; ++It)
    {
        if (It->GetName() == ActorName)
        {
            Actor = *It;
            break;
        }
    }

    if (!Actor)
    {
        MCP_LOG_WARNING("Actor not found: %s", *ActorName);
        return CreateErrorResponse(FString::Printf(TEXT("Actor not found: %s"), *ActorName));
    }

    // Check if the actor can be deleted
    if (!FActorEditorUtils::IsABuilderBrush(Actor))
    {
        bool bDestroyed = World->DestroyActor(Actor);
        if (bDestroyed)
        {
            MCP_LOG_INFO("Deleted actor: %s", *ActorName);
            return CreateSuccessResponse();
        }
        else
        {
            MCP_LOG_ERROR("Failed to delete actor: %s", *ActorName);
            return CreateErrorResponse(FString::Printf(TEXT("Failed to delete actor: %s"), *ActorName));
        }
    }
    else
    {
        MCP_LOG_WARNING("Cannot delete special actor: %s", *ActorName);
        return CreateErrorResponse(FString::Printf(TEXT("Cannot delete special actor: %s"), *ActorName));
    }
}

//
// FMCPExecutePythonHandler
//
TSharedPtr<FJsonObject> FMCPExecutePythonHandler::Execute(const TSharedPtr<FJsonObject> &Params, FSocket *ClientSocket)
{
    // This handler runs on the game thread (FMCPTCPServer::Tick -> ProcessClientData -> ProcessCommand).
    check(IsInGameThread());

    // Re-entrancy guard: GEngine->Exec(python) below runs synchronously and the executed script can pump the
    // game thread (e.g. save_asset / compile), which re-fires FTSTicker -> a NESTED Execute. Two runs would
    // collide on the temp-capture files and corrupt each other's state (observed crash: torn FString read in
    // the output-read phase). Reject the nested call rather than run it re-entrantly.
    if (bIsExecuting)
    {
        MCP_LOG_WARNING("execute_python re-entered on the game thread (an executed script pumped the tick) — rejecting the nested call");
        return CreateErrorResponse(TEXT("execute_python is already running on this thread (re-entrant call rejected). An executed script pumped the game thread (e.g. save/compile) while another command was in flight; serialize your calls or avoid pumping mid-script."));
    }
    TGuardValue<bool> ExecutingGuard(bIsExecuting, true);

    // Check if we have code or file parameter
    FString PythonCode;
    FString PythonFile;
    bool hasCode = Params->TryGetStringField(FStringView(TEXT("code")), PythonCode);
    bool hasFile = Params->TryGetStringField(FStringView(TEXT("file")), PythonFile);

    // If code/file not found directly, check if they're in a 'data' object
    if (!hasCode && !hasFile)
    {
        const TSharedPtr<FJsonObject> *DataObject;
        if (Params->TryGetObjectField(FStringView(TEXT("data")), DataObject))
        {
            hasCode = (*DataObject)->TryGetStringField(FStringView(TEXT("code")), PythonCode);
            hasFile = (*DataObject)->TryGetStringField(FStringView(TEXT("file")), PythonFile);
        }
    }

    if (!hasCode && !hasFile)
    {
        MCP_LOG_WARNING("Missing 'code' or 'file' field in execute_python command");
        return CreateErrorResponse("Missing 'code' or 'file' field. You must provide either Python code or a file path.");
    }

    FString Result;
    bool bSuccess = false;
    FString ErrorMessage;

    if (hasCode)
    {
        // For code execution, we'll create a temporary file and execute that
        MCP_LOG_INFO("Executing Python code via temporary file");

        // Create a temporary file in the project's Saved/Temp directory
        FString TempDir = FPaths::ProjectSavedDir() / MCPConstants::PYTHON_TEMP_DIR_NAME;
        IPlatformFile &PlatformFile = FPlatformFileManager::Get().GetPlatformFile();

        // Ensure the directory exists
        if (!PlatformFile.DirectoryExists(*TempDir))
        {
            PlatformFile.CreateDirectory(*TempDir);
        }

        // MCP_TOOLING_GAPS #6: per-call UNIQUE capture files. The old code reused fixed output.txt/error.txt/
        // status.txt names, so a wrapper that failed to run (e.g. a user backslash path caused a SyntaxError in
        // the wrapper itself) left the C++ side reading a stale/empty file -> every payload tool returned
        // "no @@UMCP@@ payload" until an editor restart. Unique names per call make cross-call contamination
        // impossible.
        FString RunId = FGuid::NewGuid().ToString(EGuidFormats::Digits);
        FString TempFilePath = TempDir / (FString(MCPConstants::PYTHON_TEMP_FILE_PREFIX) + RunId + TEXT(".py"));
        FString OutPath = TempDir / (TEXT("mcp_out_") + RunId + TEXT(".txt"));
        FString ErrPath = TempDir / (TEXT("mcp_err_") + RunId + TEXT(".txt"));
        FString StatusPath = TempDir / (TEXT("mcp_status_") + RunId + TEXT(".txt"));

        // MCP_TOOLING_GAPS #6: base64 the user code so backslashes / quotes / triple-quotes in it can NEVER break
        // the wrapper's own parse. Previously the code was embedded as user_code = '''<code>''' — a Windows path
        // literal like "C:\Users\..." made the wrapper raise SyntaxError: (unicode error) truncated \UXXXX escape,
        // and nothing ran. Base64 sidesteps all escaping. (Callers should still prefer forward slashes in paths.)
        FTCHARToUTF8 CodeUtf8(*PythonCode);
        FString CodeB64 = FBase64::Encode(reinterpret_cast<const uint8*>(CodeUtf8.Get()), CodeUtf8.Length());

        FString WrappedPythonCode =
              FString(TEXT("import sys, traceback, base64\n"))
            + TEXT("import unreal\n")
            + TEXT("output_file = open('") + OutPath + TEXT("', 'w')\n")
            + TEXT("error_file = open('") + ErrPath + TEXT("', 'w')\n")
            + TEXT("original_stdout = sys.stdout\n")
            + TEXT("original_stderr = sys.stderr\n")
            + TEXT("sys.stdout = output_file\n")
            + TEXT("sys.stderr = error_file\n")
            + TEXT("success = True\n")
            + TEXT("try:\n")
            + TEXT("    user_code = base64.b64decode('") + CodeB64 + TEXT("').decode('utf-8')\n")
            + TEXT("    exec(compile(user_code, '<mcp>', 'exec'))\n")
            + TEXT("except Exception:\n")
            + TEXT("    traceback.print_exc()\n")
            + TEXT("    success = False\n")
            + TEXT("finally:\n")
            + TEXT("    sys.stdout = original_stdout\n")
            + TEXT("    sys.stderr = original_stderr\n")
            + TEXT("    output_file.close()\n")
            + TEXT("    error_file.close()\n")
            + TEXT("    _sf = open('") + StatusPath + TEXT("', 'w')\n")
            + TEXT("    _sf.write('1' if success else '0')\n")
            + TEXT("    _sf.close()\n");

        // Write the Python code to the temporary file
        if (FFileHelper::SaveStringToFile(WrappedPythonCode, *TempFilePath))
        {
            // Execute the temporary file
            FString Command = FString::Printf(TEXT("py \"%s\""), *TempFilePath);
            GEngine->Exec(nullptr, *Command);

            // Read the output, error, and status files
            FString OutputContent;
            FString ErrorContent;
            FString StatusContent;

            FFileHelper::LoadFileToString(OutputContent, *OutPath);
            FFileHelper::LoadFileToString(ErrorContent, *ErrPath);
            FFileHelper::LoadFileToString(StatusContent, *StatusPath);

            bSuccess = StatusContent.TrimStartAndEnd().Equals(TEXT("1"));

            // Combine output and error for the result
            Result = OutputContent;
            ErrorMessage = ErrorContent;

            // Clean up the temporary files
            PlatformFile.DeleteFile(*TempFilePath);
            PlatformFile.DeleteFile(*OutPath);
            PlatformFile.DeleteFile(*ErrPath);
            PlatformFile.DeleteFile(*StatusPath);
        }
        else
        {
            MCP_LOG_ERROR("Failed to create temporary Python file at %s", *TempFilePath);
            return CreateErrorResponse(FString::Printf(TEXT("Failed to create temporary Python file at %s"), *TempFilePath));
        }
    }
    else if (hasFile)
    {
        // Execute Python file
        MCP_LOG_INFO("Executing Python file: %s", *PythonFile);

        // Create a temporary directory for output capture
        FString TempDir = FPaths::ProjectSavedDir() / MCPConstants::PYTHON_TEMP_DIR_NAME;
        IPlatformFile &PlatformFile = FPlatformFileManager::Get().GetPlatformFile();

        // Ensure the directory exists
        if (!PlatformFile.DirectoryExists(*TempDir))
        {
            PlatformFile.CreateDirectory(*TempDir);
        }

        // Create a wrapper script that executes the file and captures output. MCP_TOOLING_GAPS #6: unique per-call
        // capture files (base64 the FILE PATH so backslashes can't break the wrapper, same as the code path).
        FString RunId = FGuid::NewGuid().ToString(EGuidFormats::Digits);
        FString WrapperFilePath = TempDir / (FString(MCPConstants::PYTHON_TEMP_FILE_PREFIX) + TEXT("wrapper_") + RunId + TEXT(".py"));
        FString OutPath = TempDir / (TEXT("mcp_out_") + RunId + TEXT(".txt"));
        FString ErrPath = TempDir / (TEXT("mcp_err_") + RunId + TEXT(".txt"));
        FString StatusPath = TempDir / (TEXT("mcp_status_") + RunId + TEXT(".txt"));
        FTCHARToUTF8 PathUtf8(*PythonFile);
        FString PathB64 = FBase64::Encode(reinterpret_cast<const uint8*>(PathUtf8.Get()), PathUtf8.Length());

        FString WrapperCode =
              FString(TEXT("import sys, traceback, base64\n"))
            + TEXT("import unreal\n")
            + TEXT("output_file = open('") + OutPath + TEXT("', 'w')\n")
            + TEXT("error_file = open('") + ErrPath + TEXT("', 'w')\n")
            + TEXT("original_stdout = sys.stdout\n")
            + TEXT("original_stderr = sys.stderr\n")
            + TEXT("sys.stdout = output_file\n")
            + TEXT("sys.stderr = error_file\n")
            + TEXT("success = True\n")
            + TEXT("try:\n")
            + TEXT("    _p = base64.b64decode('") + PathB64 + TEXT("').decode('utf-8')\n")
            + TEXT("    with open(_p, 'r') as _f: file_content = _f.read()\n")
            + TEXT("    exec(compile(file_content, _p, 'exec'))\n")
            + TEXT("except Exception:\n")
            + TEXT("    traceback.print_exc()\n")
            + TEXT("    success = False\n")
            + TEXT("finally:\n")
            + TEXT("    sys.stdout = original_stdout\n")
            + TEXT("    sys.stderr = original_stderr\n")
            + TEXT("    output_file.close()\n")
            + TEXT("    error_file.close()\n")
            + TEXT("    _sf = open('") + StatusPath + TEXT("', 'w')\n")
            + TEXT("    _sf.write('1' if success else '0')\n")
            + TEXT("    _sf.close()\n");

        if (FFileHelper::SaveStringToFile(WrapperCode, *WrapperFilePath))
        {
            // Execute the wrapper script
            FString Command = FString::Printf(TEXT("py \"%s\""), *WrapperFilePath);
            GEngine->Exec(nullptr, *Command);

            // Read the output, error, and status files
            FString OutputContent;
            FString ErrorContent;
            FString StatusContent;

            FFileHelper::LoadFileToString(OutputContent, *OutPath);
            FFileHelper::LoadFileToString(ErrorContent, *ErrPath);
            FFileHelper::LoadFileToString(StatusContent, *StatusPath);

            bSuccess = StatusContent.TrimStartAndEnd().Equals(TEXT("1"));

            // Combine output and error for the result
            Result = OutputContent;
            ErrorMessage = ErrorContent;

            // Clean up the temporary files
            PlatformFile.DeleteFile(*WrapperFilePath);
            PlatformFile.DeleteFile(*(TempDir / TEXT("output.txt")));
            PlatformFile.DeleteFile(*(TempDir / TEXT("error.txt")));
            PlatformFile.DeleteFile(*(TempDir / TEXT("status.txt")));
        }
        else
        {
            MCP_LOG_ERROR("Failed to create wrapper Python file at %s", *WrapperFilePath);
            return CreateErrorResponse(FString::Printf(TEXT("Failed to create wrapper Python file at %s"), *WrapperFilePath));
        }
    }

    // Create the response
    TSharedPtr<FJsonObject> ResultObj = MakeShared<FJsonObject>();
    ResultObj->SetStringField("output", Result);

    if (bSuccess)
    {
        MCP_LOG_INFO("Python execution successful");
        return CreateSuccessResponse(ResultObj);
    }
    else
    {
        MCP_LOG_ERROR("Python execution failed: %s", *ErrorMessage);
        ResultObj->SetStringField("error", ErrorMessage);

        // We're returning a success response with error details rather than an error response
        // This allows the client to still access the output and error information
        TSharedPtr<FJsonObject> Response = MakeShared<FJsonObject>();
        Response->SetStringField("status", "error");
        Response->SetStringField("message", "Python execution failed with errors");
        Response->SetObjectField("result", ResultObj);
        return Response;
    }
}