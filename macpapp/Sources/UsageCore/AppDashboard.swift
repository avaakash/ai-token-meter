import Foundation

/// Everything the Mac app's dashboard draws for one source filter and
/// period, in a single call. Totals come from message timestamps, so a
/// session that spans the period boundary is split correctly.
private let periodSums = """
    COALESCE(SUM(cost), 0) AS cost,
    COALESCE(SUM(cost_no_cache), 0) AS cost_no_cache,
    COALESCE(SUM(cost_input), 0) AS cost_input,
    COALESCE(SUM(cost_output), 0) AS cost_output,
    COALESCE(SUM(cost_cache_read), 0) AS cost_cache_read,
    COALESCE(SUM(cost_cache_write), 0) AS cost_cache_write,
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
    COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
    COUNT(*) AS message_count,
    COUNT(DISTINCT session_id) AS session_count
    """

/// The stored timestamps' own format (UTC, millisecond "Z"), so string
/// comparison is time comparison.
private func utcCutoff(_ now: PyDateTime, days: Int) -> String {
    let c = now.adding(days: -days).wall
    return String(format: "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", c.year, c.month, c.day, c.hour, c.minute, c.second, c.micro / 1000)
}

/// A worktree checkout's path, shown as the repo it belongs to.
private func repoPath(_ cwd: String?) -> Any {
    guard var path = cwd, !path.isEmpty else { return NSNull() }
    for marker in ["/.claude/worktrees/", "/.worktrees/"] {
        if let r = path.range(of: marker) { path = String(path[..<r.lowerBound]); break }
    }
    return path
}

extension UsageStore {
    public func buildAppDashboard(_ source: String, days: Int) throws -> [String: Any] {
        try scanAll()
        let now = PyDateTime.now()
        let since = utcCutoff(now, days: days), prevSince = utcCutoff(now, days: 2 * days)
        let (inPeriod, periodParams) = whereClause(source, "timestamp >= ?")
        let (inPrev, prevParams) = whereClause(source, "timestamp >= ?", "timestamp < ?")

        var period = try db.queryOne("SELECT \(periodSums) FROM messages \(inPeriod)", periodParams + [since])!.dict
        period["days"] = days
        period["since"] = since
        period["prev_cost"] = try db.queryOne("SELECT COALESCE(SUM(cost), 0) AS cost FROM messages \(inPrev)",
                                              prevParams + [prevSince, since])?["cost"] ?? 0

        // Per source, regardless of the filter, so "Both" can show the split.
        let prevBySource = Dictionary(uniqueKeysWithValues: try db.query(
            "SELECT source, COALESCE(SUM(cost), 0) AS cost FROM messages WHERE timestamp >= ? AND timestamp < ? GROUP BY source",
            [prevSince, since]).map { ($0.string("source") ?? "", $0["cost"]) })
        let bySourceRows = Dictionary(uniqueKeysWithValues: try db.query(
            "SELECT source, \(periodSums) FROM messages WHERE timestamp >= ? GROUP BY source", [since])
            .map { ($0.string("source") ?? "", $0.dict) })
        let bySource: [[String: Any]] = sources.map { s in
            var row = bySourceRows[s.name] ?? ["source": s.name, "cost": 0.0, "message_count": 0, "session_count": 0]
            row["prev_cost"] = prevBySource[s.name] ?? 0
            return row
        }

        let byModel = try db.query(
            "SELECT source, model, \(periodSums) FROM messages \(inPeriod) GROUP BY source, model ORDER BY cost DESC",
            periodParams + [since]).map(\.dict)

        var topModel: [String: (String, Double)] = [:]
        for r in try db.query("SELECT project, model, SUM(cost) AS cost FROM messages \(inPeriod) GROUP BY project, model",
                              periodParams + [since]) {
            let project = r.string("project") ?? "", cost = r.double("cost") ?? 0
            if cost > (topModel[project]?.1 ?? -1) { topModel[project] = (r.string("model") ?? "unknown", cost) }
        }
        // SQLite returns the bare `cwd` from the row holding MAX(timestamp).
        let latestCwd = Dictionary(uniqueKeysWithValues: try db.query(
            "SELECT project, cwd, MAX(timestamp) AS t FROM messages \(inPeriod) GROUP BY project", periodParams + [since])
            .map { ($0.string("project") ?? "", repoPath($0.string("cwd"))) })
        let byProject: [[String: Any]] = try db.query(
            "SELECT project, \(periodSums) FROM messages \(inPeriod) GROUP BY project ORDER BY cost DESC LIMIT 60",
            periodParams + [since]).map { r in
                var d = r.dict
                let project = r.string("project") ?? ""
                d["cwd"] = latestCwd[project] ?? NSNull()
                d["top_model"] = orNull(topModel[project]?.0)
                return d
            }

        let (dayWhere, dayParams) = whereClause(source, "day != 'unknown'", "day >= ?")
        let daily = try db.query(
            "SELECT day, \(periodSums) FROM messages \(dayWhere) GROUP BY day ORDER BY day ASC",
            dayParams + [localDateString(daysAgo: max(days, 7) - 1)]).map(\.dict)

        let (sessionWhere, sessionParams) = whereClause(source, "session_id IS NOT NULL")
        let (spanWhere, spanParams) = whereClause(source, "session_id IS NOT NULL", "timestamp >= ?")
        var longest: Any = NSNull()
        var longestSeconds = -1.0
        var longestId: Any?
        for r in try db.query(
            "SELECT session_id, MIN(timestamp) AS start_ts, MAX(timestamp) AS end_ts FROM messages \(spanWhere) GROUP BY session_id",
            spanParams + [since]) {
            guard let a = PyDateTime.parse(r.string("start_ts")), let b = PyDateTime.parse(r.string("end_ts")) else { continue }
            let seconds = b.seconds(since: a)
            if seconds > longestSeconds { longestSeconds = seconds; longestId = r["session_id"] }
        }
        if let longestId, var row = try db.queryOne(
            "SELECT \(sessionColumns) FROM messages WHERE session_id = ? GROUP BY session_id", [longestId])?.dict {
            row["duration_seconds"] = longestSeconds
            longest = row
        }

        let recent = try db.query(
            "SELECT \(sessionColumns) FROM messages \(sessionWhere) GROUP BY session_id ORDER BY end_ts DESC LIMIT 14",
            sessionParams).map(\.dict)
        let sessionTotal = try db.queryOne(
            "SELECT COUNT(DISTINCT session_id) AS n FROM messages \(sessionWhere)", sessionParams)?["n"] ?? 0

        let (heatWhere, heatParams) = whereClause(source, "day != 'unknown'", "day >= ?")
        let heat = try db.query("""
            SELECT day, COALESCE(SUM(cost), 0) AS cost, COUNT(*) AS message_count, COUNT(DISTINCT session_id) AS session_count
            FROM messages \(heatWhere) GROUP BY day ORDER BY day ASC
            """, heatParams + [localDateString(daysAgo: 375)]).map(\.dict)
        let (hwWhere, hwParams) = whereClause(source, "weekday IS NOT NULL", "hour IS NOT NULL", "timestamp >= ?")
        let hourWeekday = try db.query("""
            SELECT weekday, hour, COALESCE(SUM(cost), 0) AS cost, COUNT(*) AS message_count
            FROM messages \(hwWhere) GROUP BY weekday, hour
            """, hwParams + [utcCutoff(now, days: 365)]).map(\.dict)

        var limits: [String: Any] = [:]
        for s in sources { limits[s.name] = try appLimits(s.name, now: now) }

        return [
            "generated_at": now.isoformat(),
            "source": source,
            "period": period,
            "by_source": bySource,
            "by_model": byModel,
            "by_group": try groupTotals(byModel),
            "by_project": byProject,
            "daily": daily,
            "longest_session": longest,
            "recent_sessions": recent,
            "session_total": sessionTotal,
            "activity": ["days": heat, "hour_weekday": hourWeekday],
            "limits": limits,
        ]
    }

