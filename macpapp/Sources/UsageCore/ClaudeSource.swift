import Foundation

final class ClaudeSource: UsageSource {
    let name = "claude"
    let pricing: Pricing

    static let usageURL = URL(string: "https://api.anthropic.com/api/oauth/usage")!
    static let keychainService = "Claude Code-credentials"

    static let ist = 5 * 3600 + 30 * 60
    /// A confirmed real weekly reset (Wednesday 15:30 IST / 10:00 UTC). Only
    /// the weekday and time matter — the reset is on a fixed 7-day grid.
    static let weeklyAnchor = PyDateTime.parse("2026-07-15T15:30:00+05:30")!
    static let weeklyPeriod = 7 * Micros.day
    /// Usage-anchored, not a clock grid: a window starts at the first message
    /// after the previous one's 5 hours fully elapsed.
    static let fiveHourPeriod = 5 * Micros.hour
    static let fiveHourLookbackDays = 14

    init(pricing: Pricing) { self.pricing = pricing }

    func logFiles(home: String) -> [String] {
        jsonlFiles(under: home + "/.claude/projects", recursive: true)
    }

    func computeCosts(model: String?, timestamp: String?, tokens t: TokenFields) -> Costs {
        let promptTokens = t.input + t.cacheRead + t.cache5m + t.cache1h
        let rate = pricing.resolveRate(model: model, timestamp: timestamp, promptTokens: promptTokens)
        let inPPT = rate.input / 1_000_000.0
        let outPPT = rate.output / 1_000_000.0
        var c = Costs()
        c.costInput = Double(t.input) * inPPT
        c.costOutput = Double(t.output) * outPPT
        c.costCacheRead = Double(t.cacheRead) * (rate.cachedInput ?? rate.input * 0.1) / 1_000_000.0
        c.costCacheWrite = Double(t.cache5m) * inPPT * 1.25 + Double(t.cache1h) * inPPT * 2.0
        c.costCache = c.costCacheRead + c.costCacheWrite
        c.cost = c.costInput + c.costOutput + c.costCache
        c.costNoCache = Double(t.input + t.cacheRead + t.cache5m + t.cache1h) * inPPT + Double(t.output) * outPPT
        return c
    }

    func tokenFields(from row: Row) -> TokenFields {
        TokenFields(input: row.int("input_tokens") ?? 0, output: row.int("output_tokens") ?? 0,
                    cacheRead: row.int("cache_read_tokens") ?? 0, cache5m: row.int("cache_5m_tokens") ?? 0,
                    cache1h: row.int("cache_1h_tokens") ?? 0)
    }

