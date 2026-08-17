#include "MCPTCPServer.h"
#include "Engine/World.h"
#include "Editor.h"
#include "LevelEditor.h"
#include "Engine/StaticMeshActor.h"
#include "Components/StaticMeshComponent.h"
#include "JsonObjectConverter.h"
#include "Sockets.h"
#include "SocketSubsystem.h"
#include "IPAddress.h"
#include "Interfaces/IPv4/IPv4Address.h"
#include "Interfaces/IPv4/IPv4Endpoint.h"
#include "ActorEditorUtils.h"
#include "EngineUtils.h"
#include "Containers/Ticker.h"
#include "UnrealMCP.h"
#include "MCPFileLogger.h"
#include "MCPCommandHandlers.h"
#include "MCPCommandHandlers_Blueprints.h"
#include "MCPCommandHandlers_Materials.h"
#include "HAL/PlatformFilemanager.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Misc/Guid.h"
#include "MCPConstants.h"


FMCPTCPServer::FMCPTCPServer(const FMCPTCPServerConfig& InConfig) 
    : Config(InConfig)
    , Listener(nullptr)
    , bRunning(false)
{
    // Register default command handlers
    RegisterCommandHandler(MakeShared<FMCPGetSceneInfoHandler>());
    RegisterCommandHandler(MakeShared<FMCPCreateObjectHandler>());
    RegisterCommandHandler(MakeShared<FMCPModifyObjectHandler>());
    RegisterCommandHandler(MakeShared<FMCPDeleteObjectHandler>());
    RegisterCommandHandler(MakeShared<FMCPExecutePythonHandler>());

    // Material command handlers
    RegisterCommandHandler(MakeShared<FMCPCreateMaterialHandler>());
    RegisterCommandHandler(MakeShared<FMCPModifyMaterialHandler>());
    RegisterCommandHandler(MakeShared<FMCPGetMaterialInfoHandler>());

    // Blueprint command handlers
    RegisterCommandHandler(MakeShared<FMCPCreateBlueprintHandler>());
    RegisterCommandHandler(MakeShared<FMCPModifyBlueprintHandler>());
    RegisterCommandHandler(MakeShared<FMCPGetBlueprintInfoHandler>());
    RegisterCommandHandler(MakeShared<FMCPCreateBlueprintEventHandler>());
}

FMCPTCPServer::~FMCPTCPServer()
{
    Stop();
}

void FMCPTCPServer::RegisterCommandHandler(TSharedPtr<IMCPCommandHandler> Handler)
{
    if (!Handler.IsValid())
    {
        MCP_LOG_ERROR("Attempted to register null command handler");
        return;
    }

    FString CommandName = Handler->GetCommandName();
    if (CommandName.IsEmpty())
    {
        MCP_LOG_ERROR("Attempted to register command handler with empty command name");
        return;
    }

    CommandHandlers.Add(CommandName, Handler);
    MCP_LOG_INFO("Registered command handler for '%s'", *CommandName);
}

void FMCPTCPServer::UnregisterCommandHandler(const FString& CommandName)
{
    if (CommandHandlers.Remove(CommandName) > 0)
    {
        MCP_LOG_INFO("Unregistered command handler for '%s'", *CommandName);
    }
    else
    {
        MCP_LOG_WARNING("Attempted to unregister non-existent command handler for '%s'", *CommandName);
    }
}

bool FMCPTCPServer::RegisterExternalCommandHandler(TSharedPtr<IMCPCommandHandler> Handler)
{
    if (!Handler.IsValid())
    {
        MCP_LOG_ERROR("Attempted to register null external command handler");
        return false;
    }

    FString CommandName = Handler->GetCommandName();
    if (CommandName.IsEmpty())
    {
        MCP_LOG_ERROR("Attempted to register external command handler with empty command name");
        return false;
    }

    // Check if there's a conflict with an existing handler
    if (CommandHandlers.Contains(CommandName))
    {
        MCP_LOG_WARNING("External command handler for '%s' conflicts with an existing handler", *CommandName);
        return false;
    }

    // Register the handler
    CommandHandlers.Add(CommandName, Handler);
    MCP_LOG_INFO("Registered external command handler for '%s'", *CommandName);
    return true;
}

