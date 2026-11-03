import Foundation

enum ConnectionKind: String, CaseIterable, Identifiable, Codable {
    case tools = "Tools"
    case mcp = "MCP"
    case api = "API"

    var id: String { rawValue }
    var selectionID: String { "__connections_\(rawValue.lowercased())" }

    static func from(selectionID: String?) -> ConnectionKind? {
        allCases.first { $0.selectionID == selectionID }
    }
}

enum ConnectionState: String, Codable {
    case checking = "Checking"
    case active = "Active"
    case inactive = "Inactive"
}

struct ManagedConnection: Identifiable, Codable, Equatable {
    var id: String
    var name: String
    var target: String
    var detail: String
    var kind: ConnectionKind
    var state: ConnectionState
    var tags: [String]
    var isCustom: Bool
    var instructions: String?
    var secretName: String?
    var repositoryPath: String?
    /// Nil means the resource belongs to the shared, agent-neutral environment.
    /// A value means the resource exists only inside that agent's private configuration.
    var agentID: String?

    init(id: String = UUID().uuidString, name: String, target: String, detail: String = "",
         kind: ConnectionKind, state: ConnectionState = .checking, tags: [String] = [], isCustom: Bool = true,
         instructions: String? = nil, secretName: String? = nil, repositoryPath: String? = nil,
         agentID: String? = nil) {
        self.id = id
        self.name = name
        self.target = target
        self.detail = detail
        self.kind = kind
        self.state = state
        self.tags = tags
        self.isCustom = isCustom
        self.instructions = instructions
        self.secretName = secretName
        self.repositoryPath = repositoryPath
        self.agentID = agentID
    }
}

enum ConnectionAccess {
    static func shared(in connections: [ManagedConnection]) -> [ManagedConnection] {
        connections.filter { $0.agentID == nil }
    }

    static func exclusive(to agentID: String, in connections: [ManagedConnection]) -> [ManagedConnection] {
        connections.filter { $0.agentID?.caseInsensitiveCompare(agentID) == .orderedSame }
    }

    static func promoteUniversalMCPs(in connections: [ManagedConnection], supportedAgentIDs: [String]) -> [ManagedConnection] {
        let supported = Set(supportedAgentIDs.map { $0.lowercased() })
        guard !supported.isEmpty else { return connections }
        let groups = Dictionary(grouping: connections.filter { $0.kind == .mcp && $0.agentID != nil }) {
            $0.name.lowercased()
        }
        var removed = Set<String>()
        var promoted: [ManagedConnection] = []
        for (name, copies) in groups {
            let owners = Set(copies.compactMap { $0.agentID?.lowercased() })
            guard supported.isSubset(of: owners), var shared = copies.sorted(by: { $0.id < $1.id }).first else { continue }
            copies.forEach { removed.insert($0.id) }
            shared.id = "shared:mcp:" + name.replacingOccurrences(of: #"[^a-z0-9_-]+"#, with: "-", options: .regularExpression)
            shared.agentID = nil
            shared.detail = "Available across every installed AI agent"
            shared.tags.removeAll { $0 == "exclusive" || supported.contains($0.lowercased()) }
            shared.tags.append("shared")
            shared.tags = Array(Set(shared.tags)).sorted()
            promoted.append(shared)
        }
        return connections.filter { !removed.contains($0.id) } + promoted
    }
}

enum ConnectionValidator {
    static func basicError(name: String, kind: ConnectionKind, target: String, hasSecret: Bool = false) -> String? {
        if name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "Give the connection a name." }
        let value = target.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.isEmpty { return hasSecret ? nil : "Enter setup instructions, a source, or a token." }
        if value.contains("://"), ConnectionInterpreter.endpoint(in: value) == nil,
           ConnectionInterpreter.githubURL(in: value) == nil { return "The source contains an invalid URL." }
        return nil
    }

    /// Small shell-like tokenizer for commands. It does not execute or expand shell syntax.
    static func commandWords(_ input: String) -> [String] {
        var words: [String] = [], current = ""
        var quote: Character?, escaped = false
        for character in input {
            if escaped { current.append(character); escaped = false; continue }
            if character == "\\" { escaped = true; continue }
            if let q = quote {
                if character == q { quote = nil } else { current.append(character) }
            } else if character == "'" || character == "\"" {
                quote = character
            } else if character.isWhitespace {
                if !current.isEmpty { words.append(current); current = "" }
            } else {
                current.append(character)
            }
        }
        if escaped { current.append("\\") }
        if !current.isEmpty { words.append(current) }
        return words
    }
}

enum ConnectionInterpreter {
    private static func firstMatch(_ pattern: String, in text: String) -> String? {
        guard let range = text.range(of: pattern, options: .regularExpression) else { return nil }
        return String(text[range]).trimmingCharacters(in: CharacterSet(charactersIn: ".,;:!?)\"]}"))
    }

    static func githubURL(in text: String) -> String? {
        firstMatch(#"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?"#, in: text)
    }

    static func endpoint(in text: String) -> String? {
        guard let raw = firstMatch(#"https?://[^\s<>]+"#, in: text),
              let url = URL(string: raw), url.host != nil else { return nil }
        return raw
    }

    static func command(in text: String) -> [String]? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let direct = ConnectionValidator.commandWords(trimmed)
        if !trimmed.contains("://"), let first = direct.first,
           (direct.count == 1 || first.hasPrefix("/") || first.hasPrefix("./") || direct.dropFirst().first?.hasPrefix("-") == true) {
            let proseStarters = ["clone", "install", "connect", "configure", "please", "use"]
            if !proseStarters.contains(first.lowercased()) { return direct }
        }
        let markers = ["npx ", "uvx ", "uv run ", "python3 ", "python ", "node ", "docker ", "bunx ", "deno ", "go run "]
        let lowered = text.lowercased()
        guard let match = markers.compactMap({ marker -> (String.Index, String)? in
            lowered.range(of: marker).map { ($0.lowerBound, marker) }
        }).min(by: { $0.0 < $1.0 }) else { return nil }
        let words = ConnectionValidator.commandWords(String(text[match.0...]))
        return words.isEmpty ? nil : words
    }

    static func secretName(name: String, kind: ConnectionKind, instructions: String) -> String {
        if let explicit = firstMatch(#"\b[A-Z][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD)\b"#, in: instructions) {
            return explicit
        }
        let text = "\(name) \(instructions)".lowercased()
        if text.contains("telegram") { return "TELEGRAM_BOT_TOKEN" }
        if text.contains("github") { return "GITHUB_TOKEN" }
        let base = name.uppercased().replacingOccurrences(of: #"[^A-Z0-9]+"#, with: "_", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "_"))
        return "\(base.isEmpty ? kind.rawValue.uppercased() : base)_TOKEN"
    }
}

enum ConnectionTagger {
    static func tags(name: String, kind: ConnectionKind, target: String, active: Bool) -> [String] {
        let text = "\(name) \(target)".lowercased()
        var tags = [kind == .tools ? "tool" : kind.rawValue.lowercased()]
        tags.append(target.lowercased().hasPrefix("http") ? "remote" : "local")
        tags.append(active ? "active" : "inactive")
        if text.contains("trade") || text.contains("broker") || text.contains("ctrader") { tags.append("trading") }
        if text.contains("telegram") { tags.append("messaging") }
        if text.contains("github") || text.contains("gitlab") { tags.append("developer") }
        if text.contains("database") || text.contains("postgres") || text.contains("mysql") || text.contains("sql") { tags.append("data") }
        if text.contains("oauth") { tags.append("oauth") }
        return Array(Set(tags)).sorted()
    }
}