    func scanFile(_ db: SQLiteDB, path: String, state: [String: FileState], home: String) throws {
        guard let stat = fileStat(path) else { return }
        let prev = state[path]
        if let prev, prev.mtime == stat.mtime, prev.size == stat.size { return }
        var offset = prev?.offset ?? 0
        if let prev, stat.size < prev.size { offset = 0 }

        var rows: [[Any?]] = []
        var aiTitles: [[Any?]] = []
        var customTitles: [[Any?]] = []
        var firstPrompt: [Any?]?
        var wantPrompt = offset == 0  // a resumed scan has already passed the start
        let newOffset = try forEachLine(path: path, from: offset) { line, _ in
            // Cheap pre-filter: only assistant turns with usage, title
            // records and (until found) the first user prompt matter, and
            // each always contains one of these byte sequences.
            let maybePrompt = wantPrompt && contains(line, "\"type\":\"user\"")
            guard maybePrompt || contains(line, "\"usage\"") || contains(line, "-title\"") else { return }
            guard let obj = parseJSONObject(line) else { return }
            let type = obj["type"] as? String

            if maybePrompt && type == "user" {
                if let text = Self.firstPromptText(obj), let sid = nonEmptyString(obj["sessionId"]) {
                    firstPrompt = [sid, text]
                    wantPrompt = false
                }
                return
            }

            if type == "ai-title" {
                if let title = nonEmptyString(obj["aiTitle"]), let sid = nonEmptyString(obj["sessionId"]) {
                    aiTitles.append([sid, title])
                }
                return
            }
            if type == "custom-title" {
                // What Claude Code's own session picker shows; preferred over ai-title.
                if let title = nonEmptyString(obj["customTitle"]), let sid = nonEmptyString(obj["sessionId"]) {
                    customTitles.append([sid, title])
                }
                return
            }
            guard type == "assistant", let msg = obj["message"] as? [String: Any],
                  let usage = msg["usage"] as? [String: Any], !usage.isEmpty,
                  let msgId = msg["id"], truthy(msgId) else { return }

            let model = nonEmptyString(msg["model"]) ?? "unknown"
            let ts = obj["timestamp"] as? String
            let cwd = obj["cwd"] as? String
            var t = TokenFields()
            t.input = intValue(usage["input_tokens"])
            t.output = intValue(usage["output_tokens"])
            t.cacheRead = intValue(usage["cache_read_input_tokens"])
            if let creation = usage["cache_creation"] as? [String: Any], !creation.isEmpty {
                t.cache5m = intValue(creation["ephemeral_5m_input_tokens"])
                t.cache1h = intValue(creation["ephemeral_1h_input_tokens"])
            } else {
                // Older format: assume the default 5-minute TTL.
                t.cache5m = intValue(usage["cache_creation_input_tokens"])
            }
            let c = computeCosts(model: model, timestamp: ts, tokens: t)
            let f = dayFields(ts)
            rows.append([msgId, name, model, ts, f.day, f.weekday, f.hour, projectName(fromCwd: cwd), cwd,
                         obj["sessionId"] as? String, t.input, t.output, t.cacheRead, t.cache5m, t.cache1h,
                         nil, nil, c.cost, c.costNoCache, c.costInput, c.costOutput, c.costCache,
                         c.costCacheRead, c.costCacheWrite])
        }
        guard let newOffset else { return }

        if !rows.isEmpty { try db.runMany(messageInsertSQL, rows) }
        if !aiTitles.isEmpty {
            try db.runMany("""
                INSERT INTO session_titles (session_id, ai_title) VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET ai_title = excluded.ai_title
                """, aiTitles)
        }
        if let firstPrompt { try storeFirstPrompts(db, [firstPrompt]) }
        if !customTitles.isEmpty {
            try db.runMany("""
                INSERT INTO session_titles (session_id, custom_title) VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET custom_title = excluded.custom_title
                """, customTitles)
        }
        try db.run("INSERT OR REPLACE INTO file_state (path, mtime, size, offset) VALUES (?, ?, ?, ?)",
                   [path, stat.mtime, stat.size, newOffset])
    }

    // MARK: Session names

    /// The person's own words in a `user` record, or nil for tool results,
    /// injected context (skill bodies, caveats, reminders) and bare slash
    /// commands. A slash command with arguments reads as typed: "/work-issue 5364".
    static func firstPromptText(_ obj: [String: Any]) -> String? {
        guard obj["type"] as? String == "user", !truthy(obj["isMeta"]), !truthy(obj["isSidechain"]) else { return nil }
        var content = (obj["message"] as? [String: Any])?["content"]
        if let blocks = content as? [Any] {
            content = blocks.lazy.compactMap { $0 as? [String: Any] }
                .first { $0["type"] as? String == "text" && nonEmptyString($0["text"]) != nil }?["text"]
        }
        guard var text = (content as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) else { return nil }
        if text.contains("<command-name>") {
            let name = between(text, "<command-name>", "</command-name>")
            let args = between(text, "<command-args>", "</command-args>")
            guard !name.isEmpty, !args.isEmpty else { return nil }
            text = name + " " + args
        } else if text.hasPrefix("<") || text.hasPrefix("Caveat:") {
            return nil
        }
        text = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        if text.hasPrefix("/") && !text.contains(" ") { return nil }  // a bare slash command
        return text.isEmpty ? nil : String(text.prefix(200))
    }

