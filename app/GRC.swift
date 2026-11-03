// GRC.app
//
// Drag a folder onto the sidebar to add a project, or drag files onto a
// dashboard to pin them as context. grc.py (copied into Resources) does the
// actual work. The app just runs it and shows the result.

import AppKit
import CoreServices
import ServiceManagement
import SwiftUI
import Security
import UniformTypeIdentifiers
import UserNotifications
import WebKit

// MARK: - Model

struct Project: Codable, Identifiable, Hashable {
    var path: String
    var id: String { path }
    var name: String { (path as NSString).lastPathComponent }
    var url: URL { URL(fileURLWithPath: path) }

    /// Where grc.py writes this project's page (project.json "dashboard").
    var dashboard: URL {
        let meta = url.appendingPathComponent(".grc/project.json")
        if let data = try? Data(contentsOf: meta),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let rel = json["dashboard"] as? String {
            return url.appendingPathComponent(rel)
        }
        return url.appendingPathComponent(".grc/dashboard.html")
    }

    /// Overall % and status for the sidebar, preferring a resolved shared task source.
    var summary: (percent: Int, open: Int)? {
        let resolved = url.appendingPathComponent(".grc/resolved-tasks.json")
        let local = url.appendingPathComponent(".grc/tasks.json")
        let file = FileManager.default.fileExists(atPath: resolved.path) ? resolved : local
        guard let data = try? Data(contentsOf: file),
              let tasks = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]],
              !tasks.isEmpty else { return nil }
        var total = 0, open = 0
        for t in tasks {
            let status = t["status"] as? String ?? "todo"
            if status != "done" { open += 1 }
            if let subs = t["subtasks"] as? [[String: Any]], !subs.isEmpty {
                total += 100 * subs.filter { ($0["done"] as? Bool) == true }.count / subs.count
            } else {
                total += status == "done" ? 100 : (t["percent"] as? Int ?? 0)
            }
        }
        return (total / tasks.count, open)
    }
}

// MARK: - AI agents available on this Mac

struct Agent: Identifiable {
    let id: String
    let name: String
    let detail: String
    let ready: Bool
    let path: String
    let icon: NSImage?
    let symbol: String
    var models: [String] = []
}

enum AgentScanner {
    struct Spec {
        let id, name, binary, symbol: String
        let icons: [String]
    }

    static let home = NSHomeDirectory()
    // Finder-launched apps get a minimal PATH, so check the common install dirs too.
    static let dirs = ["\(home)/.local/bin", "/usr/local/bin", "/opt/homebrew/bin", "\(home)/.npm-global/bin",
                       "\(home)/.claude/local", "\(home)/.bun/bin", "\(home)/.cargo/bin", "/usr/bin"]
    static let specs: [Spec] = [
        Spec(id: "claude", name: "Claude Code", binary: "claude", symbol: "sparkle", icons: ["/Applications/Claude.app"]),
        Spec(id: "codex", name: "Codex", binary: "codex", symbol: "chevron.left.forwardslash.chevron.right",
             icons: ["/Applications/Codex.app", "/Applications/ChatGPT.app", "/Applications/ChatGPT Classic.app"]),
        Spec(id: "ollama", name: "Ollama", binary: "ollama", symbol: "cpu", icons: ["/Applications/Ollama.app"]),
        Spec(id: "hermes", name: "Hermes", binary: "hermes", symbol: "bolt.horizontal",
             icons: ["\(home)/.hermes/hermes-agent/apps/desktop/assets/icon.png"]),
        Spec(id: "gemini", name: "Gemini CLI", binary: "gemini", symbol: "sparkles", icons: []),
        Spec(id: "cursor", name: "Cursor Agent", binary: "cursor-agent", symbol: "cursorarrow.rays", icons: ["/Applications/Cursor.app"]),
        Spec(id: "opencode", name: "OpenCode", binary: "opencode", symbol: "terminal", icons: []),
        Spec(id: "aider", name: "Aider", binary: "aider", symbol: "terminal", icons: []),
        Spec(id: "goose", name: "Goose", binary: "goose", symbol: "terminal", icons: []),
    ]

    static func find(_ binary: String) -> String? {
        dirs.map { "\($0)/\(binary)" }.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    static func output(_ path: String, _ args: [String], timeout: TimeInterval = 6, input: String? = nil) -> String? {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: path)
        proc.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = (dirs + ["/bin", "/usr/sbin", "/sbin"]).joined(separator: ":")
        proc.environment = env
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = out
        var stdinPipe: Pipe?
        if input != nil {
            stdinPipe = Pipe()
            proc.standardInput = stdinPipe
        }
        do { try proc.run() } catch { return nil }
        if let input, let stdinPipe {
            stdinPipe.fileHandleForWriting.write(Data(input.utf8))
            try? stdinPipe.fileHandleForWriting.close()
        }
        let deadline = Date().addingTimeInterval(timeout)
        while proc.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if proc.isRunning { proc.terminate(); return nil }
        return String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)
    }

    static func version(_ text: String?) -> String? {
        guard let text, let r = text.range(of: #"\d+\.\d+(\.\d+)?"#, options: .regularExpression) else { return nil }
        return "v" + text[r]
    }

    static func icon(_ spec: Spec) -> NSImage? {
        for path in spec.icons where FileManager.default.fileExists(atPath: path) {
            return path.hasSuffix(".app") ? NSWorkspace.shared.icon(forFile: path) : NSImage(contentsOfFile: path)
        }
        return nil
    }

    static func ollamaModels() -> [String]? {
        guard let url = URL(string: "http://127.0.0.1:11434/api/tags") else { return nil }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        let done = DispatchSemaphore(value: 0)
        var models: [String]?
        URLSession.shared.dataTask(with: request) { data, _, _ in
            if let data, let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let list = json["models"] as? [[String: Any]] {
                models = list.compactMap { $0["name"] as? String }.map { $0.replacingOccurrences(of: ":latest", with: "") }
            }
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 3)
        return models
    }

    static func scan() -> [Agent] {
        specs.compactMap { spec in
            guard let path = find(spec.binary) else { return nil }
            let v = version(output(path, ["--version"]))
            var detail = v ?? "installed"
            var ready = true
            var models: [String] = []
            switch spec.id {
            case "ollama":
                if let list = ollamaModels() {
                    models = list
                    detail = "running · \(list.count) local model\(list.count == 1 ? "" : "s")"
                } else {
                    detail = "installed · not running"
                    ready = false
                }
            case "codex":
                let signedIn = FileManager.default.fileExists(atPath: "\(home)/.codex/auth.json")
                detail = [v, signedIn ? "signed in" : "not signed in"].compactMap { $0 }.joined(separator: " · ")
                ready = signedIn
            default:
                break
            }
            return Agent(id: spec.id, name: spec.name, detail: detail, ready: ready, path: path,
                         icon: icon(spec), symbol: spec.symbol, models: models)
        }
    }
}

