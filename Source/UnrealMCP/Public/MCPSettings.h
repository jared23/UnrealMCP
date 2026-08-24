#pragma once
#include "CoreMinimal.h"
#include "Engine/DeveloperSettings.h"
#include "MCPConstants.h"
#include "MCPSettings.generated.h"

UCLASS(config = Editor, defaultconfig)
class UNREALMCP_API UMCPSettings : public UDeveloperSettings
{
    GENERATED_BODY()
public:
    UPROPERTY(config, EditAnywhere, Category = "MCP", meta = (ClampMin = "1024", ClampMax = "65535"))
    int32 Port = MCPConstants::DEFAULT_PORT;

    /** Address the server binds to. Set to this machine's Tailscale IP to only accept
     *  connections over Tailscale. Leave blank to listen on all interfaces (0.0.0.0). */
    UPROPERTY(config, EditAnywhere, Category = "MCP", meta = (DisplayName = "Bind Address (blank = all interfaces)"))
    FString BindAddress = TEXT("100.96.65.74");

    /** If true, the MCP server starts automatically when the editor finishes loading,
     *  with no need to click the toolbar button. */
    UPROPERTY(config, EditAnywhere, Category = "MCP", meta = (DisplayName = "Auto-start server on launch"))
    bool bAutoStartServer = true;
};