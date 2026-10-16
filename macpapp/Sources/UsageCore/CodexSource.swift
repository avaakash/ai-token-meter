import Foundation

final class CodexSource: UsageSource {
    let name = "codex"
    let pricing: Pricing

    // Codex windows aren't labeled; bucket them by their reported length.
    static let fiveHourMinutes = 200.0...400.0
    static let weeklyMinutes = 9000.0...11500.0
    static let appServerTimeout: TimeInterval = 15
    static let fallbackBinaryDirs = ["/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/bin"]

    init(pricing: Pricing) { self.pricing = pricing }

    func logFiles(home: String) -> [String] {
        jsonlFiles(under: home + "/.codex/sessions", recursive: true)
            + jsonlFiles(under: home + "/.codex/archived_sessions", recursive: false)
    }

    /// `input` includes cached reads and writes (OpenAI semantics); service
    /// tiers scale every rate.
    func computeCosts(model: String?, timestamp: String?, tokens t: TokenFields) -> Costs {
        let cacheWrite = min(max(0, t.cache5m), max(0, t.input - t.cacheRead))
        let freshInput = max(0, t.input - t.cacheRead - cacheWrite)
        let rate = pricing.resolveRate(model: model, timestamp: timestamp, promptTokens: t.input)
        let mult = pricing.tierMultiplier(nonEmptyString(t.serviceTier))
        let inPPT = rate.input * mult / 1_000_000.0
        let outPPT = rate.output * mult / 1_000_000.0
        let cachedPPT = (rate.cachedInput ?? rate.input) * mult / 1_000_000.0
        let writePPT = (rate.cacheWrite ?? rate.input) * mult / 1_000_000.0
        var c = Costs()
        c.costInput = Double(freshInput) * inPPT
        c.costCacheRead = Double(t.cacheRead) * cachedPPT
        c.costOutput = Double(t.output) * outPPT
        c.costCacheWrite = Double(cacheWrite) * writePPT
        c.costCache = c.costCacheRead + c.costCacheWrite
        c.cost = c.costInput + c.costOutput + c.costCache
        c.costNoCache = Double(t.input) * inPPT + Double(t.output) * outPPT
        return c
    }

    func tokenFields(from row: Row) -> TokenFields {
        TokenFields(input: row.int("input_tokens") ?? 0, output: row.int("output_tokens") ?? 0,
                    cacheRead: row.int("cache_read_tokens") ?? 0, cache5m: row.int("cache_5m_tokens") ?? 0,
                    reasoningOutput: row.int("reasoning_output_tokens") ?? 0, serviceTier: row.string("service_tier"))
    }