bool FMCPTCPServer::UnregisterExternalCommandHandler(const FString& CommandName)
{
    if (CommandName.IsEmpty())
    {
        MCP_LOG_ERROR("Attempted to unregister external command handler with empty command name");
        return false;
    }

    // Check if the handler exists
    if (!CommandHandlers.Contains(CommandName))
    {
        MCP_LOG_WARNING("Attempted to unregister non-existent external command handler for '%s'", *CommandName);
        return false;
    }

    // Unregister the handler
    CommandHandlers.Remove(CommandName);
    MCP_LOG_INFO("Unregistered external command handler for '%s'", *CommandName);
    return true;
}

bool FMCPTCPServer::Start()
{
    if (bRunning)
    {
        MCP_LOG_WARNING("Start called but server is already running, returning true");
        return true;
    }
    
    MCP_LOG_WARNING("Starting MCP server on port %d", Config.Port);

    // Resolve the bind address from config. Empty or unparseable falls back to all
    // interfaces (0.0.0.0); a valid address (e.g. this machine's Tailscale IP) restricts
    // the listener to that interface only.
    FIPv4Address BindAddr = FIPv4Address::Any;
    if (!Config.BindAddress.IsEmpty())
    {
        FIPv4Address Parsed;
        if (FIPv4Address::Parse(Config.BindAddress, Parsed))
        {
            BindAddr = Parsed;
        }
        else
        {
            MCP_LOG_ERROR("Invalid MCP bind address '%s', falling back to all interfaces", *Config.BindAddress);
        }
    }
    MCP_LOG_WARNING("Binding MCP server to %s:%d", *BindAddr.ToString(), Config.Port);

    // Use a simple ASCII string for the socket description to avoid encoding issues
    Listener = new FTcpListener(FIPv4Endpoint(BindAddr, Config.Port));
    if (!Listener || !Listener->IsActive())
    {
        MCP_LOG_ERROR("Failed to start MCP server on port %d", Config.Port);
        Stop();
        return false;
    }

    // Clear any existing client connections
    ClientConnections.Empty();

    TickerHandle = FTSTicker::GetCoreTicker().AddTicker(FTickerDelegate::CreateRaw(this, &FMCPTCPServer::Tick), Config.TickIntervalSeconds);
    bRunning = true;
    MCP_LOG_INFO("MCP Server started on port %d", Config.Port);
    return true;
}

void FMCPTCPServer::Stop()
{
    // Clean up all client connections
    CleanupAllClientConnections();
    
    if (Listener)
    {
        delete Listener;
        Listener = nullptr;
    }
    
    if (TickerHandle.IsValid())
    {
        FTSTicker::GetCoreTicker().RemoveTicker(TickerHandle);
        TickerHandle.Reset();
    }
    
    bRunning = false;
    MCP_LOG_INFO("MCP Server stopped");
}

bool FMCPTCPServer::Tick(float DeltaTime)
{
    if (!bRunning) return false;
    
    // Normal processing
    ProcessPendingConnections();
    ProcessClientData();
    CheckClientTimeouts(DeltaTime);
    return true;
}

void FMCPTCPServer::ProcessPendingConnections()
{
    if (!Listener) return;
    
    // Always accept new connections
    if (!Listener->OnConnectionAccepted().IsBound())
    {
        Listener->OnConnectionAccepted().BindRaw(this, &FMCPTCPServer::HandleConnectionAccepted);
    }

    // Drain connections accepted on the listener thread into ClientConnections HERE, on the game thread,
    // so ClientConnections is only ever mutated/read from one thread (fixes the accept-vs-iterate race).
    FMCPClientConnection NewConnection;
    while (PendingConnectionQueue.Dequeue(NewConnection))
    {
        const FString EndpointStr = NewConnection.Endpoint.ToString();
        ClientConnections.Add(MoveTemp(NewConnection));
        MCP_LOG_INFO("MCP Client registered from %s (Total clients: %d)", *EndpointStr, ClientConnections.Num());
    }
}