/// Reads Hermes' live configuration without reading or displaying secret values.
enum ConnectionScanner {
    static func scan() -> [ManagedConnection] {
        var found: [ManagedConnection] = []

        if let hermes = AgentScanner.find("hermes") {
        if let text = AgentScanner.output(hermes, ["tools", "list"], timeout: 12) {
            for raw in text.split(separator: "\n") {
                let line = raw.trimmingCharacters(in: .whitespaces)
                guard line.hasPrefix("✓ enabled") else { continue }
                let rest = line.dropFirst("✓ enabled".count).trimmingCharacters(in: .whitespaces)
                let parts = rest.split(maxSplits: 1, whereSeparator: { $0.isWhitespace })
                guard let first = parts.first else { continue }
                let name = String(first)
                let detail = parts.count > 1 ? String(parts[1]) : "Enabled in Hermes"
                found.append(ManagedConnection(id: "hermes:tool:\(name)", name: name, target: name, detail: detail,
                                               kind: .tools, state: .active, isCustom: false, agentID: "hermes"))
            }
        }

        if let text = AgentScanner.output(hermes, ["mcp", "list"], timeout: 15) {
            for raw in text.split(separator: "\n") {
                let line = raw.trimmingCharacters(in: .whitespaces)
                guard line.contains("✓ enabled"), !line.hasPrefix("Name") else { continue }
                let parts = line.split(whereSeparator: { $0.isWhitespace })
                guard let first = parts.first else { continue }
                let name = String(first)
                let detail = line.replacingOccurrences(of: "✓ enabled", with: "")
                    .dropFirst(name.count).trimmingCharacters(in: .whitespaces)
                found.append(ManagedConnection(id: "hermes:mcp:\(name)", name: name, target: detail,
                                               detail: detail.isEmpty ? "Connected through Hermes" : detail,
                                               kind: .mcp, state: .active, isCustom: false, agentID: "hermes"))
            }
        }

        if let text = AgentScanner.output(hermes, ["status"], timeout: 20) {
            var section = ""
            for raw in text.split(separator: "\n") {
                let line = raw.trimmingCharacters(in: .whitespaces)
                if line.hasPrefix("◆ ") { section = String(line.dropFirst(2)); continue }
                guard line.contains("✓"), ["API Keys", "Auth Providers", "API-Key Providers", "Messaging Platforms"].contains(section) else { continue }
                let clean = line.replacingOccurrences(of: "✓", with: "").trimmingCharacters(in: .whitespaces)
                let name = clean.components(separatedBy: "  ").first?.trimmingCharacters(in: .whitespaces) ?? clean
                guard !name.isEmpty else { continue }
                found.append(ManagedConnection(id: "hermes:api:\(section):\(name)", name: name, target: section,
                                               detail: section == "Messaging Platforms" ? "Messaging connection" : section,
                                               kind: .api, state: .active, isCustom: false, agentID: "hermes"))
            }
        }
        }
        if let claude = AgentScanner.find("claude"),
           let text = AgentScanner.output(claude, ["mcp", "list"], timeout: 15) {
            for raw in text.split(separator: "\n") {
                let line = raw.trimmingCharacters(in: .whitespaces)
                guard let colon = line.range(of: ": "), line.contains(" - ") else { continue }
                let name = String(line[..<colon.lowerBound])
                let remainder = String(line[colon.upperBound...])
                let target = remainder.components(separatedBy: " - ").first ?? remainder
                let active = line.contains("Connected")
                guard !name.isEmpty else { continue }
                found.append(ManagedConnection(id: "claude:mcp:\(slug(name))", name: name, target: target,
                                               detail: active ? "Configured in Claude Code" : "Claude Code · authentication needed",
                                               kind: .mcp, state: active ? .active : .inactive, isCustom: false, agentID: "claude"))
            }
        }

        if let codex = AgentScanner.find("codex"),
           let text = AgentScanner.output(codex, ["mcp", "list"], timeout: 15) {
            for raw in text.split(separator: "\n") {
                let line = raw.trimmingCharacters(in: .whitespaces)
                guard !line.isEmpty, !line.hasPrefix("Name"),
                      line.range(of: #"[A-Za-z0-9]"#, options: .regularExpression) != nil else { continue }
                let parts = line.split(whereSeparator: { $0.isWhitespace })
                guard parts.count >= 2 else { continue }
                let name = String(parts[0])
                let active = line.lowercased().contains("enabled")
                found.append(ManagedConnection(id: "codex:mcp:\(slug(name))", name: name, target: String(parts[1]),
                                               detail: active ? "Configured in Codex" : "Codex · unavailable",
                                               kind: .mcp, state: active ? .active : .inactive, isCustom: false, agentID: "codex"))
            }
        }

        for index in found.indices {
            found[index].tags = ConnectionTagger.tags(name: found[index].name, kind: found[index].kind,
                                                       target: found[index].target, active: found[index].state == .active)
            found[index].tags.append("exclusive")
            if let agentID = found[index].agentID { found[index].tags.append(agentID) }
            found[index].tags = Array(Set(found[index].tags)).sorted()
        }
        let supportedMCPAgents = ["hermes", "claude", "codex"].filter { AgentScanner.find($0) != nil }
        return ConnectionAccess.promoteUniversalMCPs(in: found, supportedAgentIDs: supportedMCPAgents)
    }

    private static func slug(_ value: String) -> String {
        value.lowercased().replacingOccurrences(of: #"[^a-z0-9_-]+"#, with: "-", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "-"))
    }
}

struct ServerLaunch {
    let command: String
    let expectedURL: URL?
}

/// Convention-based launcher. Projects can override detection with .grc/run.json.
enum ServerLauncher {
    static func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    static func launch(for project: Project) -> ServerLaunch? {
        let fm = FileManager.default
        let root = project.url
        let config = root.appendingPathComponent(".grc/run.json")
        if let data = try? Data(contentsOf: config),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let command = json["command"] as? String, !command.isEmpty {
            return ServerLaunch(command: command, expectedURL: (json["url"] as? String).flatMap(URL.init(string:)))
        }

        let package = root.appendingPathComponent("package.json")
        if let data = try? Data(contentsOf: package),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let scripts = json["scripts"] as? [String: Any],
           let script = ["dev", "start", "serve"].first(where: { scripts[$0] is String }) {
            let runner: String
            if fm.fileExists(atPath: root.appendingPathComponent("pnpm-lock.yaml").path) { runner = "pnpm" }
            else if fm.fileExists(atPath: root.appendingPathComponent("yarn.lock").path) { runner = "yarn" }
            else { runner = "npm run" }
            return ServerLaunch(command: "\(runner) \(script)", expectedURL: nil)
        }
        if fm.fileExists(atPath: root.appendingPathComponent("manage.py").path) {
            return ServerLaunch(command: "python3 manage.py runserver 127.0.0.1:8000", expectedURL: URL(string: "http://127.0.0.1:8000"))
        }
        for file in ["app.py", "main.py"] where fm.fileExists(atPath: root.appendingPathComponent(file).path) {
            return ServerLaunch(command: "python3 \(shellQuote(file))", expectedURL: nil)
        }
        if fm.fileExists(atPath: root.appendingPathComponent("index.html").path) {
            return ServerLaunch(command: "python3 -m http.server 8000", expectedURL: URL(string: "http://127.0.0.1:8000"))
        }
        return nil
    }
}

// MARK: - Genie (the local GRC agent)

struct GenieMessage: Identifiable {
    enum Role { case user, assistant, error }
    let id = UUID()
    let role: Role
    var text: String
    var steps: [String] = []
    var changed: [String] = []
    var checkpoint: String?
}

final class GenieChat: ObservableObject {
    @Published var messages: [GenieMessage] = []
    @Published var running = false
    @Published var model = ""
    @Published var step = ""
    let project: Project
    private let python: String
    private let engine: String
    private var process: Process?
    var onChange: (() -> Void)?

    init(project: Project, python: String, engine: String) {
        self.project = project
        self.python = python
        self.engine = engine
        loadHistory()
    }

    private var historyFile: URL { project.url.appendingPathComponent(".grc/genie.jsonl") }

    func loadHistory() {
        guard let text = try? String(contentsOf: historyFile, encoding: .utf8) else { return }
        messages = text.split(separator: "\n").suffix(40).compactMap { line in
            guard let data = line.data(using: .utf8),
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let role = json["role"] as? String, let content = json["content"] as? String else { return nil }
            if role == "user" { return GenieMessage(role: .user, text: content) }
            return GenieMessage(role: .assistant, text: content, changed: json["changed"] as? [String] ?? [],
                                checkpoint: json["checkpoint"] as? String)
        }
        if let m = text.split(separator: "\n").last(where: { $0.contains("\"model\"") }),
           let data = m.data(using: .utf8), let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            model = json["model"] as? String ?? ""
        }
    }

    func clear() {
        let archive = project.url.appendingPathComponent(".grc/genie-\(Int(Date().timeIntervalSince1970)).jsonl")
        try? FileManager.default.moveItem(at: historyFile, to: archive)
        messages = []
    }

    func send(_ question: String) {
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return }
        start(["ask", "--dir", project.path, q], shown: q)
    }

    /// Resume offline from the last handoff. Only called after the user confirms.
    func continueOffline(from agent: String) {
        start(["continue", "--dir", project.path, "--from", agent],
              shown: "Continue offline from where \(agent) stopped.")
    }

    func undo(_ run: String) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: python)
        proc.arguments = [engine, "undo", "--dir", project.path, run]
        let out = Pipe()
        proc.standardOutput = out
        try? proc.run()
        proc.waitUntilExit()
        let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        for i in messages.indices where messages[i].checkpoint == run { messages[i].checkpoint = nil }
        messages.append(GenieMessage(role: .assistant, text: "↩︎ " + text.trimmingCharacters(in: .whitespacesAndNewlines)))
        onChange?()
    }

    private func start(_ args: [String], shown: String) {
        guard !running else { return }
        messages.append(GenieMessage(role: .user, text: shown))
        messages.append(GenieMessage(role: .assistant, text: ""))
        running = true
        step = "Thinking…"

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: python)
        proc.arguments = [engine] + args
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = Pipe()
        var buffer = Data()
        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            guard !chunk.isEmpty else { return }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: 0x0A) {
                let line = buffer.subdata(in: buffer.startIndex..<nl)
                buffer.removeSubrange(buffer.startIndex...nl)
                guard let json = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
                DispatchQueue.main.async { self?.handle(json) }
            }
        }
        proc.terminationHandler = { [weak self] _ in
            out.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                guard let self else { return }
                self.running = false
                self.step = ""
                if let last = self.messages.indices.last, self.messages[last].role == .assistant,
                   self.messages[last].text.isEmpty, self.messages[last].changed.isEmpty {
                    self.messages[last].text = "(no answer)"
                }
                self.onChange?()
            }
        }
        do {
            try proc.run()
            process = proc
        } catch {
            running = false
            messages[messages.count - 1] = GenieMessage(role: .error, text: "Couldn't start Genie: \(error.localizedDescription)")
        }
    }

    func stop() { process?.terminate() }

    private func handle(_ e: [String: Any]) {
        guard let last = messages.indices.last else { return }
        switch e["t"] as? String {
        case "model":
            model = e["name"] as? String ?? model
        case "token":
            messages[last].text += e["text"] as? String ?? ""
            step = "Answering…"
        case "tool":
            let args = e["args"] as? [String: Any] ?? [:]
            let label = Self.describe(e["name"] as? String ?? "", args)
            messages[last].steps.append(label)
            step = label + "…"
        case "changed":
            messages[last].changed = e["items"] as? [String] ?? []
        case "checkpoint":
            messages[last].checkpoint = e["run"] as? String
        case "error":
            messages[last] = GenieMessage(role: .error, text: e["text"] as? String ?? "Something went wrong.")
        default:
            break
        }
    }

    static func describe(_ name: String, _ a: [String: Any]) -> String {
        switch name {
        case "search_files": return "Searched for “\(a["query"] as? String ?? "")”"
        case "read_file": return "Read \(a["path"] as? String ?? "a file")"
        case "list_files": return "Listed \((a["folder"] as? String).flatMap { $0 == "." ? nil : $0 } ?? "the project folder")"
        case "focus": return "Looked at the map around \(a["target"] as? String ?? "")"
        case "add_task": return "Adding a task"
        case "update_task": return "Updating \(a["id"] as? String ?? "a task")"
        case "set_resume": return "Updating the Resume card"
        case "edit_file": return "Editing \(a["path"] as? String ?? "a file")"
        case "write_file": return "Writing \(a["path"] as? String ?? "a file")"
        default: return name
        }
    }
}

/// Pending "continue offline?" prompt.
struct Offer: Identifiable, Equatable {
    let id = UUID().uuidString
    let agent: String
    let project: String
    let reset: String
    var projectName: String { (project as NSString).lastPathComponent }
}

// Secrets are stored in the user Keychain and never serialized into connections.json.
enum ConnectionSecretStore {
    private static let service = "com.nous.grc.connections"

    @discardableResult
    static func save(_ secret: String, for id: String) -> Bool {
        guard let data = secret.data(using: .utf8), !secret.isEmpty else { return false }
        delete(for: id)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: id,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: data
        ]
        return SecItemAdd(query as CFDictionary, nil) == errSecSuccess
    }

    static func read(for id: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: id,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func delete(for id: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: id
        ]
        SecItemDelete(query as CFDictionary)
    }
}

final class Store: ObservableObject {
    @Published var projects: [Project] = []
    @Published var selection: String?
    @Published var busy: Set<String> = []
    @Published var reloadToken: [String: Int] = [:]
    @Published var lastBuilt: [String: Date] = [:]
    @Published var notice: String?
    @Published var agents: [Agent] = []
    @Published var connections: [ManagedConnection] = []
    @Published var runningServers: Set<String> = []
    @Published var serverURLs: [String: URL] = [:]
    @Published var briefing: Set<String> = []
    @Published var offers: [Offer] = []
    var openMainWindow: (() -> Void)?
    private var checkingLimits = false
    private var chats: [String: GenieChat] = [:]
    private var lastBriefTry: [String: Date] = [:]
    @Published var scanningAgents = false

