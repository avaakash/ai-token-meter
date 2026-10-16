import Foundation

/// A session's transcript as turns: each prompt the person typed and
/// everything the agent did until the next one, priced from the same
/// `messages` rows as the rest of the dashboard.
private final class Turn {
    var kind: String
    var prompt: String?
    var startedAt: String?
    var endedAt: String?
    var response = ""
    var responseChars = 0
    var tools: [String: Int] = [:]
    var models: [String: Int] = [:]
    var priced = Set<String>()
    var inherited = Set<String>()
    var subagentMessages = 0
    var sums: [String: Double] = [:]

    init(kind: String, prompt: String?, at: String?) {
        self.kind = kind
        self.prompt = prompt
        startedAt = at
        endedAt = at
    }

    static let responseLimit = 1500

    func addText(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        responseChars += trimmed.count
        guard response.count < Self.responseLimit else { return }
        if !response.isEmpty { response += "\n\n" }
        response += String(trimmed.prefix(Self.responseLimit - response.count))
    }

    func add(_ row: Row) {
        for key in ["cost", "cost_input", "cost_output", "cost_cache_read", "cost_cache_write"] {
            sums[key, default: 0] += row.double(key) ?? 0
        }
        for key in ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"] {
            sums[key, default: 0] += row.double(key) ?? 0
        }
        if let model = row.string("model"), model != "<synthetic>" { models[model, default: 0] += 1 }
    }

    func touch(_ ts: String?) {
        guard let ts else { return }
        if startedAt == nil { startedAt = ts }
        if endedAt == nil || ts > endedAt! { endedAt = ts }
    }

    var json: [String: Any] {
        var d: [String: Any] = [
            "kind": kind, "prompt": orNull(prompt), "started_at": orNull(startedAt), "ended_at": orNull(endedAt),
            "response": response, "response_chars": responseChars,
            "tools": tools.sorted { $0.value != $1.value ? $0.value > $1.value : $0.key < $1.key }.map { ["name": $0.key, "count": $0.value] },
            "models": models.sorted { $0.value > $1.value }.map(\.key),
            "message_count": priced.count, "subagent_messages": subagentMessages,
            "carried_over": priced.isEmpty && !inherited.isEmpty,
        ]
        for (k, v) in sums { d[k] = k.hasSuffix("_tokens") ? Int(v) as Any : v as Any }
        for key in ["cost", "cost_input", "cost_output", "cost_cache_read", "cost_cache_write"] where d[key] == nil { d[key] = 0.0 }
        for key in ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"] where d[key] == nil { d[key] = 0 }
        return d
    }
}

private let rowColumns = """
    message_id, session_id, model, cost, cost_input, cost_output, cost_cache_read, cost_cache_write,
    input_tokens, output_tokens, cache_read_tokens, cache_5m_tokens + cache_1h_tokens AS cache_write_tokens
    """

extension UsageStore {
    public func buildTranscript(sessionId: String) throws -> [String: Any] {
        try scanAll()
        guard let meta = try db.queryOne(
            "SELECT \(sessionColumns) FROM messages WHERE session_id = ? GROUP BY session_id", [sessionId]) else {
            throw ValidationError(description: "no session with id \(sessionId)")
        }
        let source = meta.string("source") ?? "claude"
        let turns = source == "codex" ? try codexTurns(sessionId) : try claudeTurns(sessionId)
        let visible = turns.filter { !($0.kind == "start" && $0.priced.isEmpty && $0.inherited.isEmpty && $0.response.isEmpty) }
        return ["session": meta.dict, "turns": visible.map(\.json)]
    }

    private func rows(for ids: [String]) throws -> [String: Row] {
        var out: [String: Row] = [:]
        var i = 0
        while i < ids.count {
            let chunk = Array(ids[i..<min(ids.count, i + 400)])
            let marks = Array(repeating: "?", count: chunk.count).joined(separator: ",")
            for r in try db.query("SELECT \(rowColumns) FROM messages WHERE message_id IN (\(marks))", chunk) {
                if let id = r.string("message_id") { out[id] = r }
            }
            i += 400
        }
        return out
    }

