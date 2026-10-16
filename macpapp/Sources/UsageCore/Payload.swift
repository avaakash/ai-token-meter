import Foundation

public let usagePeriods: [(String, Int)] = [
    ("1d", 1), ("3d", 3), ("7d", 7), ("2w", 14), ("1m", 30), ("3m", 90), ("6m", 180), ("1y", 365),
]

/// A rename made in an app outranks one recorded in the transcript (the app
/// can rename a session whose CLI isn't running, which never reaches the
/// log); generated titles come after any rename. The first prompt is
/// returned separately so the UI can present it as a quote.
private let sessionTitleColumns = """
    (SELECT COALESCE(CASE WHEN st.app_title_source = 'user' THEN st.app_title END,
                     st.custom_title, st.app_title, st.ai_title)
     FROM session_titles st WHERE st.session_id = messages.session_id) AS title,
    (SELECT st.first_prompt FROM session_titles st WHERE st.session_id = messages.session_id) AS first_prompt
    """

let sessionColumns = """
    session_id, source,
    (SELECT project FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS project,
    (SELECT cwd FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS cwd,
    MIN(timestamp) AS start_ts,
    MAX(timestamp) AS end_ts,
    COUNT(*) AS message_count,
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
    COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
    COALESCE(SUM(cost), 0) AS cost,
    COALESCE(SUM(cost_input), 0) AS cost_input,
    COALESCE(SUM(cost_output), 0) AS cost_output,
    COALESCE(SUM(cost_cache), 0) AS cost_cache,
    COALESCE(SUM(cost_cache_read), 0) AS cost_cache_read,
    COALESCE(SUM(cost_cache_write), 0) AS cost_cache_write,
    GROUP_CONCAT(DISTINCT model) AS models,
    \(sessionTitleColumns)
    """

private let lookupColumns = """
    session_id, source,
    (SELECT project FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS project,
    (SELECT cwd FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS cwd,
    MIN(timestamp) AS start_ts,
    MAX(timestamp) AS end_ts,
    COUNT(*) AS message_count,
    COALESCE(SUM(input_tokens), 0) AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens,
    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
    COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
    COALESCE(SUM(cost), 0) AS cost,
    COALESCE(SUM(cost_input), 0) AS cost_input,
    COALESCE(SUM(cost_output), 0) AS cost_output,
    COALESCE(SUM(cost_cache_read), 0) AS cost_cache_read,
    COALESCE(SUM(cost_cache_write), 0) AS cost_cache_write,
    GROUP_CONCAT(DISTINCT model) AS models,
    \(sessionTitleColumns)
    """

/// WHERE clause with the source filter first, so callers can append their
/// own extra-condition params after the returned ones.
func whereClause(_ source: String, _ extra: String...) -> (String, [Any?]) {
    var conditions: [String] = []
    var params: [Any?] = []
    if source != "all" {
        conditions.append("source = ?")
        params.append(source)
    }
    conditions += extra
    return conditions.isEmpty ? ("", []) : ("WHERE " + conditions.joined(separator: " AND "), params)
}

extension UsageStore {
    /// Rolling-window totals keyed by period. Cutoffs use the stored
    /// timestamps' own format (UTC, millisecond "Z"), so string comparison
    /// is time comparison.
    func buildPeriodTotals(_ source: String, now: PyDateTime = .now()) throws -> [String: Any] {
        let (whereSQL, params) = whereClause(source, "timestamp >= ?")
        var periods: [String: Any] = [:]
        for (key, days) in usagePeriods {
            let cutoff = now.adding(days: -days)
            let c = cutoff.wall
            let since = String(format: "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ",
                               c.year, c.month, c.day, c.hour, c.minute, c.second, c.micro / 1000)
            guard var row = try db.queryOne("""
                SELECT
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                    COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(cost), 0) AS cost,
                    COUNT(*) AS message_count,
                    COUNT(DISTINCT session_id) AS session_count
                FROM messages \(whereSQL)
                """, params + [since])?.dict else { continue }
            row["days"] = days
            row["since"] = since
            periods[key] = row
        }
        return periods
    }