    private var lastAgentScan = Date.distantPast
    private var rerun: Set<String> = []
    private var knownRisks: [String: Set<String>] = [:]
    private var overviewPending: DispatchWorkItem?
    private var serverProcesses: [String: Process] = [:]
    private var serverPipes: [String: Pipe] = [:]
    private var serverBuffers: [String: Data] = [:]
    private var customConnections: [ManagedConnection] = []

    static let overviewID = "__overview__"
    static let support: URL = {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("GRC", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }()
    var overviewFile: URL { Store.support.appendingPathComponent("overview.html") }

    let python = "/usr/bin/python3"
    let engine: String = Bundle.main.path(forResource: "grc", ofType: "py") ?? "\(NSHomeDirectory())/grc/grc.py"

    private var registry: URL { Store.support.appendingPathComponent("projects.json") }
    private var connectionRegistry: URL { Store.support.appendingPathComponent("connections.json") }

    init() {
        if let data = try? Data(contentsOf: connectionRegistry),
           let saved = try? JSONDecoder().decode([ManagedConnection].self, from: data) {
            customConnections = saved
            connections = saved
        }
        if let data = try? Data(contentsOf: registry),
           let saved = try? JSONDecoder().decode([Project].self, from: data) {
            projects = saved.filter { FileManager.default.fileExists(atPath: $0.path) }
        } else if FileManager.default.fileExists(atPath: "\(NSHomeDirectory())/grc/.grc") {
            projects = [Project(path: "\(NSHomeDirectory())/grc")]  // default project on first launch
            save()
        }
        selection = projects.first?.id
        // No file watchers or timers. Refresh happens on: app activation, selecting
        // a project, Rebuild, MCP writes, and the post-commit hook.
        wake()
        NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification,
                                               object: nil, queue: .main) { [weak self] _ in self?.wake() }
    }

    /// Called on app activation. Catch up once, then stay idle.
    func wake() {
        checkLimits()
        if Date().timeIntervalSince(lastAgentScan) > 600 {
            lastAgentScan = Date()
            refreshAgents()
            refreshConnections()
        }
        if let p = projects.first(where: { $0.id == selection }) { refreshIfStale(p) }
    }

    /// Rebuild the selected project if its last build is older than 60s.
    func refreshIfStale(_ project: Project) {
        if let t = lastBuilt[project.id], Date().timeIntervalSince(t) < 60 { return }
        rebuild(project)
    }

    func chat(for project: Project) -> GenieChat {
        if let c = chats[project.id] { return c }
        let c = GenieChat(project: project, python: python, engine: engine)
        c.onChange = { [weak self] in self?.reloadToken[project.id, default: 0] += 1 }
        chats[project.id] = c
        return c
    }