    /// One source's windows, forecast and history, reshaped for the app.
    private func appLimits(_ source: String, now: PyDateTime) throws -> [String: Any] {
        let raw = try buildLimitWindows(source)
        let status = raw["remote_status"] as? [String: Any]
        let lastError = status?["last_error"] as? String
        let polledAt = raw["remote_polled_at"] as? String

        func window(_ key: String) -> Any {
            guard let w = raw[key] as? [String: Any] else { return NSNull() }
            let buckets = (w["buckets"] as? [[String: Any]] ?? []).map { b -> Any in
                guard let start = PyDateTime.parse(b["start"] as? String), start <= now else { return NSNull() }
                return b["cost"] ?? 0
            }
            return [
                "start": w["window_start"]!, "end": w["window_end"]!, "pct": w["remote_pct"] ?? NSNull(),
                "cost": w["cost"] ?? 0, "messages": w["message_count"] ?? 0, "buckets": buckets,
                "prev_cost": (w["previous"] as? [String: Any])?["cost"] ?? 0,
                "estimated": (w["source"] as? String) != "remote",
                "credit": w["credit_estimate"] ?? NSNull(),
            ]
        }

        var forecast: Any = NSNull()
        var history: [[String: Any]] = []
        if raw["weekly"] is [String: Any] {
            forecast = try predictWeeklyUsage(source)["exhaustion"] ?? NSNull()
            history = try listWeeklyUsage(source, limit: 9).map {
                ["start": $0["window_start"]!, "end": $0["window_end"]!, "peak": $0["week_max_percentage"]!]
            }
        }

        let state: String
        if let lastError, lastError.contains("could not find the 'codex' CLI") {
            state = "missing"
        } else if lastError != nil {
            state = "error"
        } else if polledAt != nil {
            state = "ok"
        } else {
            state = "waiting"
        }
        return [
            "status": state, "error": orNull(lastError), "synced_at": orNull(polledAt),
            "five_hour": window("five_hour"), "weekly": window("weekly"),
            "forecast": forecast, "history": history, "budgets": try budgetStatus(source),
        ]
    }
}