bool FMCPTCPServer::HandleConnectionAccepted(FSocket* InSocket, const FIPv4Endpoint& Endpoint)
{
    if (!InSocket)
    {
        MCP_LOG_ERROR("HandleConnectionAccepted called with null socket");
        return false;
    }

    MCP_LOG_VERBOSE("Connection attempt from %s", *Endpoint.ToString());
    
    // Accept all connections
    InSocket->SetNonBlocking(true);

    // NOTE: this runs on the FTcpListener's OWN thread. Do NOT touch ClientConnections here (it is
    // game-thread-only) — that raced with Tick's copy/iterate and corrupted the array (AV / editor crash).
    // Hand the connection to the game thread via the lock-free queue; ProcessPendingConnections drains it.
    PendingConnectionQueue.Enqueue(FMCPClientConnection(InSocket, Endpoint, Config.ReceiveBufferSize));

    MCP_LOG_INFO("MCP Client accepted from %s (queued for registration)", *Endpoint.ToString());
    return true;
}

void FMCPTCPServer::ProcessClientData()
{
    // Make a copy of the array since we might modify it during iteration
    TArray<FMCPClientConnection> ConnectionsCopy = ClientConnections;
    
    for (FMCPClientConnection& ClientConnection : ConnectionsCopy)
    {
        if (!ClientConnection.Socket) continue;
        
        // Check if the client is still connected
        uint32 PendingDataSize = 0;
        if (!ClientConnection.Socket->HasPendingData(PendingDataSize))
        {
            // Try to check connection status
            uint8 DummyBuffer[1];
            int32 BytesRead = 0;
            
            bool bConnectionLost = false;
            
            try
            {
                if (!ClientConnection.Socket->Recv(DummyBuffer, 1, BytesRead, ESocketReceiveFlags::Peek))
                {
                    // Check if it's a real error or just a non-blocking socket that would block
                    int32 ErrorCode = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->GetLastErrorCode();
                    if (ErrorCode != SE_EWOULDBLOCK)
                    {
                        // Real connection error
                        MCP_LOG_INFO("Client connection from %s appears to be closed (error code %d), cleaning up", 
                            *ClientConnection.Endpoint.ToString(), ErrorCode);
                        bConnectionLost = true;
                    }
                }
            }
            catch (...)
            {
                MCP_LOG_ERROR("Exception while checking client connection status for %s", 
                    *ClientConnection.Endpoint.ToString());
                bConnectionLost = true;
            }
            
            if (bConnectionLost)
            {
                CleanupClientConnection(ClientConnection);
                continue; // Skip to the next client
            }
        }
        
        // Reset PendingDataSize and check again to ensure we have the latest value
        PendingDataSize = 0;
        if (ClientConnection.Socket->HasPendingData(PendingDataSize))
        {
            if (Config.bEnableVerboseLogging)
            {
                MCP_LOG_VERBOSE("Client from %s has %u bytes of pending data", 
                    *ClientConnection.Endpoint.ToString(), PendingDataSize);
            }
            
            // Reset timeout timer since we're receiving data
            ClientConnection.TimeSinceLastActivity = 0.0f;
            
            // Reassembly: a command can be split across TCP reads or be larger than one ReceiveBuffer.
            // Drain everything currently available into this client's accumulator, then try to parse a
            // complete JSON object. The previous code did a single Recv and treated the raw chunk as a
            // whole message, so anything >~64KB (or fragmented mid-flight) got truncated -> "Invalid
            // JSON". Recv is still capped at Num()-1 (a full chunk once caused ReceiveBuffer[65536]=0,
            // 1 past the 64KB array end -> fatal bounds assert / editor crash).
            TArray<uint8>& Pending = PartialMessages.FindOrAdd(ClientConnection.Socket);
            bool bSocketError = false;
            for (;;)
            {
                int32 BytesRead = 0;
                if (ClientConnection.Socket->Recv(ClientConnection.ReceiveBuffer.GetData(), ClientConnection.ReceiveBuffer.Num() - 1, BytesRead))
                {
                    if (BytesRead > 0)
                    {
                        Pending.Append(ClientConnection.ReceiveBuffer.GetData(), BytesRead);
                        if (Config.bEnableVerboseLogging)
                        {
                            MCP_LOG_VERBOSE("Read %d bytes from client %s (buffered %d)",
                                BytesRead, *ClientConnection.Endpoint.ToString(), Pending.Num());
                        }
                    }
                    // Keep draining while the socket still has queued data (large / fragmented message).
                    uint32 MorePending = 0;
                    if (BytesRead > 0 && ClientConnection.Socket->HasPendingData(MorePending) && MorePending > 0)
                    {
                        continue;
                    }
                    break;
                }

                // Recv failed: distinguish a would-block (normal for non-blocking sockets) from a real error.
                int32 ErrorCode = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM)->GetLastErrorCode();
                if (ErrorCode != SE_EWOULDBLOCK)
                {
                    MCP_LOG_WARNING("Socket error %d for client %s, closing connection",
                        ErrorCode, *ClientConnection.Endpoint.ToString());
                    bSocketError = true;
                }
                break;
            }

            if (bSocketError)
            {
                PartialMessages.Remove(ClientConnection.Socket);
                CleanupClientConnection(ClientConnection);
                continue;
            }

            if (Pending.Num() > 0)
            {
                // Try to parse everything buffered so far as one complete JSON command. MCP is
                // request/response (at most one command in flight per connection), so a successful
                // parse means the whole message has arrived.
                TArray<uint8> NullTerminated = Pending;
                NullTerminated.Add(0);
                const FString Candidate = FString(UTF8_TO_TCHAR(reinterpret_cast<const char*>(NullTerminated.GetData())));

                TSharedPtr<FJsonObject> ParsedCheck;
                TSharedRef<TJsonReader<>> CheckReader = TJsonReaderFactory<>::Create(Candidate);
                if (FJsonSerializer::Deserialize(CheckReader, ParsedCheck) && ParsedCheck.IsValid())
                {
                    // Complete, valid command — consume the buffer, then dispatch.
                    PartialMessages.Remove(ClientConnection.Socket);
                    ProcessCommand(Candidate, ClientConnection.Socket);
                }
                else if (Pending.Num() >= MCPConstants::MAX_PENDING_MESSAGE_BYTES)
                {
                    // Not valid JSON and we've buffered an unreasonable amount: give up on this client's
                    // stream so it can't grow without bound, and tell the client instead of hanging.
                    MCP_LOG_WARNING("Discarding %d buffered bytes from client %s: no complete JSON within limit",
                        Pending.Num(), *ClientConnection.Endpoint.ToString());
                    PartialMessages.Remove(ClientConnection.Socket);

                    TSharedPtr<FJsonObject> ErrorResponse = MakeShared<FJsonObject>();
                    ErrorResponse->SetStringField(TEXT("status"), TEXT("error"));
                    ErrorResponse->SetStringField(TEXT("message"), TEXT("Message too large or malformed"));
                    SendResponse(ClientConnection.Socket, ErrorResponse);
                }
                // else: valid JSON not yet complete and still under the cap -> keep buffered for next tick.
            }
        }
    }
}