    // MARK: Claude Code

    /// The person's words in a `user` record, a slash command as typed, or
    /// nil for tool results and injected context.
    static func claudeTurnPrompt(_ obj: [String: Any]) -> (kind: String, text: String)? {
        guard !truthy(obj["isMeta"]), !truthy(obj["isSidechain"]), !truthy(obj["isCompactSummary"]) else { return nil }
        var content = (obj["message"] as? [String: Any])?["content"]
        if let blocks = content as? [Any] {
            let texts = blocks.compactMap { $0 as? [String: Any] }.filter { $0["type"] as? String == "text" }
            guard !texts.isEmpty else { return nil }  // tool results
            content = texts.compactMap { $0["text"] as? String }.joined(separator: "\n")
        }
        // Claude Code prepends system reminders to the person's own message.
        guard let text = (content as? String)?
            .replacingOccurrences(of: #"<system-reminder>[\s\S]*?</system-reminder>"#, with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else { return nil }
        if text.contains("<command-name>") {
            func between(_ a: String, _ b: String) -> String {
                guard let s = text.range(of: a) else { return "" }
                let rest = text[s.upperBound...]
                return rest[..<(rest.range(of: b)?.lowerBound ?? rest.endIndex)].trimmingCharacters(in: .whitespacesAndNewlines)
            }
            let name = between("<command-name>", "</command-name>")
            guard !name.isEmpty else { return nil }
            let args = between("<command-args>", "</command-args>")
            return ("command", args.isEmpty ? name : name + " " + args)
        }
        if text.hasPrefix("<") || text.hasPrefix("Caveat:") || text.hasPrefix("[Request interrupted") { return nil }
        return ("prompt", String(text.prefix(4000)))
    }

    private func claudeTurns(_ sessionId: String) throws -> [Turn] {
        let root = paths.home + "/.claude/projects"
        var mainFiles: [String] = [], subagentFiles: [String] = []
        for dir in (try? FileManager.default.contentsOfDirectory(atPath: root)) ?? [] {
            let file = "\(root)/\(dir)/\(sessionId).jsonl"
            if FileManager.default.fileExists(atPath: file) { mainFiles.append(file) }
            subagentFiles += jsonlFiles(under: "\(root)/\(dir)/\(sessionId)/subagents", recursive: true)
        }

        var turns = [Turn(kind: "start", prompt: nil, at: nil)]
        var events: [(turn: Turn, id: String, own: Bool)] = []
        var seen = Set<String>()
        for file in mainFiles.sorted() {
            _ = try forEachLine(path: file, from: 0) { line, _ in
                guard let obj = parseJSONObject(line) else { return }
                let ts = obj["timestamp"] as? String
                switch obj["type"] as? String {
                case "user":
                    if let p = Self.claudeTurnPrompt(obj) { turns.append(Turn(kind: p.kind, prompt: p.text, at: ts)) }
                case "assistant":
                    guard !truthy(obj["isSidechain"]), let msg = obj["message"] as? [String: Any] else { return }
                    let turn = turns[turns.count - 1]
                    turn.touch(ts)
                    for block in msg["content"] as? [[String: Any]] ?? [] {
                        switch block["type"] as? String {
                        case "text": turn.addText(block["text"] as? String ?? "")
                        case "tool_use": turn.tools[block["name"] as? String ?? "tool", default: 0] += 1
                        default: break
                        }
                    }
                    if let id = msg["id"] as? String, msg["usage"] != nil, seen.insert(id).inserted {
                        events.append((turn, id, true))
                    }
                default:
                    break
                }
            }
        }

        // Sub-agent messages land in the turn that was running when they did.
        var subagentEvents: [(ts: String, id: String)] = []
        for file in subagentFiles {
            _ = try forEachLine(path: file, from: 0) { line, _ in
                guard contains(line, "\"usage\""), let obj = parseJSONObject(line), obj["type"] as? String == "assistant",
                      let msg = obj["message"] as? [String: Any], let id = msg["id"] as? String,
                      let ts = obj["timestamp"] as? String, seen.insert(id).inserted else { return }
                subagentEvents.append((ts, id))
            }
        }
        for e in subagentEvents {
            let turn = turns.last { ($0.startedAt ?? "") <= e.ts } ?? turns[0]
            events.append((turn, e.id, false))
            turn.subagentMessages += 1
        }

        let priced = try rows(for: events.map(\.id))
        for e in events {
            guard let row = priced[e.id] else { continue }
            // A resumed or forked session's file starts with the earlier
            // conversation; those messages are billed to that session.
            if e.own && row.string("session_id") != sessionId {
                e.turn.inherited.insert(e.id)
                continue
            }
            e.turn.priced.insert(e.id)
            e.turn.add(row)
        }
        return turns
    }

    // MARK: Codex

    private static let codexInjected = ["<", "# AGENTS.md", ">>> RETAINED USER INSTRUCTIONS", "The following is the Codex agent history"]

    private func codexTurns(_ sessionId: String) throws -> [Turn] {
        // Codex rows are keyed "codex:<path>:<byte offset>"; the path tells us
        // which logs hold this session (resolving a later move to archived_sessions).
        let ids = try db.query("SELECT \(rowColumns) FROM messages WHERE session_id = ?", [sessionId])
        var byKey: [String: Row] = [:]
        var files = Set<String>()
        for r in ids {
            guard let id = r.string("message_id"), id.hasPrefix("codex:"), let colon = id.lastIndex(of: ":") else { continue }
            let path = String(id[id.index(id.startIndex, offsetBy: 6)..<colon])
            byKey[(path as NSString).lastPathComponent + ":" + id[id.index(after: colon)...]] = r
            if FileManager.default.fileExists(atPath: path) {
                files.insert(path)
            } else {
                let archived = paths.home + "/.codex/archived_sessions/" + (path as NSString).lastPathComponent
                if FileManager.default.fileExists(atPath: archived) { files.insert(archived) }
            }
        }

        var turns = [Turn(kind: "start", prompt: nil, at: nil)]
        for file in files.sorted() {
            let base = (file as NSString).lastPathComponent
            _ = try forEachLine(path: file, from: 0) { line, offset in
                guard let obj = parseJSONObject(line) else { return }
                let ts = obj["timestamp"] as? String
                let payload = obj["payload"] as? [String: Any] ?? [:]
                let turn = turns[turns.count - 1]
                switch (obj["type"] as? String, payload["type"] as? String) {
                case ("event_msg", "task_started"):
                    turns.append(Turn(kind: "prompt", prompt: nil, at: ts))
                case ("response_item", "message"):
                    let texts = (payload["content"] as? [[String: Any]] ?? []).compactMap { $0["text"] as? String }
                    let text = texts.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
                    if payload["role"] as? String == "user" {
                        guard !text.isEmpty, !Self.codexInjected.contains(where: { text.hasPrefix($0) }) else { return }
                        if turn.prompt == nil {
                            turn.prompt = String(text.prefix(4000))
                        } else if !turn.priced.isEmpty {
                            turns.append(Turn(kind: "prompt", prompt: String(text.prefix(4000)), at: ts))
                        }
                    } else if payload["role"] as? String == "assistant" {
                        turn.touch(ts)
                        turn.addText(text)
                    }
                case ("response_item", "function_call"), ("response_item", "custom_tool_call"):
                    turn.touch(ts)
                    turn.tools[payload["name"] as? String ?? "tool", default: 0] += 1
                case ("event_msg", "token_count"):
                    guard let row = byKey[base + ":" + String(offset)], let id = row.string("message_id") else { return }
                    turn.touch(ts)
                    if turn.priced.insert(id).inserted { turn.add(row) }
                default:
                    break
                }
            }
        }
        for turn in turns where turn.kind == "prompt" && turn.prompt == nil { turn.kind = "automatic" }
        return turns
    }
}