    public func buildUsagePayload(_ source: String) throws -> [String: Any] {
        try scanAll()
        let (whereSQL, whereParams) = whereClause(source)

        let totals = try db.queryOne("""
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(cost_no_cache), 0) AS cost_no_cache,
                COUNT(*) AS message_count,
                COUNT(DISTINCT session_id) AS session_count
            FROM messages \(whereSQL)
            """, whereParams)!

        let bySource = try db.query("""
            SELECT source,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COUNT(*) AS message_count,
                COUNT(DISTINCT session_id) AS session_count
            FROM messages
            GROUP BY source
            """)

        let byModel = try db.query("""
            SELECT source, model,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(cost_input), 0) AS cost_input,
                COALESCE(SUM(cost_output), 0) AS cost_output,
                COALESCE(SUM(cost_cache), 0) AS cost_cache,
                COALESCE(SUM(cost_cache_read), 0) AS cost_cache_read,
                COALESCE(SUM(cost_cache_write), 0) AS cost_cache_write,
                COUNT(*) AS message_count
            FROM messages \(whereSQL)
            GROUP BY source, model
            ORDER BY cost DESC
            """, whereParams)

        let (dayWhere, dayParams) = whereClause(source, "day != 'unknown'")
        let byDay = try db.query("""
            SELECT day,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(cost_input), 0) AS cost_input,
                COALESCE(SUM(cost_output), 0) AS cost_output,
                COALESCE(SUM(cost_cache_read), 0) AS cost_cache_read,
                COALESCE(SUM(cost_cache_write), 0) AS cost_cache_write
            FROM messages \(dayWhere)
            GROUP BY day
            ORDER BY day DESC
            LIMIT 30
            """, dayParams)

        let (heatWhere, heatParams) = whereClause(source, "day != 'unknown'", "day >= ?")
        let heatmapDays = try db.query("""
            SELECT day,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COUNT(*) AS message_count
            FROM messages \(heatWhere)
            GROUP BY day
            ORDER BY day ASC
            """, heatParams + [localDateString(daysAgo: 375)])

        let byProject = try db.query("""
            SELECT source, project,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(cost_input), 0) AS cost_input,
                COALESCE(SUM(cost_output), 0) AS cost_output,
                COALESCE(SUM(cost_cache), 0) AS cost_cache,
                COALESCE(SUM(cost_cache_read), 0) AS cost_cache_read,
                COALESCE(SUM(cost_cache_write), 0) AS cost_cache_write,
                COUNT(*) AS message_count
            FROM messages \(whereSQL)
            GROUP BY source, project
            ORDER BY cost DESC
            LIMIT 15
            """, whereParams)

        let (sessionWhere, sessionParams) = whereClause(source, "session_id IS NOT NULL")
        let topSessions = try db.query(
            "SELECT \(sessionColumns) FROM messages \(sessionWhere) GROUP BY session_id ORDER BY cost DESC LIMIT 10",
            sessionParams)
        let lastSessions = try db.query(
            "SELECT \(sessionColumns) FROM messages \(sessionWhere) GROUP BY session_id ORDER BY end_ts DESC LIMIT 10",
            sessionParams)

        let topDay = try db.queryOne("""
            SELECT day, COALESCE(SUM(cost), 0) AS cost
            FROM messages \(dayWhere)
            GROUP BY day
            ORDER BY cost DESC
            LIMIT 1
            """, dayParams)

        let (hwWhere, hwParams) = whereClause(source, "weekday IS NOT NULL", "hour IS NOT NULL")
        let hourWeekday = try db.query("""
            SELECT weekday, hour,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                COUNT(*) AS message_count
            FROM messages \(hwWhere)
            GROUP BY weekday, hour
            """, hwParams)

        let spans = try db.query("""
            SELECT session_id,
                (SELECT project FROM messages m2 WHERE m2.session_id = messages.session_id ORDER BY m2.timestamp ASC LIMIT 1) AS project,
                MIN(timestamp) AS start_ts,
                MAX(timestamp) AS end_ts, SUM(cost) AS cost, COUNT(*) AS message_count
            FROM messages \(sessionWhere)
            GROUP BY session_id
            """, sessionParams)
        var longest: Any = NSNull()
        var longestSeconds = -1.0
        for row in spans {
            guard let start = PyDateTime.parse(row.string("start_ts")), let end = PyDateTime.parse(row.string("end_ts")) else { continue }
            let seconds = end.seconds(since: start)
            if seconds > longestSeconds {
                longestSeconds = seconds
                var d = row.dict
                d["duration_seconds"] = seconds
                longest = d
            }
        }

        let (todayWhere, todayParams) = whereClause(source, "day = ?")
        let today = try db.queryOne("""
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cost), 0) AS cost
            FROM messages \(todayWhere)
            """, todayParams + [localDateString()])!

        let periods = try buildPeriodTotals(source)
        var limits: [String: Any] = [:]
        for s in sources { limits[s.name] = try buildLimitWindows(s.name) }
        var pricing: [String: Any] = [:]
        for s in sources {
            pricing[s.name] = ["last_verified": orNull(s.pricing.lastVerified), "source": orNull(s.pricing.sourceNote)]
        }

        return [
            "generated_at": PyDateTime.now().isoformat(),
            "source": source,
            "pricing": pricing,
            "totals": totals.dict,
            "by_source": bySource.map(\.dict),
            "today": today.dict,
            "periods": periods,
            "limits": limits,
            "by_model": byModel.map(\.dict),
            "by_day": byDay.map(\.dict),
            "heatmap_days": heatmapDays.map(\.dict),
            "by_project": byProject.map(\.dict),
            "top_sessions": topSessions.map(\.dict),
            "last_sessions": lastSessions.map(\.dict),
            "top_day": orNull(topDay?.dict),
            "longest_session": longest,
            "hour_weekday": hourWeekday.map(\.dict),
        ]
    }