    private static func between(_ text: String, _ start: String, _ end: String) -> String {
        guard let s = text.range(of: start) else { return "" }
        let rest = text[s.upperBound...]
        let e = rest.range(of: end)?.lowerBound ?? rest.endIndex
        return rest[..<e].trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func backfillFirstPrompts(_ db: SQLiteDB, home: String) throws {
        var rows: [[Any?]] = []
        for path in logFiles(home: home) {
            guard let handle = FileHandle(forReadingAtPath: path) else { continue }
            defer { try? handle.close() }
            // Read in chunks and stop at the file's first prompt.
            var buffer = Data()
            var found: [Any?]?
            reading: while found == nil, let chunk = try? handle.read(upToCount: 1 << 16), !chunk.isEmpty {
                buffer.append(chunk)
                while let nl = buffer.firstIndex(of: 0x0A) {
                    let line = Data(buffer[buffer.startIndex..<nl])
                    buffer.removeSubrange(buffer.startIndex...nl)
                    guard contains(line, "\"type\":\"user\""), let obj = parseJSONObject(line),
                          let text = Self.firstPromptText(obj), let sid = nonEmptyString(obj["sessionId"]) else { continue }
                    found = [sid, text]
                    break reading
                }
            }
            if let found { rows.append(found) }
        }
        try storeFirstPrompts(db, rows)
    }

    /// The Claude desktop app keeps its own per-session metadata, including
    /// names it generated and renames made while the CLI wasn't running —
    /// neither of which is ever written into the transcript.
    func syncSessionTitles(_ db: SQLiteDB, home: String) throws {
        let root = home + "/Library/Application Support/Claude/claude-code-sessions"
        guard let walker = FileManager.default.enumerator(atPath: root) else { return }
        var rows: [[Any?]] = []
        while let rel = walker.nextObject() as? String {
            guard rel.hasSuffix(".json"),
                  let data = FileManager.default.contents(atPath: root + "/" + rel),
                  let meta = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let sid = nonEmptyString(meta["cliSessionId"]),
                  let title = nonEmptyString((meta["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)) else { continue }
            rows.append([sid, title, nonEmptyString(meta["titleSource"]) ?? "auto"])
        }
        try storeAppTitles(db, rows)
    }

    // MARK: Windows

    /// Anthropic buckets a session start down to the 10-minute mark.
    static func floorTo10Min(_ dt: PyDateTime) -> PyDateTime { dt.flooredWall(to: 10 * Micros.minute) }

    func fiveHourWindow(_ db: SQLiteDB, now: PyDateTime) throws -> (PyDateTime, PyDateTime) {
        let cutoff = now.adding(days: -Self.fiveHourLookbackDays).dateString()
        let rows = try db.query(
            "SELECT timestamp FROM messages WHERE source = ? AND day >= ? ORDER BY timestamp ASC", [name, cutoff])
        var start: PyDateTime?
        var end: PyDateTime?
        for row in rows {
            guard let dt = PyDateTime.parse(row.string("timestamp")) else { continue }
            if end == nil || dt >= end! {
                start = Self.floorTo10Min(dt)
                end = start!.adding(micros: Self.fiveHourPeriod)
            }
        }
        if let start, let end { return (start, end) }
        let s = Self.floorTo10Min(now)
        return (s, s.adding(micros: Self.fiveHourPeriod))
    }

    func heuristicWindow(_ db: SQLiteDB, type: String, now: PyDateTime) throws -> (PyDateTime, PyDateTime)? {
        switch type {
        case "five_hour":
            return try fiveHourWindow(db, now: now)
        case "weekly":
            let anchor = Self.weeklyAnchor
            let periods = floorDiv(now.micros - anchor.micros, Self.weeklyPeriod)
            let start = anchor.adding(micros: periods * Self.weeklyPeriod)
            return (start, start.adding(micros: Self.weeklyPeriod))
        default:
            return nil
        }
    }

    func remoteStatus(_ db: SQLiteDB) throws -> [String: Any] {
        var status: [String: String] = [:]
        for row in try db.query("""
            SELECT key, value FROM meta
            WHERE key IN ('claude_remote_usage_last_error', 'claude_remote_usage_last_attempt_at')
            """) {
            if let k = row.string("key") { status[k] = row.string("value") }
        }
        return [
            "last_attempt_at": orNull(status["claude_remote_usage_last_attempt_at"]),
            "last_error": orNull(nonEmptyString(status["claude_remote_usage_last_error"])),
        ]
    }

    func remoteWindows(_ db: SQLiteDB) throws -> RemoteWindows {
        var result = RemoteWindows(remoteStatus: try remoteStatus(db), remotePolledAt: NSNull())
        guard let remote = try db.queryOne("""
            SELECT polled_at, five_hour_pct, five_hour_resets_at, weekly_pct, weekly_resets_at
            FROM remote_usage_polls ORDER BY polled_at DESC LIMIT 1
            """) else { return result }
        result.remotePolledAt = remote["polled_at"]
        if let end = PyDateTime.parse(nonEmptyString(remote["five_hour_resets_at"])) {
            result.fiveHour = WindowBounds(start: end.adding(micros: -Self.fiveHourPeriod), end: end,
                                           remotePct: remote["five_hour_pct"])
        }
        if let end = PyDateTime.parse(nonEmptyString(remote["weekly_resets_at"])) {
            result.weekly = WindowBounds(start: end.adding(micros: -Self.weeklyPeriod), end: end,
                                         remotePct: remote["weekly_pct"])
        }
        return result
    }

    // MARK: Remote polling (network part runs off the store queue)

    /// Claude Code's own OAuth token from the Keychain. Read through
    /// /usr/bin/security, like the Python dashboard, so an existing
    /// "Always Allow" grant keeps working without a new prompt.
    static func loadOAuthToken() throws -> String {
        let result: (status: Int32, stdout: Data)
        do {
            result = try runProcess("/usr/bin/security", ["find-generic-password", "-s", keychainService, "-w"], timeout: 10)
        } catch {
            throw ValidationError(description: "could not read macOS Keychain: \(error)")
        }
        guard result.status == 0 else {
            throw ValidationError(description: "no '\(keychainService)' Keychain entry found — is Claude Code installed and logged in?")
        }
        guard let obj = try? JSONSerialization.jsonObject(with: result.stdout) as? [String: Any],
              let oauth = obj["claudeAiOauth"] as? [String: Any], let token = oauth["accessToken"] else {
            throw ValidationError(description: "unexpected Keychain entry format")
        }
        guard let token = token as? String, !token.isEmpty else {
            throw ValidationError(description: "Keychain entry has no access token")
        }
        return token
    }

    static func fetchRemoteUsage() throws -> [String: Any] {
        let token = try loadOAuthToken()
        var request = URLRequest(url: usageURL, timeoutInterval: 15)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("2023-06-01", forHTTPHeaderField: "anthropic-version")
        let (data, response) = try fetch(request)
        if response.statusCode >= 400 {
            throw ValidationError(description: "HTTP \(response.statusCode) from Claude usage API — token may be expired; use Claude Code to refresh it")
        }
        guard (response.value(forHTTPHeaderField: "Content-Type") ?? "").contains("application/json") else {
            throw ValidationError(description: "Claude usage API did not return JSON")
        }
        guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ValidationError(description: "Claude usage API returned unexpected JSON")
        }
        return obj
    }
}

/// Synchronous URLSession call; errors are worded like the Python client's.
func fetch(_ request: URLRequest) throws -> (Data, HTTPURLResponse) {
    let done = DispatchSemaphore(value: 0)
    var result: Result<(Data, HTTPURLResponse), Error>!
    URLSession.shared.dataTask(with: request) { data, response, error in
        if let error {
            result = .failure(ValidationError(description: "network error reaching Claude usage API: \(error.localizedDescription)"))
        } else if let http = response as? HTTPURLResponse {
            result = .success((data ?? Data(), http))
        } else {
            result = .failure(ValidationError(description: "network error reaching Claude usage API: no HTTP response"))
        }
        done.signal()
    }.resume()
    done.wait()
    return try result.get()
}

func runProcess(_ executable: String, _ arguments: [String], timeout: TimeInterval) throws -> (status: Int32, stdout: Data) {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    let out = Pipe()
    process.standardOutput = out
    process.standardError = FileHandle.nullDevice
    try process.run()
    var data = Data()
    let finished = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        data = out.fileHandleForReading.readDataToEndOfFile()
        finished.signal()
    }
    if finished.wait(timeout: .now() + timeout) == .timedOut {
        process.terminate()
        throw ValidationError(description: "\(executable) timed out")
    }
    process.waitUntilExit()
    return (process.terminationStatus, data)
}