    func scanFile(_ db: SQLiteDB, path: String, state: [String: FileState], home: String) throws {
        guard let stat = fileStat(path) else { return }
        var prev = state[path]

        // Codex later moves finished sessions to archived_sessions/<basename>.
        // Adopt the old path's progress instead of re-reading the file and
        // inserting every row a second time under the new path.
        var renamedFrom: String?
        if prev == nil {
            let codexRoot = home + "/.codex/"
            let basename = (path as NSString).lastPathComponent
            for (otherPath, info) in state.sorted(by: { $0.key < $1.key })
            where otherPath != path && otherPath.hasPrefix(codexRoot)
                && (otherPath as NSString).lastPathComponent == basename
                && !FileManager.default.fileExists(atPath: otherPath) {
                prev = info
                renamedFrom = otherPath
                break
            }
        }

        if let p = prev, p.mtime == stat.mtime, p.size == stat.size {
            if let renamedFrom { try persist(db, path, stat, p, renamedFrom: renamedFrom) }
            return
        }

        var offset = prev?.offset ?? 0
        let truncated = prev.map { stat.size < $0.size } ?? false
        if truncated { offset = 0 }

        // Model/session/cwd come from earlier records in the file, so seed
        // them from the previous scan unless the file was rewritten.
        let carry = truncated ? nil : prev
        var sessionId = carry?.carrySessionId
        var cwd = carry?.carryCwd
        var model = carry?.carryModel
        var serviceTier = nonEmptyString(carry?.carryServiceTier) ?? "default"
        var latestRateLimits: [String: Any]?
        var rows: [[Any?]] = []

        let newOffset = try forEachLine(path: path, from: offset) { line, lineStart in
            guard contains(line, "token_count") || contains(line, "turn_context")
                || contains(line, "session_meta") || contains(line, "thread_settings_applied") else { return }
            guard let obj = parseJSONObject(line) else { return }
            let type = obj["type"] as? String
            let payload = obj["payload"] as? [String: Any] ?? [:]

            if type == "session_meta" {
                sessionId = nonEmptyString(payload["id"]) ?? nonEmptyString(payload["session_id"]) ?? sessionId
                cwd = nonEmptyString(payload["cwd"]) ?? cwd
                return
            }
            if type == "turn_context" {
                model = nonEmptyString(payload["model"]) ?? model
                cwd = nonEmptyString(payload["cwd"]) ?? cwd
                return
            }
            guard type == "event_msg" else { return }
            let eventType = payload["type"] as? String
            if eventType == "thread_settings_applied" {
                let settings = payload["thread_settings"] as? [String: Any] ?? [:]
                model = nonEmptyString(settings["model"]) ?? model
                serviceTier = nonEmptyString(settings["service_tier"]) ?? serviceTier
                return
            }
            guard eventType == "token_count" else { return }
            let info = payload["info"] as? [String: Any] ?? [:]
            guard let last = info["last_token_usage"] as? [String: Any], !last.isEmpty else { return }
            if let rl = payload["rate_limits"] as? [String: Any], !rl.isEmpty { latestRateLimits = rl }

            let ts = obj["timestamp"] as? String
            var t = TokenFields()
            t.input = intValue(last["input_tokens"])
            t.output = intValue(last["output_tokens"])
            t.cacheRead = intValue(last["cached_input_tokens"])
            t.cache5m = intValue(last["cache_write_input_tokens"])
            t.reasoningOutput = intValue(last["reasoning_output_tokens"])
            t.serviceTier = serviceTier
            let resolvedModel = model ?? "unknown"
            let c = computeCosts(model: resolvedModel, timestamp: ts, tokens: t)
            let f = dayFields(ts)
            rows.append(["codex:\(path):\(lineStart)", name, resolvedModel, ts, f.day, f.weekday, f.hour,
                         projectName(fromCwd: cwd), cwd, sessionId, t.input, t.output, t.cacheRead, t.cache5m, 0,
                         t.reasoningOutput, serviceTier, c.cost, c.costNoCache, c.costInput, c.costOutput,
                         c.costCache, c.costCacheRead, c.costCacheWrite])
        }
        guard let newOffset else { return }

        if !rows.isEmpty { try db.runMany(messageInsertSQL, rows) }
        if let latestRateLimits { try Self.storeRateLimits(db, latestRateLimits, origin: "log") }
        try persist(db, path, stat, FileState(mtime: stat.mtime, size: stat.size, offset: newOffset, carryModel: model,
                                              carryServiceTier: serviceTier, carrySessionId: sessionId, carryCwd: cwd),
                    renamedFrom: renamedFrom)
    }