    /// Sessions by exact ID or ID prefix, each with a per-model breakdown.
    public func findSessions(_ query: String, source: String = "all", limit: Int = 20) throws -> [[String: Any]] {
        let query = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { return [] }
        try scanAll()
        let pattern = query.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "%", with: "\\%")
            .replacingOccurrences(of: "_", with: "\\_") + "%"
        let (whereSQL, params) = whereClause(source, "session_id LIKE ? ESCAPE '\\'")
        let rows = try db.query("""
            SELECT \(lookupColumns)
            FROM messages
            \(whereSQL)
            GROUP BY session_id
            ORDER BY cost DESC
            LIMIT ?
            """, params + [pattern, limit])
        return try rows.map { row in
            var d = row.dict
            d["by_model"] = try db.query("""
                SELECT model,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                    COALESCE(SUM(cache_5m_tokens + cache_1h_tokens), 0) AS cache_write_tokens,
                    COALESCE(SUM(cost), 0) AS cost,
                    COUNT(*) AS message_count
                FROM messages
                WHERE session_id = ?
                GROUP BY model
                ORDER BY cost DESC
                """, [row["session_id"]]).map(\.dict)
            return d
        }
    }

    /// Every session, unpaginated; the page sorts and filters client-side.
    public func listAllSessions(_ source: String = "all") throws -> [[String: Any]] {
        try scanAll()
        let (whereSQL, params) = whereClause(source, "session_id IS NOT NULL")
        return try db.query("""
            SELECT \(lookupColumns)
            FROM messages
            \(whereSQL)
            GROUP BY session_id
            ORDER BY end_ts DESC
            """, params).map(\.dict)
    }
}