void FMCPTCPServer::CheckClientTimeouts(float DeltaTime)
{
    // Make a copy of the array since we might modify it during iteration
    TArray<FMCPClientConnection> ConnectionsCopy = ClientConnections;
    
    for (FMCPClientConnection& ClientConnection : ConnectionsCopy)
    {
        if (!ClientConnection.Socket) continue;
        
        // Increment time since last activity
        ClientConnection.TimeSinceLastActivity += DeltaTime;
        
        // Check if client has timed out
        if (ClientConnection.TimeSinceLastActivity > Config.ClientTimeoutSeconds)
        {
            MCP_LOG_WARNING("Client from %s timed out after %.1f seconds of inactivity, disconnecting", 
                *ClientConnection.Endpoint.ToString(), ClientConnection.TimeSinceLastActivity);
            CleanupClientConnection(ClientConnection);
        }
    }
}

void FMCPTCPServer::CleanupAllClientConnections()
{
    MCP_LOG_INFO("Cleaning up all client connections (%d total)", ClientConnections.Num());
    
    // Make a copy of the array since we'll be modifying it during iteration
    TArray<FMCPClientConnection> ConnectionsCopy = ClientConnections;
    
    for (FMCPClientConnection& Connection : ConnectionsCopy)
    {
        CleanupClientConnection(Connection);
    }
    
    // Ensure the array is empty
    ClientConnections.Empty();
}