    private func persist(_ db: SQLiteDB, _ path: String, _ stat: (mtime: Double, size: Int), _ info: FileState,
                         renamedFrom: String?) throws {
        try db.run("""
            INSERT OR REPLACE INTO file_state
            (path, mtime, size, offset, carry_model, carry_service_tier, carry_session_id, carry_cwd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [path, stat.mtime, stat.size, info.offset, info.carryModel, info.carryServiceTier,
                  info.carrySessionId, info.carryCwd])
        if let renamedFrom { try db.run("DELETE FROM file_state WHERE path = ?", [renamedFrom]) }
    }

    // MARK: Session names

    /// Thread names live in session_index.jsonl, not the session logs; the
    /// latest entry for an id (a rename) wins.
    func syncSessionTitles(_ db: SQLiteDB, home: String) throws {
        guard let data = FileManager.default.contents(atPath: home + "/.codex/session_index.jsonl") else { return }
        var names: [String: (name: String, stamp: String)] = [:]
        for line in data.split(separator: 0x0A) {
            guard let entry = parseJSONObject(Data(line)), let sid = nonEmptyString(entry["id"]),
                  let name = nonEmptyString((entry["thread_name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines))
            else { continue }
            let stamp = entry["updated_at"] as? String ?? ""
            if let existing = names[sid], stamp < existing.stamp { continue }
            names[sid] = (name, stamp)
        }
        try storeAppTitles(db, names.sorted { $0.key < $1.key }.map { [$0.key, $0.value.name, "codex"] })
    }

    /// Codex's own user messages are wrapped in injected context, so there's
    /// no reliable first prompt to recover.
    func backfillFirstPrompts(_ db: SQLiteDB, home: String) throws {}

    // MARK: Rate-limit windows

    /// The live app-server reply is camelCase, logged events are snake_case.
    static func normalizeWindow(_ win: Any?) -> Any {
        guard let win = win as? [String: Any], !win.isEmpty else { return NSNull() }
        func pick(_ snake: String, _ camel: String) -> Any { win.keys.contains(snake) ? win[snake]! : orNull(win[camel]) }
        return [
            "used_percent": pick("used_percent", "usedPercent"),
            "window_minutes": pick("window_minutes", "windowDurationMins"),
            "resets_at": pick("resets_at", "resetsAt"),
        ]
    }

    static func normalizeRateLimits(_ rl: [String: Any]) -> [String: Any] {
        func pick(_ snake: String, _ camel: String) -> Any { rl.keys.contains(snake) ? rl[snake]! : orNull(rl[camel]) }
        return [
            "limit_id": pick("limit_id", "limitId"),
            "limit_name": pick("limit_name", "limitName"),
            "primary": normalizeWindow(rl["primary"]),
            "secondary": normalizeWindow(rl["secondary"]),
            "plan_type": pick("plan_type", "planType"),
        ]
    }

    static func storeRateLimits(_ db: SQLiteDB, _ rateLimits: [String: Any], origin: String) throws {
        let blob: [String: Any] = [
            "rate_limits": normalizeRateLimits(rateLimits),
            "observed_at": PyDateTime.now().isoformat(),
            "origin": origin,
        ]
        let json = String(decoding: try JSONSerialization.data(withJSONObject: blob), as: UTF8.self)
        try db.run("INSERT OR REPLACE INTO meta (key, value) VALUES ('codex_rate_limits', ?)", [json])
    }

    /// `resets_at` is a Unix epoch here, unlike Claude's ISO string.
    static func parseResetsAt(_ v: Any?) -> PyDateTime? {
        if let n = v as? NSNumber { return PyDateTime.fromTimestamp(n.doubleValue) }
        return PyDateTime.parse(v as? String)
    }

    static func mapWindow(_ win: [String: Any], now: PyDateTime) -> WindowBounds? {
        guard truthy(win["resets_at"]), let minutes = (win["window_minutes"] as? NSNumber)?.doubleValue, minutes != 0,
              let end = parseResetsAt(win["resets_at"]) else { return nil }
        // A reset time in the past means a stale snapshot, not a live quota.
        guard end > now else { return nil }
        return WindowBounds(start: end.adding(seconds: -minutes * 60), end: end, remotePct: orNull(win["used_percent"]))
    }

    func remoteStatus(_ db: SQLiteDB) throws -> [String: Any] {
        var status: [String: String] = [:]
        for row in try db.query("""
            SELECT key, value FROM meta
            WHERE key IN ('codex_remote_usage_last_error', 'codex_remote_usage_last_attempt_at')
            """) {
            if let k = row.string("key") { status[k] = row.string("value") }
        }
        return [
            "last_attempt_at": orNull(status["codex_remote_usage_last_attempt_at"]),
            "last_error": orNull(nonEmptyString(status["codex_remote_usage_last_error"])),
        ]
    }

    func remoteWindows(_ db: SQLiteDB) throws -> RemoteWindows {
        var result = RemoteWindows(remoteStatus: try remoteStatus(db), remotePolledAt: NSNull())
        guard let value = try db.queryOne("SELECT value FROM meta WHERE key = 'codex_rate_limits'")?.string("value"),
              let data = try? JSONSerialization.jsonObject(with: Data(value.utf8)) as? [String: Any] else { return result }
        let rateLimits = data["rate_limits"] as? [String: Any] ?? [:]
        result.remotePolledAt = orNull(data["observed_at"])
        let now = PyDateTime.now()
        for key in ["primary", "secondary"] {
            guard let win = rateLimits[key] as? [String: Any],
                  let minutes = (win["window_minutes"] as? NSNumber)?.doubleValue, minutes != 0,
                  let mapped = Self.mapWindow(win, now: now) else { continue }
            if Self.fiveHourMinutes.contains(minutes) && result.fiveHour == nil {
                result.fiveHour = mapped
            } else if Self.weeklyMinutes.contains(minutes) && result.weekly == nil {
                result.weekly = mapped
            }
        }
        return result
    }

    /// No local heuristic for Codex windows: metered billing has no quota.
    func heuristicWindow(_ db: SQLiteDB, type: String, now: PyDateTime) throws -> (PyDateTime, PyDateTime)? { nil }

    // MARK: Live polling through `codex app-server` (runs off the store queue)

    static func findBinary() -> String? {
        let pathDirs = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        for dir in pathDirs + fallbackBinaryDirs.map({ ($0 as NSString).expandingTildeInPath }) {
            let candidate = dir + "/codex"
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return nil
    }

    /// Spawns `codex app-server`, does the JSON-RPC initialize handshake,
    /// sends each request and returns {id: result}.
    static func callAppServer(_ requests: [(id: String, method: String)]) throws -> [String: Any] {
        guard let binary = findBinary() else {
            throw ValidationError(description: "could not find the 'codex' CLI binary — is it installed?")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: binary)
        process.arguments = ["app-server"]
        // Apps opened from Finder get launchd's minimal PATH; an npm-installed
        // codex is a node script that needs `node` resolvable.
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = ((env["PATH"].map { [$0] } ?? []) + fallbackBinaryDirs.map { ($0 as NSString).expandingTildeInPath })
            .joined(separator: ":")
        process.environment = env
        let stdin = Pipe(), stdout = Pipe()
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch {
            throw ValidationError(description: "could not launch '\(binary) app-server': \(error.localizedDescription)")
        }
        defer {
            stdout.fileHandleForReading.readabilityHandler = nil
            process.terminate()
            let deadline = Date().addingTimeInterval(3)
            while process.isRunning && Date() < deadline { usleep(20_000) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }

        let lock = NSLock()
        var pending = Set(requests.map(\.id))
        var results: [String: Any] = [:]
        var errors: [Any] = []
        var buffer = Data()
        let finished = DispatchSemaphore(value: 0)
        var signalled = false
        stdout.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData
            lock.lock()
            defer { lock.unlock() }
            guard !signalled else { return }
            if chunk.isEmpty {
                signalled = true
                finished.signal()
                return
            }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: 0x0A) {
                let line = buffer[buffer.startIndex..<nl]
                buffer.removeSubrange(buffer.startIndex...nl)
                guard let msg = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                      let id = msg["id"] as? String, pending.contains(id) else { continue }
                if let error = msg["error"] { errors.append(error) } else { results[id] = orNull(msg["result"]) }
                pending.remove(id)
            }
            if pending.isEmpty {
                signalled = true
                finished.signal()
            }
        }

        func send(_ message: [String: Any]) throws {
            var data = try JSONSerialization.data(withJSONObject: message)
            data.append(0x0A)
            try stdin.fileHandleForWriting.write(contentsOf: data)
        }
        do {
            try send(["jsonrpc": "2.0", "id": "init", "method": "initialize",
                      "params": ["clientInfo": ["name": "token-meter", "version": "1.0"]]])
            for request in requests {
                try send(["jsonrpc": "2.0", "id": request.id, "method": request.method, "params": [String: Any]()])
            }
        } catch {
            throw ValidationError(description: "could not talk to '\(binary) app-server': \(error.localizedDescription)")
        }

        _ = finished.wait(timeout: .now() + appServerTimeout)
        lock.lock()
        defer { lock.unlock() }
        signalled = true
        if !pending.isEmpty {
            throw ValidationError(description: "'codex app-server' timed out waiting for: \(pending.sorted().joined(separator: ", "))")
        }
        if let first = errors.first {
            let message = (first as? [String: Any])?["message"].map { "\($0)" } ?? "\(first)"
            throw ValidationError(description: "codex app-server error: \(message)")
        }
        return results
    }
}