    /// Refresh brief.md through Genie. Skipped if Ollama is down or a chat is open.
    func maybeBrief(_ project: Project, force: Bool = false) {
        guard agents.contains(where: { $0.id == "ollama" && $0.ready }),
              !briefing.contains(project.id), chats[project.id]?.running != true else { return }
        if !force {
            if let t = lastBriefTry[project.id], Date().timeIntervalSince(t) < 900 { return }
            let file = project.url.appendingPathComponent(".grc/brief.md")
            if let m = (try? FileManager.default.attributesOfItem(atPath: file.path))?[.modificationDate] as? Date,
               Date().timeIntervalSince(m) < 86_400 { return }
        }
        lastBriefTry[project.id] = Date()
        briefing.insert(project.id)
        let python = self.python, engine = self.engine
        DispatchQueue.global(qos: .background).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: python)
            proc.arguments = [engine, "brief", "--dir", project.path]
            proc.standardOutput = Pipe()
            proc.standardError = Pipe()
            try? proc.run()
            proc.waitUntilExit()
            DispatchQueue.main.async {
                self.briefing.remove(project.id)
                self.reloadToken[project.id, default: 0] += 1
            }
        }
    }

    // MARK: auto-continue

    /// Look for agents that hit a usage limit in a registered project.
    /// The handoff file is written right away (it only touches .grc/), then the user is asked.
    func checkLimits() {
        guard !checkingLimits, !projects.isEmpty else { return }
        checkingLimits = true
        let python = self.python, engine = self.engine, paths = projects.map(\.path)
        DispatchQueue.global(qos: .utility).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: python)
            proc.arguments = [engine, "limits"] + paths
            let out = Pipe()
            proc.standardOutput = out
            proc.standardError = Pipe()
            try? proc.run()
            let data = out.fileHandleForReading.readDataToEndOfFile()
            proc.waitUntilExit()
            let hits = String(data: data, encoding: .utf8)?.split(separator: "\n").compactMap {
                try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any]
            } ?? []
            DispatchQueue.main.async {
                self.checkingLimits = false
                for hit in hits {
                    guard let agent = hit["agent"] as? String, let project = hit["project"] as? String else { continue }
                    let offer = Offer(agent: agent, project: project, reset: hit["reset"] as? String ?? "")
                    self.offers.append(offer)
                    if let p = self.projects.first(where: { $0.path == project }) { self.reloadToken[p.id, default: 0] += 1 }
                    self.notify(offer)
                }
                if !hits.isEmpty { NSApp.requestUserAttention(.criticalRequest) }
            }
        }
    }

    private func notify(_ offer: Offer) {
        let content = UNMutableNotificationContent()
        content.title = "\(offer.agent) ran out of usage"
        content.body = "On \(offer.projectName). GRC saved a handoff. Continue now with the offline model?"
            + (offer.reset.isEmpty ? "" : " (\(offer.reset))")
        content.sound = .default
        content.categoryIdentifier = "GRC_LIMIT"
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: offer.id, content: content, trigger: nil))
    }

    /// Handles the reply. The offline worker only starts on yes.
    func respond(to id: String, yes: Bool) {
        guard let offer = offers.first(where: { $0.id == id }) else { return }
        offers.removeAll { $0.id == id }
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [id])
        guard yes, let project = projects.first(where: { $0.path == offer.project }) else { return }
        selection = project.id
        UserDefaults.standard.set(true, forKey: "genieOpen")
        openMainWindow?()
        NSApp.activate(ignoringOtherApps: true)
        let start = { self.chat(for: project).continueOffline(from: offer.agent) }
        if agents.contains(where: { $0.id == "ollama" && $0.ready }) {
            start()
        } else {
            NSWorkspace.shared.open(URL(fileURLWithPath: "/Applications/Ollama.app"))
            notice = "Starting Ollama first…"
            DispatchQueue.main.asyncAfter(deadline: .now() + 6) { self.refreshAgents(); start() }
        }
    }

    func refreshAgents() {
        guard !scanningAgents else { return }
        scanningAgents = true
        DispatchQueue.global(qos: .utility).async {
            let found = AgentScanner.scan()
            DispatchQueue.main.async {
                self.agents = found
                self.scanningAgents = false
            }
        }
    }

    func refreshConnections() {
        DispatchQueue.global(qos: .utility).async {
            let found = ConnectionScanner.scan()
            DispatchQueue.main.async { self.mergeConnections(found) }
        }
    }

    func addConnection(name: String, instructions: String, secret: String, kind: ConnectionKind) {
        let cleanName = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanInstructions = instructions.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanSecret = secret
        if let error = ConnectionValidator.basicError(name: cleanName, kind: kind,
                                                       target: cleanInstructions, hasSecret: !cleanSecret.isEmpty) {
            notice = error
            return
        }

        let repo = ConnectionInterpreter.githubURL(in: cleanInstructions)
        let command = ConnectionInterpreter.command(in: cleanInstructions)?.joined(separator: " ")
        let endpoint = ConnectionInterpreter.endpoint(in: cleanInstructions)
        var resolved = command ?? (repo ?? endpoint ?? "")
        let lowerName = cleanName.lowercased()
        if resolved.isEmpty && kind == .api && lowerName.contains("telegram") { resolved = "https://api.telegram.org" }
        if resolved.isEmpty && kind == .api && lowerName.contains("github") { resolved = "https://api.github.com/user" }

        var item = ManagedConnection(name: cleanName, target: resolved,
                                     detail: repo == nil ? "The local agent is checking this connection…" : "Cloning and inspecting the repository…",
                                     kind: kind, instructions: cleanInstructions.isEmpty ? nil : cleanInstructions,
                                     secretName: cleanSecret.isEmpty ? nil : ConnectionInterpreter.secretName(name: cleanName, kind: kind, instructions: cleanInstructions))
        item.tags = ConnectionTagger.tags(name: cleanName, kind: kind, target: cleanInstructions, active: false)
        item.tags.append("shared")
        if !cleanSecret.isEmpty && !ConnectionSecretStore.save(cleanSecret, for: item.id) {
            notice = "The token could not be saved in Keychain. Nothing was added."
            return
        }
        if repo == nil, !cleanSecret.isEmpty, kind != .api,
           let words = ConnectionInterpreter.command(in: cleanInstructions),
           let wrapper = installSecretWrapper(for: item, words: words) {
            item.target = wrapper
        }
        customConnections.append(item)
        saveConnections()
        mergeConnections(connections.filter { !$0.isCustom })
        if let repo { prepareRepository(item.id, repositoryURL: repo) }
        else { validateConnection(item.id, configureMCP: kind == .mcp) }
        notice = "Added \(cleanName). The local agent is preparing and checking it…"
    }

    private func installSecretWrapper(for item: ManagedConnection, words: [String]) -> String? {
        guard let variable = item.secretName, !words.isEmpty else { return nil }
        let folder = Store.support.appendingPathComponent("ConnectionWrappers", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let file = folder.appendingPathComponent(item.id)
        let command = words.map(ServerLauncher.shellQuote).joined(separator: " ")
        let account = ServerLauncher.shellQuote(item.id)
        let script = """
        #!/bin/zsh
        set -e
        GRC_CONNECTION_SECRET=$(/usr/bin/security find-generic-password -s com.nous.grc.connections -a \(account) -w)
        export \(variable)="$GRC_CONNECTION_SECRET"
        exec \(command)
        """
        do {
            try script.write(to: file, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: file.path)
            return file.path
        } catch { return nil }
    }

    private func prepareRepository(_ id: String, repositoryURL: String) {
        let folder = Store.support.appendingPathComponent("Connections", isDirectory: true).appendingPathComponent(id, isDirectory: true)
        let item = customConnections.first { $0.id == id }
        DispatchQueue.global(qos: .utility).async {
            try? FileManager.default.createDirectory(at: folder.deletingLastPathComponent(), withIntermediateDirectories: true)
            if !FileManager.default.fileExists(atPath: folder.path) {
                let process = Process()
                process.executableURL = URL(fileURLWithPath: "/usr/bin/git")
                process.arguments = ["clone", "--depth", "1", "--", repositoryURL, folder.path]
                process.standardOutput = Pipe()
                process.standardError = Pipe()
                try? process.run()
                process.waitUntilExit()
            }
            let cloned = FileManager.default.fileExists(atPath: folder.appendingPathComponent(".git").path)
            DispatchQueue.main.async {
                guard let index = self.customConnections.firstIndex(where: { $0.id == id }) else { return }
                self.customConnections[index].repositoryPath = cloned ? folder.path : nil
                let explicit = item?.instructions.flatMap { ConnectionInterpreter.command(in: $0) }
                if cloned, let explicit, !explicit.isEmpty {
                    let command = explicit.map(ServerLauncher.shellQuote).joined(separator: " ")
                    let script = "cd \(ServerLauncher.shellQuote(folder.path)) && exec \(command)"
                    self.customConnections[index].target = "/bin/zsh -lc \(ServerLauncher.shellQuote(script))"
                    if self.customConnections[index].secretName != nil {
                        let configured = self.customConnections[index]
                        let words = ConnectionValidator.commandWords(configured.target)
                        if let wrapper = self.installSecretWrapper(for: configured, words: words) {
                            self.customConnections[index].target = wrapper
                        }
                    }
                    self.customConnections[index].detail = "Repository cloned. Checking the supplied launch command…"
                    self.saveConnections()
                    self.mergeConnections(self.connections.filter { !$0.isCustom })
                    self.validateConnection(id, configureMCP: self.customConnections[index].kind == .mcp)
                } else {
                    let detail = cloned ? "Repository cloned safely. Add a launch command to the setup instructions to activate it." : "The repository could not be cloned."
                    self.updateConnection(id, state: .inactive, detail: detail)
                }
            }
        }
    }

    func validateConnection(_ id: String, configureMCP: Bool = false) {
        guard let item = customConnections.first(where: { $0.id == id }) else { return }
        updateConnection(id, state: .checking, detail: "The local agent is checking this connection…")
        switch item.kind {
        case .api:
            let secret = ConnectionSecretStore.read(for: item.id)
            var endpoint = item.target
            if item.name.lowercased().contains("telegram"), let secret {
                endpoint = "https://api.telegram.org/bot\(secret)/getMe"
            }
            guard let url = URL(string: endpoint), url.scheme?.hasPrefix("http") == true else {
                finishValidation(item, active: false, detail: secret == nil ? "No API endpoint could be inferred." : "Token saved securely; add service or endpoint instructions to validate it.")
                return
            }
            var request = URLRequest(url: url, timeoutInterval: 12)
            request.httpMethod = secret == nil ? "HEAD" : "GET"
            if let secret, !item.name.lowercased().contains("telegram") {
                let instructions = item.instructions?.lowercased() ?? ""
                if instructions.contains("x-api-key") || instructions.contains("api key header") {
                    request.setValue(secret, forHTTPHeaderField: "X-API-Key")
                } else {
                    request.setValue(secret.lowercased().hasPrefix("bearer ") ? secret : "Bearer \(secret)", forHTTPHeaderField: "Authorization")
                }
            }
            URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
                let code = (response as? HTTPURLResponse)?.statusCode
                let active = code.map { secret == nil ? (100..<500).contains($0) : (200..<400).contains($0) } ?? false
                let prefix = secret == nil ? "Reachable" : "Authenticated check"
                let detail = code.map { "\(prefix) · HTTP \($0)" } ?? (error?.localizedDescription ?? "No response")
                DispatchQueue.main.async { self?.finishValidation(item, active: active, detail: detail) }
            }.resume()
        case .tools:
            DispatchQueue.global(qos: .utility).async {
                let words = ConnectionValidator.commandWords(item.target)
                let executable = words.first ?? ""
                let path = executable.hasPrefix("/") && FileManager.default.isExecutableFile(atPath: executable)
                    ? executable : AgentScanner.find(executable)
                DispatchQueue.main.async {
                    self.finishValidation(item, active: path != nil,
                                          detail: path.map { "Executable found · \($0)" } ?? "Executable not found")
                }
            }
        case .mcp:
            DispatchQueue.global(qos: .utility).async {
                let result = self.configureSharedMCP(item, install: configureMCP)
                let active = result.total > 0 && result.ready == result.total
                let names = result.agents.joined(separator: ", ")
                let detail = result.total == 0 ? "No supported AI agent is installed" :
                    "Shared with \(result.ready)/\(result.total) installed agents\(names.isEmpty ? "" : " · \(names)")"
                DispatchQueue.main.async {
                    self.finishValidation(item, active: active, detail: detail)
                    self.refreshConnections()
                }
            }
        }
    }

    private func configureSharedMCP(_ item: ManagedConnection, install: Bool) -> (ready: Int, total: Int, agents: [String]) {
        let key = mcpKey(item.name)
        let remote = item.target.lowercased().hasPrefix("http")
        let words = ConnectionValidator.commandWords(item.target)
        var total = 0, ready = 0
        var agents: [String] = []

        if let hermes = AgentScanner.find("hermes") {
            total += 1
            if install {
                _ = AgentScanner.output(hermes, ["mcp", "remove", key], timeout: 15)
                var args = ["mcp", "add", key]
                if remote { args += ["--url", item.target] }
                else if let command = words.first {
                    args += ["--command", command]
                    if words.count > 1 { args += ["--args"] + words.dropFirst() }
                }
                _ = AgentScanner.output(hermes, args, timeout: 30, input: "\n")
            }
            let check = AgentScanner.output(hermes, ["mcp", "test", key], timeout: 30) ?? ""
            if check.contains("✓ Connected") { ready += 1; agents.append("Hermes") }
        }

        if let claude = AgentScanner.find("claude") {
            total += 1
            if install {
                _ = AgentScanner.output(claude, ["mcp", "remove", "--scope", "user", key], timeout: 15)
                var args = ["mcp", "add", "--scope", "user"]
                if remote { args += ["--transport", "http", key, item.target] }
                else { args += [key, "--"] + words }
                _ = AgentScanner.output(claude, args, timeout: 30)
            }
            let check = AgentScanner.output(claude, ["mcp", "get", key], timeout: 30) ?? ""
            if check.contains("Connected") { ready += 1; agents.append("Claude Code") }
        }

        if let codex = AgentScanner.find("codex") {
            total += 1
            if install {
                _ = AgentScanner.output(codex, ["mcp", "remove", key], timeout: 15)
                let args = remote ? ["mcp", "add", key, "--url", item.target] : ["mcp", "add", key, "--"] + words
                _ = AgentScanner.output(codex, args, timeout: 30)
            }
            let check = AgentScanner.output(codex, ["mcp", "get", key, "--json"], timeout: 15) ?? ""
            if check.contains("\"name\"") || check.contains("\"transport\"") {
                ready += 1; agents.append("Codex")
            }
        }
        return (ready, total, agents)
    }

    private func removeSharedMCP(_ name: String) {
        let key = mcpKey(name)
        if let hermes = AgentScanner.find("hermes") { _ = AgentScanner.output(hermes, ["mcp", "remove", key], timeout: 15) }
        if let claude = AgentScanner.find("claude") { _ = AgentScanner.output(claude, ["mcp", "remove", "--scope", "user", key], timeout: 15) }
        if let codex = AgentScanner.find("codex") { _ = AgentScanner.output(codex, ["mcp", "remove", key], timeout: 15) }
    }

    func removeConnection(_ item: ManagedConnection) {
        guard item.isCustom else { return }
        ConnectionSecretStore.delete(for: item.id)
        try? FileManager.default.removeItem(at: Store.support.appendingPathComponent("ConnectionWrappers").appendingPathComponent(item.id))
        if let repositoryPath = item.repositoryPath { try? FileManager.default.removeItem(atPath: repositoryPath) }
        customConnections.removeAll { $0.id == item.id }
        saveConnections()
        mergeConnections(connections.filter { !$0.isCustom })
        if item.kind == .mcp {
            DispatchQueue.global(qos: .utility).async {
                self.removeSharedMCP(item.name)
                DispatchQueue.main.async { self.refreshConnections() }
            }
        }
        notice = "Removed \(item.name)."
    }

    private func mcpKey(_ name: String) -> String {
        let lowered = name.lowercased().replacingOccurrences(of: #"[^a-z0-9_-]+"#, with: "-", options: .regularExpression)
        return lowered.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
    }

    private func finishValidation(_ item: ManagedConnection, active: Bool, detail: String) {
        updateConnection(item.id, state: active ? .active : .inactive, detail: detail)
    }

    private func updateConnection(_ id: String, state: ConnectionState, detail: String) {
        guard let index = customConnections.firstIndex(where: { $0.id == id }) else { return }
        customConnections[index].state = state
        customConnections[index].detail = detail
        customConnections[index].tags = ConnectionTagger.tags(name: customConnections[index].name,
                                                                kind: customConnections[index].kind,
                                                                target: customConnections[index].target,
                                                                active: state == .active)
        customConnections[index].tags.append("shared")
        saveConnections()
        mergeConnections(connections.filter { !$0.isCustom })
    }

    private func saveConnections() {
        if let data = try? JSONEncoder().encode(customConnections) { try? data.write(to: connectionRegistry) }
    }

    private func mergeConnections(_ discovered: [ManagedConnection]) {
        let filtered = discovered.filter { found in
            !customConnections.contains { custom in
                custom.kind == found.kind && (custom.name.caseInsensitiveCompare(found.name) == .orderedSame ||
                                              (custom.kind == .mcp && mcpKey(custom.name) == found.name.lowercased()))
            }
        }
        connections = (customConnections + filtered).sorted {
            if $0.kind.rawValue != $1.kind.rawValue { return $0.kind.rawValue < $1.kind.rawValue }
            return $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
    }

    func toggleServer(_ project: Project) {
        if let process = serverProcesses[project.id], process.isRunning {
            process.terminate()
            notice = "Stopping \(project.name)…"
            return
        }
        guard let launch = ServerLauncher.launch(for: project) else {
            notice = "No server command found for \(project.name). Add .grc/run.json with a command and URL."
            return
        }
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-lc", "cd \(ServerLauncher.shellQuote(project.path)) && exec \(launch.command)"]
        process.standardOutput = pipe
        process.standardError = pipe
        serverBuffers[project.id] = Data()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            DispatchQueue.main.async { self?.consumeServerOutput(data, project: project) }
        }
        process.terminationHandler = { [weak self] process in
            pipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                self?.runningServers.remove(project.id)
                self?.serverProcesses[project.id] = nil
                self?.serverPipes[project.id] = nil
                if process.terminationStatus != 0 && process.terminationReason == .exit {
                    self?.notice = "\(project.name) server exited with code \(process.terminationStatus)."
                }
            }
        }
        do {
            try process.run()
            serverProcesses[project.id] = process
            serverPipes[project.id] = pipe
            runningServers.insert(project.id)
            if let url = launch.expectedURL { serverURLs[project.id] = url }
            notice = "Launching \(project.name)…"
        } catch {
            notice = "Couldn't launch \(project.name): \(error.localizedDescription)"
        }
    }

    func stopAllServers() {
        for process in serverProcesses.values where process.isRunning { process.terminate() }
    }

    private func consumeServerOutput(_ data: Data, project: Project) {
        var buffer = serverBuffers[project.id] ?? Data()
        buffer.append(data)
        if buffer.count > 32_768 { buffer = Data(buffer.suffix(32_768)) }
        serverBuffers[project.id] = buffer
        guard let text = String(data: buffer, encoding: .utf8),
              let range = text.range(of: #"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(?:/[^\s\"']*)?"#, options: .regularExpression) else { return }
        let value = String(text[range]).replacingOccurrences(of: "0.0.0.0", with: "127.0.0.1")
        if let url = URL(string: value) { serverURLs[project.id] = url }
    }

    private func save() {
        if let data = try? JSONEncoder().encode(projects) { try? data.write(to: registry) }
        refreshOverview()
    }

    /// Rebuilds the All Projects page.
    func refreshOverview() {
        overviewPending?.cancel()
        let python = self.python, engine = self.engine
        let work = DispatchWorkItem { [weak self] in
            DispatchQueue.global(qos: .utility).async {
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: python)
                proc.arguments = [engine, "overview"]
                proc.standardOutput = Pipe()
                proc.standardError = Pipe()
                try? proc.run()
                proc.waitUntilExit()
                DispatchQueue.main.async { self?.reloadToken[Store.overviewID, default: 0] += 1 }
            }
        }
        overviewPending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 2, execute: work)
    }

    /// Notify when a new risky file shows up, e.g. an untracked .env that git would commit.
    private func checkRisks(_ project: Project) {
        let file = project.url.appendingPathComponent(".grc/scan.json")
        guard let data = try? Data(contentsOf: file),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        let risks = Set((json["env_files"] as? [[String: Any]] ?? [])
            .filter { ($0["ignored"] as? Bool) != true }
            .compactMap { $0["path"] as? String })
        defer { knownRisks[project.id] = risks }
        guard let before = knownRisks[project.id] else { return }  // first scan after launch, nothing to compare yet
        let fresh = risks.subtracting(before)
        guard !fresh.isEmpty else { return }
        let content = UNMutableNotificationContent()
        content.title = "Possible secret in \(project.name)"
        content.body = "\(fresh.sorted().joined(separator: ", ")) isn't ignored by git, so it could be committed. Add it to .gitignore."
        content.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "risk-\(project.id)-\(Date().timeIntervalSince1970)", content: content, trigger: nil))
    }

    static func handOffScript(path: String, name: String, agent: String, prompt: String) -> String {
        let command: String
        switch agent {
        case "codex": command = "codex"
        case "hermes": command = "hermes chat -q"
        default: command = "claude"
        }
        func q(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }
        let script = """
        #!/bin/zsh -l
        cd \(q(path)) || exit 1
        export GRC_AGENT=\(q(agent == "claude" ? "claude-code" : agent))
        printf '\\033]0;%s · %s\\007' \(q(agent)) \(q(name))
        \(command) \(q(prompt))
        """
        return script
    }

    /// Opens Terminal in the project with the chosen agent CLI and a prompt to read GRC first.
    func handOff(_ project: Project, to agent: String, task: String) {
        let job = task.trimmingCharacters(in: .whitespacesAndNewlines)
        let prompt = """
        You're picking up the project in this folder. First get its shared context: call the grc_context MCP tool \
        (or run: python3 '\(engine)' context). Read .grc/handoff.md if it exists. \
        Then: \(job.isEmpty ? "do the Next step on the Resume card." : job) \
        Keep the GRC to-do list and Resume card up to date with the grc tools as you go, and call grc_handoff before you stop.
        """
        let script = Store.handOffScript(path: project.path, name: project.name, agent: agent, prompt: prompt)
        let file = Store.support.appendingPathComponent("handoff-\(agent).command")
        do {
            try script.write(to: file, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: file.path)
        } catch {
            notice = "Couldn't write the hand-off script: \(error.localizedDescription)"
            return
        }
        NSWorkspace.shared.open(file)
        run(["log", "--dir", project.path, "--agent", "you", "--type", "handoff",
             "Handed to \(agent) from Genie: " + (job.isEmpty ? "next step on the Resume card" : String(job.prefix(160)))], for: project)
    }

    // MARK: actions

    func addProjects(_ urls: [URL]) {
        var added: [Project] = []
        for url in urls where url.isDirectory {
            let p = Project(path: url.standardizedFileURL.path)
            if !projects.contains(p) { projects.append(p); added.append(p) }
            selection = p.id
        }
        let files = urls.filter { !$0.isDirectory }.count
        if files > 0 { notice = "Drop folders on the sidebar to add projects. To add files, drop them on a project's dashboard." }
        guard !added.isEmpty else { return }
        save()
        projects.sort { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
        for p in added { run(["init", p.path], for: p) }
    }

    func addContext(_ urls: [URL], to project: Project) {
        guard !urls.isEmpty else { return }
        run(["add", "--dir", project.path] + urls.map(\.path), for: project)
        notice = "Added \(urls.count) item\(urls.count == 1 ? "" : "s") to \(project.name)'s context."
    }

    func remove(_ project: Project) {
        if let process = serverProcesses[project.id], process.isRunning { process.terminate() }
        serverURLs[project.id] = nil
        projects.removeAll { $0 == project }
        if selection == project.id { selection = projects.first?.id }
        save()
    }

    func rebuild(_ project: Project) { run(["build", project.path], for: project) }

    func installHooks(_ project: Project) {
        let python = self.python, engine = self.engine
        DispatchQueue.global(qos: .utility).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: python)
            proc.arguments = [engine, "hooks", project.path]
            let out = Pipe()
            proc.standardOutput = out
            proc.standardError = Pipe()
            try? proc.run()
            let text = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            proc.waitUntilExit()
            DispatchQueue.main.async {
                self.notice = "\(project.name) git hooks — " + text.split(separator: "\n").joined(separator: "; ")
            }
        }
    }
    func rebuildAll() { projects.forEach(rebuild) }

    func pickFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = "Add Project"
        if panel.runModal() == .OK { addProjects(panel.urls) }
    }

    // MARK: engine

    private func run(_ args: [String], for project: Project) {
        if busy.contains(project.id) { rerun.insert(project.id); return }
        busy.insert(project.id)
        let python = self.python, engine = self.engine
        DispatchQueue.global(qos: .utility).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: python)
            proc.arguments = [engine] + args
            let err = Pipe()
            proc.standardError = err
            proc.standardOutput = Pipe()
            var message: String?
            do {
                try proc.run()
                proc.waitUntilExit()
                if proc.terminationStatus != 0 {
                    let text = String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                    message = "\(project.name): " + (text.split(separator: "\n").last.map(String.init) ?? "build failed")
                }
            } catch {
                message = "Couldn't run \(python): \(error.localizedDescription)"
            }
            DispatchQueue.main.async {
                self.busy.remove(project.id)
                if let message { self.notice = message }
                self.lastBuilt[project.id] = Date()
                if message == nil && (args.first == "build" || args.first == "init") {
                    self.maybeBrief(project)
                    self.checkRisks(project)
                }
                self.refreshOverview()
                self.reloadToken[project.id, default: 0] += 1
                self.objectWillChange.send()
                if self.rerun.remove(project.id) != nil { self.rebuild(project) }
            }
        }
    }
}