void FMCPTCPServer::CleanupClientConnection(FSocket* ClientSocket)
{
    if (!ClientSocket) return;
    
    // Find the client connection with this socket
    for (FMCPClientConnection& Connection : ClientConnections)
    {
        if (Connection.Socket == ClientSocket)
        {
            CleanupClientConnection(Connection);
            break;
        }
    }
}

void FMCPTCPServer::CleanupClientConnection(FMCPClientConnection& ClientConnection)
{
    if (!ClientConnection.Socket) return;
    
    MCP_LOG_INFO("Cleaning up client connection from %s", *ClientConnection.Endpoint.ToString());
    
    try
    {
        // Get the socket description before closing
        FString SocketDesc = GetSafeSocketDescription(ClientConnection.Socket);
        MCP_LOG_VERBOSE("Closing client socket with description: %s", *SocketDesc);
        
        // First close the socket
        bool bCloseSuccess = ClientConnection.Socket->Close();
        if (!bCloseSuccess)
        {
            MCP_LOG_ERROR("Failed to close client socket");
        }
        
        // Then destroy it
        ISocketSubsystem* SocketSubsystem = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
        if (SocketSubsystem)
        {
            SocketSubsystem->DestroySocket(ClientConnection.Socket);
            MCP_LOG_VERBOSE("Successfully destroyed client socket");
        }
        else
        {
            MCP_LOG_ERROR("Failed to get socket subsystem when cleaning up client connection");
        }
    }
    catch (const std::exception& Ex)
    {
        MCP_LOG_ERROR("Exception while cleaning up client connection: %s", UTF8_TO_TCHAR(Ex.what()));
    }
    catch (...)
    {
        MCP_LOG_ERROR("Unknown exception while cleaning up client connection");
    }
    
    // Drop any partial-message buffer for this socket so it can't leak across reconnects.
    PartialMessages.Remove(ClientConnection.Socket);

    // Remove from our list of connections
    ClientConnections.RemoveAll([&ClientConnection](const FMCPClientConnection& Connection) {
        return Connection.Socket == ClientConnection.Socket;
    });

    MCP_LOG_INFO("MCP Client disconnected (Remaining clients: %d)", ClientConnections.Num());
}

