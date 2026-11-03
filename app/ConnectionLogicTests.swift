import Foundation

@main
struct ConnectionLogicTests {
    static func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
        guard condition() else {
            FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
            exit(1)
        }
    }

    static func main() {
        expect(ConnectionKind.tools.selectionID == "__connections_tools", "Tools selection ID")
        expect(ConnectionKind.mcp.selectionID == "__connections_mcp", "MCP selection ID")
        expect(ConnectionKind.api.selectionID == "__connections_api", "API selection ID")
        expect(ConnectionKind.from(selectionID: "__connections_mcp") == .mcp, "selection maps to MCP")
        expect(ConnectionKind.from(selectionID: "project") == nil, "project is not a connection selection")

        expect(ConnectionValidator.basicError(name: "", kind: .api, target: "https://example.com") != nil, "name is required")
        expect(ConnectionValidator.basicError(name: "Broker", kind: .api, target: "Clone the broker connector and configure its API") == nil, "natural-language setup instructions accepted")
        expect(ConnectionValidator.basicError(name: "Broker", kind: .api, target: "https://example.com") == nil, "HTTPS API accepted")
        expect(ConnectionValidator.basicError(name: "cTrader", kind: .mcp, target: "npx -y ctrader-mcp") == nil, "MCP command accepted")
        expect(ConnectionValidator.basicError(name: "cTrader", kind: .mcp, target: "https://example.com/mcp") == nil, "MCP URL accepted")
        expect(ConnectionValidator.basicError(name: "CLI", kind: .tools, target: "") != nil, "tool target required")

        let words = ConnectionValidator.commandWords("npx -y '@scope/server' --flag \"hello world\"")
        expect(words == ["npx", "-y", "@scope/server", "--flag", "hello world"], "quoted command parsing")

        let activeTags = ConnectionTagger.tags(name: "cTrader", kind: .mcp, target: "https://broker.example/mcp", active: true)
        expect(activeTags.contains("mcp") && activeTags.contains("remote") && activeTags.contains("active") && activeTags.contains("trading"), "automatic active MCP tags")
        let inactiveTags = ConnectionTagger.tags(name: "Local CLI", kind: .tools, target: "/usr/local/bin/tool", active: false)
        expect(inactiveTags.contains("tool") && inactiveTags.contains("local") && inactiveTags.contains("inactive"), "automatic inactive local tool tags")
        expect(ConnectionTagger.tags(name: "Telegram", kind: .api, target: "https://api.telegram.org", active: true).contains("messaging"), "Telegram should receive messaging tag")

        expect(ConnectionValidator.basicError(name: "Telegram", kind: .api, target: "", hasSecret: true) == nil,
               "a securely supplied token should allow a token-only connection")
        expect(ConnectionInterpreter.githubURL(in: "Clone and configure https://github.com/acme/trader-mcp please") == "https://github.com/acme/trader-mcp",
               "GitHub repository should be extracted from a sentence")
        expect(ConnectionInterpreter.endpoint(in: "Connect this API: https://api.example.com/v2 and test it") == "https://api.example.com/v2",
               "endpoint should be extracted from prose")
        expect(ConnectionInterpreter.command(in: "Install and run npx -y @acme/trader-mcp") == ["npx", "-y", "@acme/trader-mcp"],
               "command should be extracted from setup instructions")
        expect(ConnectionInterpreter.command(in: "ctrader-cli --port 9000") == ["ctrader-cli", "--port", "9000"],
               "direct custom commands should remain supported")
        expect(ConnectionInterpreter.secretName(name: "Telegram Bot", kind: .api, instructions: "bot token") == "TELEGRAM_BOT_TOKEN",
               "known providers should receive a useful secret name")
        expect(ConnectionInterpreter.secretName(name: "cTrader", kind: .mcp, instructions: "use access token") == "CTRADER_TOKEN",
               "generic secret names should be stable and provider scoped")
        expect(ConnectionInterpreter.secretName(name: "Custom", kind: .mcp, instructions: "export BROKER_ACCESS_TOKEN for the server") == "BROKER_ACCESS_TOKEN",
               "explicit environment variable names should be preserved")

        let oldJSON = Data(#"{"id":"old","name":"Legacy","target":"tool","detail":"","kind":"Tools","state":"Active","tags":[],"isCustom":true}"#.utf8)
        expect((try? JSONDecoder().decode(ManagedConnection.self, from: oldJSON)) != nil,
               "connections saved by GRC 0.8 should remain readable")
        let safeItem = ManagedConnection(name: "Safe", target: "https://example.com", kind: .api, secretName: "SAFE_TOKEN")
        let encoded = String(data: try! JSONEncoder().encode(safeItem), encoding: .utf8)!
        expect(!encoded.contains("super-secret-value"), "credential values must never be serialized")

        let shared = ManagedConnection(id: "shared", name: "Shared API", target: "https://example.com", kind: .api)
        let hermesOnly = ManagedConnection(id: "hermes-only", name: "Hermes MCP", target: "server", kind: .mcp, agentID: "hermes")
        let codexOnly = ManagedConnection(id: "codex-only", name: "Codex MCP", target: "server", kind: .mcp, agentID: "codex")
        let resources = [shared, hermesOnly, codexOnly]
        expect(ConnectionAccess.shared(in: resources).map(\.id) == ["shared"],
               "the general connection pool should contain only agent-neutral resources")
        expect(ConnectionAccess.exclusive(to: "hermes", in: resources).map(\.id) == ["hermes-only"],
               "agent cards should contain that agent's exclusive resources")
        expect(ConnectionAccess.exclusive(to: "claude", in: resources).isEmpty,
               "resources owned by another agent must not leak into an agent card")
        let copies = [
            ManagedConnection(id: "h", name: "GRC", target: "server", kind: .mcp, isCustom: false, agentID: "hermes"),
            ManagedConnection(id: "c", name: "GRC", target: "server", kind: .mcp, isCustom: false, agentID: "claude"),
            ManagedConnection(id: "x", name: "GRC", target: "server", kind: .mcp, isCustom: false, agentID: "codex")
        ]
        let promoted = ConnectionAccess.promoteUniversalMCPs(in: copies, supportedAgentIDs: ["hermes", "claude", "codex"])
        expect(promoted.count == 1 && promoted[0].agentID == nil,
               "an MCP configured for every supported agent should be promoted to the shared pool")

        print("ConnectionLogicTests: PASS")
    }
}