extension URL {
    var isDirectory: Bool { (try? resourceValues(forKeys: [.isDirectoryKey]))?.isDirectory == true }
}

func loadURLs(_ providers: [NSItemProvider], _ done: @escaping ([URL]) -> Void) {
    let group = DispatchGroup()
    let lock = NSLock()
    var urls: [URL] = []
    for provider in providers where provider.canLoadObject(ofClass: URL.self) {
        group.enter()
        _ = provider.loadObject(ofClass: URL.self) { url, _ in
            if let url, url.isFileURL { lock.lock(); urls.append(url); lock.unlock() }
            group.leave()
        }
    }
    group.notify(queue: .main) { done(urls) }
}

// MARK: - Web view

/// A WKWebView that never takes file drops, so drops reach the SwiftUI handler.
final class DashboardWebView: WKWebView {
    override init(frame: CGRect, configuration: WKWebViewConfiguration) {
        super.init(frame: frame, configuration: configuration)
        unregisterDraggedTypes()
    }
    required init?(coder: NSCoder) { fatalError() }
    override func registerForDraggedTypes(_ newTypes: [NSPasteboard.PasteboardType]) {}
}

struct DashboardView: NSViewRepresentable {
    let file: URL
    let root: URL
    let token: Int
    /// Return true to handle a clicked file link in the app instead of navigating.
    var openLink: ((URL) -> Bool)? = nil