void FMCPTCPServer::ProcessCommand(const FString& CommandJson, FSocket* ClientSocket)
{
    if (Config.bEnableVerboseLogging)
    {
        MCP_LOG_VERBOSE("Processing command: %s", *CommandJson);
    }
    
    TSharedPtr<FJsonObject> Command;
    TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(CommandJson);
    if (FJsonSerializer::Deserialize(Reader, Command) && Command.IsValid())
    {
        FString Type;
        if (Command->TryGetStringField(FStringView(TEXT("type")), Type))
        {
            TSharedPtr<IMCPCommandHandler> Handler = CommandHandlers.FindRef(Type);
            if (Handler.IsValid())
            {
                MCP_LOG_INFO("Processing command: %s", *Type);
                
                const TSharedPtr<FJsonObject>* ParamsPtr = nullptr;
                TSharedPtr<FJsonObject> Params = MakeShared<FJsonObject>();
                
                if (Command->TryGetObjectField(FStringView(TEXT("params")), ParamsPtr) && ParamsPtr != nullptr)
                {
                    Params = *ParamsPtr;
                }
                
                // Handle the command and get the response
                TSharedPtr<FJsonObject> Response = Handler->Execute(Params, ClientSocket);
                
                // Send the response
                SendResponse(ClientSocket, Response);
            }
            else
            {
                MCP_LOG_WARNING("Unknown command: %s", *Type);
                
                TSharedPtr<FJsonObject> Response = MakeShared<FJsonObject>();
                Response->SetStringField("status", "error");
                Response->SetStringField("message", FString::Printf(TEXT("Unknown command: %s"), *Type));
                SendResponse(ClientSocket, Response);
            }
        }
        else
        {
            MCP_LOG_WARNING("Missing 'type' field in command");
            
            TSharedPtr<FJsonObject> Response = MakeShared<FJsonObject>();
            Response->SetStringField("status", "error");
            Response->SetStringField("message", TEXT("Missing 'type' field"));
            SendResponse(ClientSocket, Response);
        }
    }
    else
    {
        // Log only a bounded prefix — a rejected message can be large (up to the reassembly cap), and
        // dumping the whole payload spams the Output Log (and lets a bad client flood it).
        MCP_LOG_WARNING("Invalid JSON format (%d chars): %s%s", CommandJson.Len(),
            *CommandJson.Left(200), CommandJson.Len() > 200 ? TEXT("...[truncated]") : TEXT(""));
        
        TSharedPtr<FJsonObject> Response = MakeShared<FJsonObject>();
        Response->SetStringField("status", "error");
        Response->SetStringField("message", TEXT("Invalid JSON format"));
        SendResponse(ClientSocket, Response);
    }
    
    // Keep the connection open for future commands
    // Do not close the socket here
}

void FMCPTCPServer::SendResponse(FSocket* Client, const TSharedPtr<FJsonObject>& Response)
{
    if (!Client) return;
    
    FString ResponseStr;
    TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&ResponseStr);
    FJsonSerializer::Serialize(Response.ToSharedRef(), Writer);
    
    if (Config.bEnableVerboseLogging)
    {
        MCP_LOG_VERBOSE("Preparing to send response: %s", *ResponseStr);
    }
    
    FTCHARToUTF8 Converter(*ResponseStr);
    int32 BytesSent = 0;
    int32 TotalBytes = Converter.Length();
    const uint8* Data = (const uint8*)Converter.Get();
    
    // Ensure all data is sent
    while (BytesSent < TotalBytes)
    {
        int32 SentThisTime = 0;
        if (!Client->Send(Data + BytesSent, TotalBytes - BytesSent, SentThisTime))
        {
            MCP_LOG_WARNING("Failed to send response");
            break;
        }
        
        if (SentThisTime <= 0)
        {
            // Would block, try again next tick
            MCP_LOG_VERBOSE("Socket would block, will try again next tick");
            break;
        }
        
        BytesSent += SentThisTime;
        
        if (Config.bEnableVerboseLogging)
        {
            MCP_LOG_VERBOSE("Sent %d/%d bytes", BytesSent, TotalBytes);
        }
    }
    
    if (BytesSent == TotalBytes)
    {
        MCP_LOG_INFO("Successfully sent complete response (%d bytes)", TotalBytes);
    }
    else
    {
        MCP_LOG_WARNING("Only sent %d/%d bytes of response", BytesSent, TotalBytes);
    }
}

FString FMCPTCPServer::GetSafeSocketDescription(FSocket* Socket)
{
    if (!Socket)
    {
        return TEXT("NullSocket");
    }
    
    try
    {
        FString Description = Socket->GetDescription();
        
        // Check if the description contains any non-ASCII characters
        bool bHasNonAscii = false;
        for (TCHAR Char : Description)
        {
            if (Char > 127)
            {
                bHasNonAscii = true;
                break;
            }
        }
        
        if (bHasNonAscii)
        {
            // Return a safe description instead
            return TEXT("Socket_") + FString::FromInt(reinterpret_cast<uint64>(Socket));
        }
        
        return Description;
    }
    catch (...)
    {
        // If there's any exception, return a safe description
        return TEXT("Socket_") + FString::FromInt(reinterpret_cast<uint64>(Socket));
    }
} 