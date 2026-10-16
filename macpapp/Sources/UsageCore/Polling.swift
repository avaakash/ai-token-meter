import Foundation

public let remotePollInterval: TimeInterval = 600

func errorText(_ error: Error) -> String {
    switch error {
    case let e as ValidationError: return e.description
    case let e as SQLiteError: return e.description
    default: return error.localizedDescription
    }
}

private func isPresent(_ v: Any?) -> Bool { v != nil && !(v is NSNull) }

extension UsageStore {
    /// Fetch once and persist the result (or the error), then log a
    /// (percentage, dollar) snapshot per window from this same poll. The
    /// network part runs on the caller's thread, database work on the queue.
    public func pollRemoteUsage(_ source: String) throws -> [String: Any] {
        switch source {
        case claude.name: return try pollClaude()
        case codex.name: return try pollCodex()
        default: throw ValidationError(description: "unknown source: \(source)")
        }
    }

    private func recordPollError(_ prefix: String, _ message: String) throws {
        try sync {
            try db.transaction {
                try setMeta("\(prefix)_remote_usage_last_error", message)
                try setMeta("\(prefix)_remote_usage_last_attempt_at", PyDateTime.now().isoformat())
            }
        }
    }

    /// Daily tracking is secondary: once the core poll has committed, a
    /// failure here must not turn the poll into a reported sync failure.
    private func recordWeeklyFromSnapshot(_ source: String, _ pct: Any, _ snapshot: [String: Any]) {
        guard snapshot["saved"] as? Bool == true else { return }
        do {
            try sync {
                try recordWeeklyDailyMax(source, pct, snapshot["window_start"], snapshot["window_end"],
                                         observedAt: snapshot["created_at"] as? String)
            }
        } catch {
            NSLog("weekly daily-max tracking failed for %@: %@", source, errorText(error))
        }
    }

    private func pollClaude() throws -> [String: Any] {
        let data: [String: Any]
        do {
            data = try ClaudeSource.fetchRemoteUsage()
        } catch {
            try recordPollError("claude", errorText(error))
            return ["ok": false, "error": errorText(error)]
        }

        let fiveHour = data["five_hour"] as? [String: Any] ?? [:]
        let sevenDay = data["seven_day"] as? [String: Any] ?? [:]
        try sync {
            try db.transaction {
                let raw = (try? JSONSerialization.data(withJSONObject: data)).map { String(decoding: $0, as: UTF8.self) }
                try db.run("""
                    INSERT INTO remote_usage_polls
                    (polled_at, five_hour_pct, five_hour_resets_at, weekly_pct, weekly_resets_at, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """, [PyDateTime.now().isoformat(), fiveHour["utilization"], fiveHour["resets_at"],
                          sevenDay["utilization"], sevenDay["resets_at"], raw])
                try setMeta("claude_remote_usage_last_error", "")
                try setMeta("claude_remote_usage_last_attempt_at", PyDateTime.now().isoformat())
            }
        }

        if let pct = fiveHour["utilization"], isPresent(pct) {
            _ = try sync { try createLimitSnapshot("claude", "five_hour", pct) }
        }
        if let pct = sevenDay["utilization"], isPresent(pct) {
            let snapshot = try sync { try createLimitSnapshot("claude", "weekly", pct) }
            recordWeeklyFromSnapshot("claude", pct, snapshot)
        }
        return ["ok": true]
    }

    private func pollCodex() throws -> [String: Any] {
        let results: [String: Any]
        do {
            results = try CodexSource.callAppServer([(id: "rl", method: "account/rateLimits/read")])
        } catch {
            try recordPollError("codex", errorText(error))
            return ["ok": false, "error": errorText(error)]
        }
        let data = results["rl"] as? [String: Any] ?? [:]
        guard let rateLimits = data["rateLimits"] as? [String: Any], !rateLimits.isEmpty else {
            try recordPollError("codex", "codex app-server returned no rateLimits (not authenticated via a ChatGPT plan?)")
            return ["ok": false, "error": "no rateLimits in response"]
        }

        try sync {
            try db.transaction {
                try CodexSource.storeRateLimits(db, rateLimits, origin: "poll")
                try setMeta("codex_remote_usage_last_attempt_at", PyDateTime.now().isoformat())
                try setMeta("codex_remote_usage_last_error", "")
            }
        }

        var weeklyPct: Any?
        let normalized = CodexSource.normalizeRateLimits(rateLimits)
        let now = PyDateTime.now()
        for key in ["primary", "secondary"] {
            guard let win = normalized[key] as? [String: Any],
                  let minutes = (win["window_minutes"] as? NSNumber)?.doubleValue, minutes != 0 else { continue }
            if CodexSource.weeklyMinutes.contains(minutes) {
                weeklyPct = CodexSource.mapWindow(win, now: now)?.remotePct
                break
            }
        }
        if let pct = weeklyPct, isPresent(pct) {
            let snapshot = try sync { try createLimitSnapshot("codex", "weekly", pct) }
            recordWeeklyFromSnapshot("codex", pct, snapshot)
        }
        return ["ok": true]
    }
}