    final class Coordinator: NSObject, WKNavigationDelegate {
        var file: URL?
        var token = -1
        var scrollY: Double = 0
        var openLink: ((URL) -> Bool)?
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            if scrollY > 0 { webView.evaluateJavaScript("window.scrollTo(0, \(scrollY))") }
        }
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.navigationType == .linkActivated, let url = action.request.url {
                if url.isFileURL, openLink?(url) == true { return decisionHandler(.cancel) }
                if !url.isFileURL { NSWorkspace.shared.open(url); return decisionHandler(.cancel) }
            }
            decisionHandler(.allow)
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let view = DashboardWebView(frame: .zero, configuration: WKWebViewConfiguration())
        view.navigationDelegate = context.coordinator
        view.setValue(false, forKey: "drawsBackground")
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        let c = context.coordinator
        c.openLink = openLink
        guard c.file != file || c.token != token else { return }
        let sameFile = c.file == file
        c.file = file
        c.token = token
        guard FileManager.default.fileExists(atPath: file.path) else { return }
        if sameFile {
            view.evaluateJavaScript("window.scrollY") { y, _ in
                c.scrollY = (y as? Double) ?? 0
                view.loadFileURL(file, allowingReadAccessTo: root)
            }
        } else {
            c.scrollY = 0
            view.loadFileURL(file, allowingReadAccessTo: root)
        }
    }
}

// MARK: - Views

struct ContentView: View {
    @EnvironmentObject var store: Store
    @State private var sidebarTarget = false
    @State private var detailTarget = false
    @AppStorage("genieOpen") private var genieOpen = true
    @Environment(\.openWindow) private var openWindow

    var selected: Project? { store.projects.first { $0.id == store.selection } }

    var body: some View {
        NavigationSplitView {
            sidebar
        } detail: {
            detail
        }
        .overlay(alignment: .bottom) { noticeBar }
        .onAppear { store.openMainWindow = { openWindow(id: "main") } }
        .onChange(of: store.selection) { _, id in
            if let p = store.projects.first(where: { $0.id == id }) { store.refreshIfStale(p) }
        }
        .alert(
            store.offers.first.map { "\($0.agent) ran out of usage" } ?? "",
            isPresented: Binding(get: { store.offers.first != nil }, set: { _ in }),
            presenting: store.offers.first
        ) { offer in
            Button("Continue offline") { store.respond(to: offer.id, yes: true) }
            Button("Not now", role: .cancel) { store.respond(to: offer.id, yes: false) }
        } message: { offer in
            Text("It stopped while working on \(offer.projectName)\(offer.reset.isEmpty ? "" : " (\(offer.reset))"). GRC has saved a handoff, so any agent can pick up from it.\n\nContinue now with the offline model on this Mac? It can read and edit files in this project. Every file it changes is backed up, and you can undo the whole run from the Genie panel.")
        }
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            List(selection: $store.selection) {
                Section {
                    ForEach(ConnectionKind.allCases) { kind in
                        HStack {
                            Label(kind.rawValue, systemImage: kind == .tools ? "wrench.and.screwdriver" :
                                  (kind == .mcp ? "point.3.connected.trianglepath.dotted" : "key"))
                            Spacer()
                            Text("\(ConnectionAccess.shared(in: store.connections).filter { $0.kind == kind }.count)")
                                .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        }
                        .tag(kind.selectionID)
                    }
                } header: {
                    HStack {
                        Text("Connections")
                        Spacer()
                        Button { store.refreshConnections() } label: { Image(systemName: "arrow.clockwise") }
                            .buttonStyle(.plain).help("Refresh shared and agent-specific connections")
                    }
                }
                Section {
                    if store.agents.isEmpty {
                        Text(store.scanningAgents ? "Looking for agents…" : "No AI agents found")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        ForEach(store.agents) { agent in
                            AgentRow(agent: agent).tag(agent.id)
                        }
                    }
                } header: {
                    HStack {
                        Text("AI Agents")
                        Spacer()
                        Button { store.refreshAgents() } label: { Image(systemName: "arrow.clockwise") }
                            .buttonStyle(.plain).help("Check again")
                    }
                }

                Section("Projects") {
                    if store.projects.count > 1 {
                        Label("All Projects", systemImage: "square.grid.2x2").tag(Store.overviewID)
                    }
                    ForEach(store.projects) { p in
                        ProjectRow(project: p, busy: store.busy.contains(p.id), token: store.reloadToken[p.id, default: 0],
                                   running: store.runningServers.contains(p.id), serverURL: store.serverURLs[p.id],
                                   toggleServer: { store.toggleServer(p) })
                            .tag(p.id)
                            .contextMenu {
                                Button("Rebuild Now") { store.rebuild(p) }
                                Button("Install Git Hooks") { store.installHooks(p) }
                                    .help("Rebuild after every commit, and tag commits with the agent that made them (GRC_AGENT)")
                                Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([p.url]) }
                                Button("Open Dashboard in Browser") { NSWorkspace.shared.open(p.dashboard) }
                                Divider()
                                Button("Remove from GRC") { store.remove(p) }
                            }
                    }
                }
            }
            .listStyle(.sidebar)

            VStack(spacing: 6) {
                Image(systemName: "folder.badge.plus").font(.title3)
                Text("Drop a project folder here").font(.caption)
                Button("Choose Folder…") { store.pickFolder() }.controlSize(.small)
            }
            .foregroundStyle(sidebarTarget ? Color.accentColor : .secondary)
            .frame(maxWidth: .infinity)
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 10)
                .strokeBorder(style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
                .foregroundStyle(sidebarTarget ? Color.accentColor : Color.secondary.opacity(0.4)))
            .padding(10)
        }
        .navigationSplitViewColumnWidth(min: 200, ideal: 240)
        .onDrop(of: [.fileURL], isTargeted: $sidebarTarget) { providers in
            loadURLs(providers) { store.addProjects($0) }
            return true
        }
    }

    @ViewBuilder private var detail: some View {
        if let kind = ConnectionKind.from(selectionID: store.selection) {
            ConnectionManagerView(kind: kind)
                .navigationTitle(kind.rawValue)
                .navigationSubtitle("Shared environment · available to every AI agent")
        } else if let agent = store.agents.first(where: { $0.id == store.selection }) {
            AgentDetailView(agent: agent)
                .navigationTitle(agent.name)
                .navigationSubtitle("Agent profile and private resources")
        } else if store.selection == Store.overviewID {
            DashboardView(file: store.overviewFile, root: Store.support, token: store.reloadToken[Store.overviewID] ?? 0) { url in
                guard let p = store.projects.first(where: { $0.dashboard.standardizedFileURL.path == url.standardizedFileURL.path }) else { return false }
                store.selection = p.id
                return true
            }
            .navigationTitle("All Projects")
            .navigationSubtitle("\(store.projects.count) projects")
            .onAppear { store.refreshOverview() }
        } else if let p = selected {
            ZStack {
                DashboardView(file: p.dashboard, root: p.url, token: store.reloadToken[p.id] ?? 0)
                if detailTarget {
                    RoundedRectangle(cornerRadius: 16)
                        .fill(Color.accentColor.opacity(0.12))
                        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.accentColor, style: StrokeStyle(lineWidth: 2, dash: [8, 6])))
                        .overlay(VStack(spacing: 8) {
                            Image(systemName: "tray.and.arrow.down").font(.system(size: 36))
                            Text("Add to \(p.name)'s context").font(.title3.weight(.semibold))
                            Text("Files from outside are copied into .grc/context/. Files inside the project are pinned.")
                                .font(.callout).foregroundStyle(.secondary)
                        })
                        .padding(16)
                        .allowsHitTesting(false)
                }
            }
            .onDrop(of: [.fileURL], isTargeted: $detailTarget) { providers in
                loadURLs(providers) { store.addContext($0, to: p) }
                return true
            }
            .navigationTitle(p.name)
            .navigationSubtitle(subtitle(p))
            .inspector(isPresented: $genieOpen) {
                GenieView(chat: store.chat(for: p))
                    .id(p.id)
                    .inspectorColumnWidth(min: 300, ideal: 380, max: 560)
            }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { genieOpen.toggle() } label: { Label("Genie", systemImage: "sparkles") }
                        .help("Ask Genie, the local GRC agent (⌘J)")
                        .keyboardShortcut("j")
                }
                ToolbarItemGroup {
                    if store.busy.contains(p.id) { ProgressView().controlSize(.small) }
                    Button { store.rebuild(p) } label: { Label("Rebuild", systemImage: "arrow.clockwise") }
                        .help("Re-check the project and rebuild its dashboard")
                    Button { NSWorkspace.shared.activateFileViewerSelecting([p.url]) } label: { Label("Show in Finder", systemImage: "folder") }
                    Button { NSWorkspace.shared.open(p.dashboard) } label: { Label("Open in Browser", systemImage: "safari") }
                }
            }
        } else {
            VStack(spacing: 12) {
                Image(systemName: "point.3.connected.trianglepath.dotted").font(.system(size: 48)).foregroundStyle(.secondary)
                Text("Drop a project folder anywhere here").font(.title2.weight(.semibold))
                Text("GRC will check it, map it, and keep its dashboard up to date in the background.")
                    .foregroundStyle(.secondary)
                Button("Choose Folder…") { store.pickFolder() }.controlSize(.large)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(detailTarget ? Color.accentColor.opacity(0.08) : .clear)
            .onDrop(of: [.fileURL], isTargeted: $detailTarget) { providers in
                loadURLs(providers) { store.addProjects($0) }
                return true
            }
        }
    }

    private func subtitle(_ p: Project) -> String {
        if store.busy.contains(p.id) { return "Checking…" }
        guard let date = store.lastBuilt[p.id] else { return p.path }
        return "Updated " + date.formatted(date: .omitted, time: .shortened)
    }

    @ViewBuilder private var noticeBar: some View {
        if let notice = store.notice {
            HStack(spacing: 10) {
                Text(notice).font(.callout).lineLimit(2)
                Button { store.notice = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
            }
            .padding(.horizontal, 14).padding(.vertical, 9)
            .background(.regularMaterial, in: Capsule())
            .padding(.bottom, 14)
            .task(id: notice) {
                try? await Task.sleep(nanoseconds: 6_000_000_000)
                if store.notice == notice { store.notice = nil }
            }
        }
    }
}

struct GenieView: View {
    @EnvironmentObject var store: Store
    @ObservedObject var chat: GenieChat
    @State private var draft = ""
    @FocusState private var focused: Bool

    private let suggestions = [
        "What's going on in this project?",
        "What should I do next?",
        "What changed recently, and who did it?",
        "Which tasks are stuck or half-done?",
    ]

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if chat.messages.isEmpty { intro }
                        ForEach(chat.messages) { m in
                            MessageView(message: m, running: chat.running && m.id == chat.messages.last?.id) { run in chat.undo(run) }
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(14)
                }
                .onChange(of: chat.messages.last?.text) { _, _ in proxy.scrollTo("end", anchor: .bottom) }
                .onChange(of: chat.messages.count) { _, _ in proxy.scrollTo("end", anchor: .bottom) }
                .onAppear { proxy.scrollTo("end", anchor: .bottom) }
            }
            Divider()
            composer
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 30, height: 30)
            VStack(alignment: .leading, spacing: 1) {
                Text("Genie").font(.headline)
                Text(status).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Menu {
                Button("Continue offline from the handoff…") {
                    let alert = NSAlert()
                    alert.messageText = "Continue offline?"
                    alert.informativeText = "The offline model will pick up from the handoff and Resume card and may edit files in \(chat.project.name). Every change is backed up so you can undo it."
                    alert.addButton(withTitle: "Continue offline")
                    alert.addButton(withTitle: "Cancel")
                    if alert.runModal() == .alertFirstButtonReturn { chat.continueOffline(from: "the last agent") }
                }.disabled(chat.running)
                Menu("Hand off to…") {
                    ForEach(handOffAgents, id: \.id) { a in
                        Button(a.name) { store.handOff(chat.project, to: a.id, task: draft); draft = "" }
                    }
                }
                .disabled(handOffAgents.isEmpty)
                .help("Opens Terminal with that agent in this project, told to start from GRC's context. Anything typed in the box becomes its task.")
                Button("Refresh the project brief now") { store.maybeBrief(chat.project, force: true) }
                Divider()
                Button("Clear conversation") { chat.clear() }.disabled(chat.running)
            } label: { Image(systemName: "ellipsis.circle") }
                .menuStyle(.borderlessButton).fixedSize()
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }

    /// Agents that can take a task in Terminal (they read GRC through the grc MCP tools).
    private var handOffAgents: [(id: String, name: String)] {
        [("claude", "Claude Code"), ("codex", "Codex"), ("hermes", "Hermes")].filter { a in store.agents.contains { $0.id == a.0 } }
            .map { (id: $0.0, name: $0.1) }
    }

    private var status: String {
        if chat.running { return chat.step }
        if store.briefing.contains(chat.project.id) { return "Updating the project brief in the background…" }
        let ollama = store.agents.first { $0.id == "ollama" }
        if ollama?.ready == false { return "Ollama isn't running — open the Ollama app" }
        return (chat.model.isEmpty ? "Local" : chat.model.replacingOccurrences(of: ":latest", with: "")) + " · on this Mac · \(chat.project.name)"
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Hi — I'm Genie, here for **\(chat.project.name)**.").font(.title3)
            Text("Ask me what's going on, or give me a task. I can read every file here, and I can change the to-do list and the Resume card. I can't edit code — I'll tell you which agent should.")
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            ForEach(suggestions, id: \.self) { s in
                Button { chat.send(s) } label: {
                    Text(s).frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 10).padding(.vertical, 7)
                        .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField("Ask Genie, or give it a task…", text: $draft, axis: .vertical)
                .textFieldStyle(.plain).lineLimit(1...6).focused($focused)
                .onSubmit(send)
                .padding(.horizontal, 10).padding(.vertical, 8)
                .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
            if chat.running {
                Button { chat.stop() } label: { Image(systemName: "stop.circle.fill").font(.title2) }
                    .buttonStyle(.plain).help("Stop")
            } else {
                Button(action: send) { Image(systemName: "arrow.up.circle.fill").font(.title2) }
                    .buttonStyle(.plain).disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(12)
        .onAppear { focused = true }
    }

    private func send() {
        let q = draft
        draft = ""
        chat.send(q)
    }
}

struct MessageView: View {
    let message: GenieMessage
    let running: Bool
    var undo: (String) -> Void = { _ in }

    var body: some View {
        switch message.role {
        case .user:
            HStack {
                Spacer(minLength: 40)
                Text(message.text).textSelection(.enabled)
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(Color.accentColor.opacity(0.16), in: RoundedRectangle(cornerRadius: 12))
            }
        case .error:
            Label(message.text, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
        case .assistant:
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(message.steps.enumerated()), id: \.offset) { _, step in
                    Label(step, systemImage: "magnifyingglass").font(.caption).foregroundStyle(.secondary)
                }
                if message.text.isEmpty && running {
                    ProgressView().controlSize(.small)
                } else if !message.text.isEmpty {
                    Text(markdown(message.text)).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
                ForEach(message.changed, id: \.self) { item in
                    Label(item, systemImage: "checkmark.circle.fill").font(.caption).foregroundStyle(.green)
                }
                if let run = message.checkpoint, !running {
                    Button { undo(run) } label: { Label("Undo the file changes from this run", systemImage: "arrow.uturn.backward") }
                        .controlSize(.small)
                }
            }
        }
    }

    private func markdown(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
    }
}

struct AgentDetailView: View {
    @EnvironmentObject var store: Store
    let agent: Agent

    private var exclusive: [ManagedConnection] {
        ConnectionAccess.exclusive(to: agent.id, in: store.connections)
    }
    private var shared: [ManagedConnection] { ConnectionAccess.shared(in: store.connections) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack(spacing: 16) {
                    Group {
                        if let image = agent.icon { Image(nsImage: image).resizable().scaledToFit() }
                        else { Image(systemName: agent.symbol).resizable().scaledToFit().padding(8) }
                    }
                    .frame(width: 52, height: 52)
                    .background(Color.accentColor.opacity(0.1), in: RoundedRectangle(cornerRadius: 12))
                    VStack(alignment: .leading, spacing: 4) {
                        Text(agent.name).font(.title.bold())
                        Label(agent.ready ? "Ready" : "Needs attention", systemImage: agent.ready ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                            .foregroundStyle(agent.ready ? .green : .orange)
                        Text(agent.detail).foregroundStyle(.secondary)
                    }
                    Spacer()
                }

                GroupBox("Agent installation") {
                    LabeledContent("Executable") { Text(agent.path).font(.caption.monospaced()).textSelection(.enabled) }
                    if !agent.models.isEmpty {
                        Divider()
                        LabeledContent("Models") { Text(agent.models.joined(separator: ", ")).multilineTextAlignment(.trailing) }
                    }
                }

                VStack(alignment: .leading, spacing: 10) {
                    Text("Shared environment").font(.title2.bold())
                    Text("These are agent-neutral resources. \(agent.name) receives the same shared pool as every other agent; no private copy is required.")
                        .foregroundStyle(.secondary)
                    HStack(spacing: 10) {
                        ResourceCount(title: "Tools", count: shared.filter { $0.kind == .tools }.count, icon: "wrench.and.screwdriver")
                        ResourceCount(title: "MCP", count: shared.filter { $0.kind == .mcp }.count, icon: "point.3.connected.trianglepath.dotted")
                        ResourceCount(title: "API", count: shared.filter { $0.kind == .api }.count, icon: "key")
                    }
                }

                VStack(alignment: .leading, spacing: 10) {
                    Text("Exclusive to \(agent.name)").font(.title2.bold())
                    Text("Only resources actually owned by this agent appear here. They stay out of the general pool so they cannot interfere with other agents.")
                        .foregroundStyle(.secondary)
                    if exclusive.isEmpty {
                        ContentUnavailableView("No exclusive resources", systemImage: "person.crop.circle.badge.checkmark",
                                               description: Text("This agent currently relies entirely on the shared environment."))
                            .frame(maxWidth: .infinity, minHeight: 180)
                    } else {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 310), spacing: 14)], spacing: 14) {
                            ForEach(exclusive) { ConnectionCard(item: $0) }
                        }
                    }
                }
            }
            .padding(24)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

struct ResourceCount: View {
    let title: String
    let count: Int
    let icon: String
    var body: some View {
        HStack {
            Image(systemName: icon).foregroundStyle(Color.accentColor)
            Text(title)
            Spacer()
            Text("\(count)").font(.title3.monospacedDigit().bold())
        }
        .padding(12).frame(maxWidth: .infinity)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
    }
}

struct ConnectionManagerView: View {
    @EnvironmentObject var store: Store
    let kind: ConnectionKind
    @State private var showingAdd = false

    private var items: [ManagedConnection] {
        ConnectionAccess.shared(in: store.connections).filter { $0.kind == kind }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(kind.rawValue).font(.largeTitle.bold())
                        Text(description).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { store.refreshConnections() } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                    Button { showingAdd = true } label: { Label("Add \(singular)", systemImage: "plus") }
                        .buttonStyle(.borderedProminent)
                }

                if items.isEmpty {
                    ContentUnavailableView("No \(kind.rawValue) connections", systemImage: icon,
                                           description: Text("Add one and the local agent will validate and tag it."))
                        .frame(maxWidth: .infinity, minHeight: 320)
                } else {
                    LazyVGrid(columns: [GridItem(.adaptive(minimum: 300), spacing: 16)], spacing: 16) {
                        ForEach(items) { item in ConnectionCard(item: item) }
                    }
                }
            }
            .padding(24)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .sheet(isPresented: $showingAdd) { AddConnectionView(initialKind: kind) }
    }

    private var singular: String { kind == .tools ? "Tool" : kind.rawValue }
    private var icon: String { kind == .tools ? "wrench.and.screwdriver" : (kind == .mcp ? "point.3.connected.trianglepath.dotted" : "key") }
    private var description: String {
        switch kind {
        case .tools: return "Agent-neutral local capabilities available to every AI agent."
        case .mcp: return "Shared Model Context Protocol servers configured across supported agents."
        case .api: return "Shared providers, messaging services, and HTTP APIs discoverable by every agent."
        }
    }
}

struct ConnectionCard: View {
    @EnvironmentObject var store: Store
    let item: ManagedConnection

    var body: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack(alignment: .top) {
                Image(systemName: icon)
                    .font(.title2).frame(width: 34, height: 34)
                    .background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                VStack(alignment: .leading, spacing: 2) {
                    Text(item.name).font(.headline).lineLimit(1)
                    Text(item.kind == .tools ? "Tool" : item.kind.rawValue).font(.caption).foregroundStyle(.secondary)
                    Label(item.agentID == nil ? "Shared with every agent" : "Exclusive to \(agentLabel)",
                          systemImage: item.agentID == nil ? "person.3.fill" : "person.crop.circle.fill")
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(item.agentID == nil ? Color.accentColor : Color.orange)
                }
                Spacer()
                HStack(spacing: 6) {
                    if item.state == .checking { ProgressView().controlSize(.small) }
                    else { Circle().fill(statusColor).frame(width: 8, height: 8) }
                    Text(item.state.rawValue).font(.caption.weight(.medium)).foregroundStyle(statusColor)
                }
            }

            Text(item.detail).font(.callout).foregroundStyle(.secondary).lineLimit(2)
            if let instructions = item.instructions, !instructions.isEmpty {
                Text(instructions).font(.caption).foregroundStyle(.secondary).lineLimit(3)
            }
            if item.secretName != nil {
                Label("Secret stored in Keychain", systemImage: "lock.fill")
                    .font(.caption.weight(.medium)).foregroundStyle(.green)
            }
            if !item.target.isEmpty {
                Text(item.target).font(.caption.monospaced()).lineLimit(2)
                    .padding(8).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
            }
            HStack(spacing: 5) {
                ForEach(item.tags.prefix(5), id: \.self) { tag in
                    Text(tag).font(.caption2.weight(.medium)).padding(.horizontal, 7).padding(.vertical, 3)
                        .background(Color.accentColor.opacity(0.1), in: Capsule())
                }
            }
            Spacer(minLength: 0)
            HStack {
                if item.isCustom {
                    Button { store.validateConnection(item.id) } label: { Label("Check", systemImage: "checkmark.shield") }
                    Button(role: .destructive) { store.removeConnection(item) } label: { Label("Remove", systemImage: "trash") }
                } else {
                    Label("Discovered automatically", systemImage: "sparkles").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if let url = URL(string: item.target), ["http", "https"].contains(url.scheme ?? "") {
                    Button("Open") { NSWorkspace.shared.open(url) }
                }
            }
            .buttonStyle(.borderless)
        }
        .padding(16)
        .frame(maxWidth: .infinity, minHeight: 220, alignment: .topLeading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.secondary.opacity(0.15)))
    }

    private var icon: String { item.kind == .tools ? "wrench.and.screwdriver" : (item.kind == .mcp ? "point.3.connected.trianglepath.dotted" : "key") }
    private var statusColor: Color { item.state == .active ? .green : (item.state == .inactive ? .red : .secondary) }
    private var agentLabel: String {
        switch item.agentID {
        case "claude": return "Claude Code"
        case "codex": return "Codex"
        case "hermes": return "Hermes"
        case let value?: return value
        case nil: return "all agents"
        }
    }
}

struct AddConnectionView: View {
    @EnvironmentObject var store: Store
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var instructions = ""
    @State private var secret = ""
    @State private var kind: ConnectionKind

    init(initialKind: ConnectionKind) { _kind = State(initialValue: initialKind) }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Add Connection").font(.title2.bold())
                    Text("Describe what to connect; the local agent will interpret, prepare, tag, and check it.").foregroundStyle(.secondary)
                }
                Spacer()
                Button { dismiss() } label: { Image(systemName: "xmark.circle.fill") }.buttonStyle(.plain)
            }
            Picker("Type", selection: $kind) {
                Text("Tool").tag(ConnectionKind.tools)
                Text("MCP").tag(ConnectionKind.mcp)
                Text("API").tag(ConnectionKind.api)
            }
            .pickerStyle(.segmented)
            TextField("Name, for example cTrader", text: $name)

            VStack(alignment: .leading, spacing: 6) {
                Text("Source or setup instructions").font(.headline)
                TextEditor(text: $instructions)
                    .font(.body.monospaced())
                    .frame(minHeight: 105)
                    .padding(6)
                    .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 7))
                    .overlay(alignment: .topLeading) {
                        if instructions.isEmpty {
                            Text(instructionPrompt).foregroundStyle(.tertiary).padding(.horizontal, 11).padding(.vertical, 14).allowsHitTesting(false)
                        }
                    }
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("Token or secret (optional)").font(.headline)
                SecureField("Paste any token, API key, or secret", text: $secret)
                Label("Saved only in macOS Keychain. It is never written to connection files, cards, or logs.", systemImage: "lock.shield")
                    .font(.caption).foregroundStyle(.secondary)
            }

            Text("GitHub sources are cloned into GRC's private support folder. Include a launch command in the sentence when the repository needs one.")
                .font(.caption).foregroundStyle(.secondary)
            if let error, !name.isEmpty || !instructions.isEmpty || !secret.isEmpty {
                Label(error, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
            }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Confirm & Connect") {
                    store.addConnection(name: name, instructions: instructions, secret: secret, kind: kind)
                    store.selection = kind.selectionID
                    dismiss()
                }
                .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(error != nil)
            }
        }
        .padding(24).frame(width: 620)
    }

    private var error: String? {
        ConnectionValidator.basicError(name: name, kind: kind, target: instructions, hasSecret: !secret.isEmpty)
    }
    private var instructionPrompt: String {
        "Example: Clone https://github.com/example/ctrader-mcp and run npx -y @example/ctrader-mcp, or connect its API at https://api.example.com"
    }
}

struct AgentRow: View {
    let agent: Agent

    var body: some View {
        HStack(spacing: 10) {
            Group {
                if let icon = agent.icon {
                    Image(nsImage: icon).resizable().interpolation(.high)
                } else {
                    Image(systemName: agent.symbol).font(.system(size: 13, weight: .medium))
                        .frame(width: 22, height: 22)
                        .background(Color.secondary.opacity(0.15), in: RoundedRectangle(cornerRadius: 5))
                }
            }
            .frame(width: 22, height: 22)
            VStack(alignment: .leading, spacing: 1) {
                Text(agent.name).lineLimit(1)
                Text(agent.detail).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            Circle().fill(agent.ready ? Color.green : Color.secondary.opacity(0.4)).frame(width: 7, height: 7)
                .help(agent.ready ? "Ready to use" : "Installed but not ready")
        }
        .padding(.vertical, 2)
        .help(agent.path)
    }
}

struct ProjectRow: View {
    let project: Project
    let busy: Bool
    let token: Int  // bumped after each build so the summary reloads
    let running: Bool
    let serverURL: URL?
    let toggleServer: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            let s = project.summary
            ZStack {
                Circle().stroke(Color.secondary.opacity(0.25), lineWidth: 3)
                Circle().trim(from: 0, to: CGFloat(s?.percent ?? 0) / 100)
                    .stroke(Color.accentColor, style: StrokeStyle(lineWidth: 3, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                if busy { ProgressView().controlSize(.mini) }
            }
            .frame(width: 22, height: 22)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 5) {
                    Text(project.name).lineLimit(1)
                    Button(action: toggleServer) {
                        Image(systemName: running ? "stop.fill" : "play.fill")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(running ? Color.red : Color.accentColor)
                    }
                    .buttonStyle(.borderless)
                    .help(running ? "Stop server" : "Launch server")
                }
                HStack(spacing: 5) {
                    Text(s.map { "\($0.percent)% · \($0.open) open" } ?? "no tasks yet")
                    if let url = serverURL {
                        Button(url.host ?? "Open") { NSWorkspace.shared.open(url) }
                            .buttonStyle(.link).lineLimit(1).help(url.absoluteString)
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }
}

struct MenuContent: View {
    @EnvironmentObject var store: Store
    @Environment(\.openWindow) private var openWindow
    @State private var loginItem = SMAppService.mainApp.status == .enabled

    var body: some View {
        ForEach(store.projects) { p in
            Button("\(p.name)\(p.summary.map { " — \($0.percent)%" } ?? "")") {
                store.selection = p.id
                openWindow(id: "main")
                NSApp.activate(ignoringOtherApps: true)
            }
        }
        if store.projects.isEmpty { Text("No projects yet") }
        if store.projects.count > 1 {
            Button("All Projects") {
                store.selection = Store.overviewID
                openWindow(id: "main")
                NSApp.activate(ignoringOtherApps: true)
            }
        }
        if !store.offers.isEmpty {
            Divider()
            ForEach(store.offers) { o in
                Button("⚠︎ \(o.agent) ran out on \(o.projectName) — continue offline?") {
                    store.openMainWindow?()
                    NSApp.activate(ignoringOtherApps: true)
                }
            }
        }
        Divider()
        Button("Add Project…") { NSApp.activate(ignoringOtherApps: true); store.pickFolder() }
        Button("Rebuild All") { store.rebuildAll() }
        Button("Check Now") { store.wake() }
            .help("Look for agents that ran out of usage and refresh the open project. GRC doesn't watch in the background.")
        Toggle("Open at Login", isOn: Binding(get: { loginItem }, set: { on in
            do {
                if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
                loginItem = on
            } catch {
                store.notice = "Couldn't change login item: \(error.localizedDescription)"
            }
        }))
        Divider()
        Button("Quit GRC") { NSApp.terminate(nil) }.keyboardShortcut("q")
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    static weak var store: Store?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let yes = UNNotificationAction(identifier: "CONTINUE", title: "Continue offline", options: [.foreground])
        let no = UNNotificationAction(identifier: "NOT_NOW", title: "Not now", options: [])
        center.setNotificationCategories([UNNotificationCategory(identifier: "GRC_LIMIT", actions: [yes, no], intentIdentifiers: [])])
        center.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    func applicationWillTerminate(_ notification: Notification) {
        AppDelegate.store?.stopAllServers()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let id = response.notification.request.identifier
        DispatchQueue.main.async {
            switch response.actionIdentifier {
            case "CONTINUE": AppDelegate.store?.respond(to: id, yes: true)
            case "NOT_NOW": AppDelegate.store?.respond(to: id, yes: false)
            default:  // banner click: bring the app forward
                AppDelegate.store?.openMainWindow?()
                NSApp.activate(ignoringOtherApps: true)
            }
            done()
        }
    }
}

@main
struct GRCApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var store: Store

    init() {
        let s = Store()
        _store = StateObject(wrappedValue: s)
        AppDelegate.store = s
    }

    var body: some Scene {
        WindowGroup("GRC", id: "main") {
            ContentView()
                .environmentObject(store)
                .frame(minWidth: 900, minHeight: 600)
        }
        .commands {
            CommandGroup(after: .newItem) {
                Button("Add Project…") { store.pickFolder() }.keyboardShortcut("o")
                Button("Rebuild All") { store.rebuildAll() }.keyboardShortcut("r", modifiers: [.command, .shift])
            }
        }

        MenuBarExtra("GRC", systemImage: "point.3.connected.trianglepath.dotted") {
            MenuContent().environmentObject(store)
        }
    }
}
