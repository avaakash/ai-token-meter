import Foundation

/// Python's `float(x)` for a JSON/SQLite value.
func pythonFloat(_ v: Any?) -> Double? {
    switch v {
    case let n as NSNumber: return n.doubleValue
    case let s as String: return Double(s.trimmingCharacters(in: .whitespaces))
    default: return nil
    }
}

func validatePercentage(_ v: Any?) throws -> Double {
    guard let pct = pythonFloat(v) else { throw ValidationError(description: "percentage must be a number") }
    guard pct >= 0 && pct <= 100 else { throw ValidationError(description: "percentage must be between 0 and 100") }
    return pct
}

extension UsageStore {
    static let creditEstimateMinPoints = 20

    // MARK: Rate-limit windows

    /// Tokens/cost for `source` within [start, end), a per-model breakdown,
    /// and `numBuckets` sub-windows of `bucketPeriod` for the mini charts.
    func sumWindow(_ source: String, start: PyDateTime, end: PyDateTime, dayLookbackDays: Int,
                   bucketPeriod: Int64, numBuckets: Int) throws -> [String: Any] {
        let dayCutoff = start.adding(days: -dayLookbackDays).dateString()
        let rows = try db.query("""
            SELECT timestamp, model, input_tokens, output_tokens, cache_read_tokens,
                   cache_5m_tokens, cache_1h_tokens, cost, cost_input, cost_output,
                   cost_cache_read, cost_cache_write
            FROM messages
            WHERE source = ? AND day >= ?
            """, [source, dayCutoff])

        var inputTokens = 0, outputTokens = 0, cacheRead = 0, cacheWrite = 0, messageCount = 0
        var cost = 0.0, costInput = 0.0, costOutput = 0.0, costCacheRead = 0.0, costCacheWrite = 0.0
        var byModel: [String: [String: Any]] = [:]
        var buckets: [[String: Any]] = (0..<numBuckets).map { i in
            [
                "start": start.adding(micros: Int64(i) * bucketPeriod).isoformat(),
                "end": start.adding(micros: Int64(i + 1) * bucketPeriod).isoformat(),
                "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
                "cost": 0.0, "cost_input": 0.0, "cost_output": 0.0, "cost_cache_read": 0.0, "cost_cache_write": 0.0,
                "message_count": 0,
            ]
        }
        func add(_ dict: inout [String: Any], _ key: String, _ v: Int) { dict[key] = (dict[key] as! Int) + v }
        func add(_ dict: inout [String: Any], _ key: String, _ v: Double) { dict[key] = (dict[key] as! Double) + v }

        for r in rows {
            guard let dt = PyDateTime.parse(r.string("timestamp")), dt >= start, dt < end else { continue }
            let it = r.int("input_tokens") ?? 0
            let ot = r.int("output_tokens") ?? 0
            let cr = r.int("cache_read_tokens") ?? 0
            let cw = (r.int("cache_5m_tokens") ?? 0) + (r.int("cache_1h_tokens") ?? 0)
            let c = r.double("cost") ?? 0
            let ci = r.double("cost_input") ?? 0
            let co = r.double("cost_output") ?? 0
            let ccr = r.double("cost_cache_read") ?? 0
            let ccw = r.double("cost_cache_write") ?? 0

            inputTokens += it; outputTokens += ot; cacheRead += cr; cacheWrite += cw
            cost += c; costInput += ci; costOutput += co; costCacheRead += ccr; costCacheWrite += ccw
            messageCount += 1

            let model = nonEmptyString(r["model"]) ?? "unknown"
            var entry = byModel[model] ?? ["cost": 0.0, "input_tokens": 0, "output_tokens": 0, "message_count": 0]
            add(&entry, "cost", c); add(&entry, "input_tokens", it); add(&entry, "output_tokens", ot)
            add(&entry, "message_count", 1)
            byModel[model] = entry

            let idx = floorDiv(dt.micros - start.micros, bucketPeriod)
            if idx >= 0 && idx < numBuckets {
                var b = buckets[Int(idx)]
                add(&b, "input_tokens", it); add(&b, "output_tokens", ot)
                add(&b, "cache_read_tokens", cr); add(&b, "cache_write_tokens", cw)
                add(&b, "cost", c); add(&b, "cost_input", ci); add(&b, "cost_output", co)
                add(&b, "cost_cache_read", ccr); add(&b, "cost_cache_write", ccw)
                add(&b, "message_count", 1)
                buckets[Int(idx)] = b
            }
        }
        return [
            "input_tokens": inputTokens, "output_tokens": outputTokens,
            "cache_read_tokens": cacheRead, "cache_write_tokens": cacheWrite,
            "cost": cost, "cost_input": costInput, "cost_output": costOutput,
            "cost_cache_read": costCacheRead, "cost_cache_write": costCacheWrite,
            "message_count": messageCount, "by_model": byModel, "buckets": buckets,
        ]
    }

    /// The source's own remote window when known, else its local heuristic.
    func buildLimitWindows(_ source: String) throws -> [String: Any] {
        guard let module = self.source(named: source) else { throw ValidationError(description: "unknown source: \(source)") }
        let now = PyDateTime.now()
        let remote = try module.remoteWindows(db)
        var result: [String: Any] = ["remote_status": remote.remoteStatus, "remote_polled_at": remote.remotePolledAt]

        let specs: [(String, Int64, Int, Int, WindowBounds?)] = [
            ("five_hour", Micros.hour, 5, 2, remote.fiveHour),
            ("weekly", Micros.day, 7, 8, remote.weekly),
        ]
        for (type, bucketPeriod, numBuckets, lookback, remoteWindow) in specs {
            let start: PyDateTime, end: PyDateTime, remotePct: Any, winSource: String
            if let remoteWindow {
                (start, end, remotePct, winSource) = (remoteWindow.start, remoteWindow.end, remoteWindow.remotePct, "remote")
            } else if let heuristic = try module.heuristicWindow(db, type: type, now: now) {
                (start, end, remotePct, winSource) = (heuristic.0, heuristic.1, NSNull(), "heuristic")
            } else {
                result[type] = NSNull()
                continue
            }
            let period = end.micros - start.micros
            var window = try sumWindow(source, start: start, end: end, dayLookbackDays: lookback,
                                       bucketPeriod: bucketPeriod, numBuckets: numBuckets)
            let prevStart = start.adding(micros: -period)
            let prev = try sumWindow(source, start: prevStart, end: start, dayLookbackDays: lookback + 1,
                                     bucketPeriod: period, numBuckets: 1)
            window["window_start"] = start.isoformat()
            window["window_end"] = end.isoformat()
            window["seconds_remaining"] = max(0.0, end.seconds(since: now))
            window["source"] = winSource
            window["remote_pct"] = remotePct
            window["previous"] = [
                "window_start": prevStart.isoformat(), "window_end": start.isoformat(),
                "cost": prev["cost"]!, "input_tokens": prev["input_tokens"]!,
                "output_tokens": prev["output_tokens"]!, "message_count": prev["message_count"]!,
            ]
            window["credit_estimate"] = try estimateCreditLimit(source, type)
            result[type] = window
        }
        return result
    }

    /// Pairs a polled usage % with the locally computed cost of the same
    /// window. A 0% reading with nonzero cost is a reset-instant artifact and
    /// is discarded rather than stored.
    func createLimitSnapshot(_ source: String, _ windowType: String, _ percentage: Any?) throws -> [String: Any] {
        guard windowType == "five_hour" || windowType == "weekly" else {
            throw ValidationError(description: "window_type must be 'five_hour' or 'weekly'")
        }
        guard self.source(named: source) != nil else { throw ValidationError(description: "unknown source: \(source)") }
        let pct = try validatePercentage(percentage)

        try scanAll()
        return try db.transaction {
            guard let window = try buildLimitWindows(source)[windowType] as? [String: Any] else {
                return ["saved": false, "reason": "no active window to pair with"]
            }
            let dollarValue = window["cost"] as! Double
            if pct == 0 && dollarValue > 0 {
                return ["saved": false, "reason": "0% with nonzero local cost — discarded as invalid"]
            }
            let createdAt = PyDateTime.now().isoformat()
            try db.run("""
                INSERT INTO limit_snapshots
                (source, window_type, percentage, dollar_value, window_start, window_end, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, [source, windowType, pct, dollarValue, window["window_start"], window["window_end"], createdAt])
            return [
                "saved": true, "source": source, "window_type": windowType, "percentage": pct,
                "dollar_value": dollarValue, "window_start": window["window_start"]!,
                "window_end": window["window_end"]!, "created_at": createdAt,
            ]
        }
    }

    func estimateCreditLimit(_ source: String, _ windowType: String) throws -> [String: Any] {
        let rows = try db.query("""
            SELECT percentage, dollar_value, created_at FROM limit_snapshots
            WHERE source = ? AND window_type = ? ORDER BY created_at ASC
            """, [source, windowType])

        // Consecutive equal readings are idle time, not new evidence.
        var deduped: [Row] = []
        var lastPct: Double?
        for r in rows {
            let pct = r.double("percentage")
            if lastPct != nil && pct == lastPct { continue }
            deduped.append(r)
            lastPct = pct
        }
        let unavailable: [String: Any] = [
            "available": false, "points": deduped.count, "points_needed": Self.creditEstimateMinPoints,
        ]
        guard deduped.count >= Self.creditEstimateMinPoints else { return unavailable }

        let implied = deduped.compactMap { r -> Double? in
            guard let pct = r.double("percentage"), pct > 0 else { return nil }
            return (r.double("dollar_value") ?? 0) / (pct / 100.0)
        }.sorted()
        guard !implied.isEmpty else { return unavailable }

        let mid = implied.count / 2
        let estimated = implied.count % 2 == 1 ? implied[mid] : (implied[mid - 1] + implied[mid]) / 2.0
        let latest = rows[rows.count - 1]
        let current = latest.double("dollar_value") ?? 0
        return [
            "available": true, "points": deduped.count, "estimated_total_dollars": estimated,
            "current_dollars": current, "current_pct": latest["percentage"],
            "remaining_dollars": max(0.0, estimated - current),
        ]
    }

    func listLimitSnapshots(_ source: String?, limit: Int = 200) throws -> [[String: Any]] {
        let columns = "id, source, window_type, percentage, dollar_value, window_start, window_end, created_at"
        let rows = source != nil
            ? try db.query("SELECT \(columns) FROM limit_snapshots WHERE source = ? ORDER BY created_at DESC LIMIT ?", [source, limit])
            : try db.query("SELECT \(columns) FROM limit_snapshots ORDER BY created_at DESC LIMIT ?", [limit])
        return rows.map(\.dict)
    }

    // MARK: Weekly windows and per-day readings

    /// Round to the nearest minute: the remote reset time for one real window
    /// jitters by about a second across a minute boundary between polls.
    static func floorWindowKey(_ iso: String?) -> Any {
        guard let dt = PyDateTime.parse(iso) else { return orNull(iso) }
        let bumped = dt.wall.second >= 30 ? dt.adding(micros: Micros.minute) : dt
        return bumped.flooredWall(to: Micros.minute).isoformat()
    }

    /// File a reading under the latest tracked window if that window started
    /// within half a period of this one. Codex recomputes its reset from "now"
    /// on every poll, so without this each poll would open a new window.
    private func resolveWeeklyWindowKey(_ source: String, _ start: Any, _ end: Any) throws -> Any {
        guard let latest = try db.queryOne("""
            SELECT window_start, window_end FROM weekly_windows
            WHERE source = ? ORDER BY window_start DESC LIMIT 1
            """, [source]) else { return start }
        guard let latestStart = PyDateTime.parse(latest.string("window_start")),
              let newStart = PyDateTime.parse(start as? String),
              let newEnd = PyDateTime.parse(end as? String) else { return start }
        let period = newEnd.micros - newStart.micros
        return abs(newStart.micros - latestStart.micros) * 2 <= period ? latest["window_start"] : start
    }

    /// Keeps the LATEST reading per day, not a running max: a rolling weekly
    /// quota can go down within a window.
    func upsertWeeklyDailyMax(_ source: String, _ pct: Double, _ windowStart: Any?, _ windowEnd: Any?, _ observedAt: String) throws {
        let start = Self.floorWindowKey(windowStart as? String)
        let end = Self.floorWindowKey(windowEnd as? String)
        let day = dayFields(observedAt).day
        let effectiveStart = try resolveWeeklyWindowKey(source, start, end)
        try db.run("""
            INSERT INTO weekly_windows (source, window_start, window_end, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source, window_start) DO UPDATE SET
                window_end = excluded.window_end
            WHERE excluded.window_end > weekly_windows.window_end
            """, [source, effectiveStart, end, observedAt])
        try db.run("""
            INSERT INTO weekly_daily_max_pct (source, window_start, day, latest_percentage, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, window_start, day) DO UPDATE SET
                latest_percentage = excluded.latest_percentage,
                updated_at = excluded.updated_at
            WHERE excluded.updated_at >= weekly_daily_max_pct.updated_at
            """, [source, effectiveStart, day, pct, observedAt])
    }

    func recordWeeklyDailyMax(_ source: String, _ percentage: Any?, _ start: Any?, _ end: Any?, observedAt: String? = nil) throws {
        guard self.source(named: source) != nil else { throw ValidationError(description: "unknown source: \(source)") }
        let pct = try validatePercentage(percentage)
        try db.transaction {
            try upsertWeeklyDailyMax(source, pct, start, end, observedAt ?? PyDateTime.now().isoformat())
        }
    }

    /// Replays limit_snapshots history into the weekly tables. Idempotent,
    /// so it runs on every start.
    func backfillWeeklyUsageFromSnapshots(_ source: String) throws {
        try db.transaction {
            for row in try db.query("""
                SELECT percentage, window_start, window_end, created_at
                FROM limit_snapshots
                WHERE source = ? AND window_type = 'weekly'
                ORDER BY created_at ASC
                """, [source]) {
                let pct = try validatePercentage(row["percentage"])
                try upsertWeeklyDailyMax(source, pct, row["window_start"], row["window_end"], row.string("created_at") ?? "")
            }
        }
    }

    func weeklyPeak(_ source: String, _ start: Any, _ end: Any) throws -> Any {
        try db.queryOne("""
            SELECT MAX(percentage) AS peak FROM limit_snapshots
            WHERE source = ? AND window_type = 'weekly'
              AND created_at >= ? AND created_at < ?
            """, [source, start, end])?["peak"] ?? NSNull()
    }

    func dailyReadings(_ source: String, _ windowStart: Any) throws -> [[String: Any]] {
        try db.query("""
            SELECT day, latest_percentage, updated_at
            FROM weekly_daily_max_pct
            WHERE source = ? AND window_start = ?
            ORDER BY day ASC
            """, [source, windowStart]).map(\.dict)
    }

    func listWeeklyUsage(_ source: String?, limit: Int = 20) throws -> [[String: Any]] {
        if let source, self.source(named: source) == nil { throw ValidationError(description: "unknown source: \(source)") }
        let rows = source != nil
            ? try db.query("SELECT source, window_start, window_end, created_at FROM weekly_windows WHERE source = ? ORDER BY window_start DESC", [source])
            : try db.query("SELECT source, window_start, window_end, created_at FROM weekly_windows ORDER BY window_start DESC")

        // Collapse per source before limiting, or one fragmenting source's
        // near-duplicate rows fill the whole list.
        let now = PyDateTime.now()
        var order: [String] = []
        var bySource: [String: [Row]] = [:]
        for r in rows {
            let s = r.string("source") ?? ""
            if bySource[s] == nil { order.append(s) }
            bySource[s, default: []].append(r)
        }
        var windows: [Row] = []
        for s in order { windows += collapseOverlappingWindows(bySource[s]!, now: now).prefix(max(0, limit)) }
        windows = windows.enumerated().sorted { a, b in
            let sa = a.element.string("window_start") ?? "", sb = b.element.string("window_start") ?? ""
            return sa != sb ? sa > sb : a.offset < b.offset
        }.map(\.element)
        windows = Array(windows.prefix(max(0, limit)))

        return try windows.map { w in
            let daily = try dailyReadings(w.string("source") ?? "", w["window_start"])
            // Peak from raw poll history: a collapsed window's readings are
            // filed under whichever fragment key was current at the time.
            var peak = try weeklyPeak(w.string("source") ?? "", w["window_start"], w["window_end"])
            if peak is NSNull, let last = daily.last { peak = last["latest_percentage"]! }
            return [
                "source": w["source"], "window_start": w["window_start"], "window_end": w["window_end"],
                "created_at": w["created_at"], "daily_max": daily, "week_max_percentage": peak,
            ]
        }
    }
}

// MARK: Overlap collapsing

/// Reduce windows to genuinely separate ones, newest first, skipping any
/// that overlap what's already kept. The anchor (if any) is always kept first.
func collapseOverlapping<T>(_ items: [T], start: (T) -> PyDateTime, end: (T) -> PyDateTime, anchor: Int?) -> [T] {
    let ordered = items.indices.sorted { a, b in
        let sa = start(items[a]), sb = start(items[b])
        return sa != sb ? sa > sb : a < b
    }
    var kept: [T] = []
    var boundary: PyDateTime?
    if let anchor {
        kept.append(items[anchor])
        boundary = start(items[anchor])
    }
    for i in ordered where i != anchor {
        if let boundary, end(items[i]) > boundary { continue }
        kept.append(items[i])
        boundary = start(items[i])
    }
    return kept
}

/// Anchored on the window still in progress so the current week is never
/// dropped in favour of an overlapping fragment.
func collapseOverlappingWindows(_ rows: [Row], now: PyDateTime) -> [Row] {
    let usable = rows.filter { PyDateTime.parse($0.string("window_start")) != nil && PyDateTime.parse($0.string("window_end")) != nil }
    let startOf = { (r: Row) in PyDateTime.parse(r.string("window_start"))! }
    let endOf = { (r: Row) in PyDateTime.parse(r.string("window_end"))! }
    let newestFirst = usable.indices.sorted { a, b in
        let sa = startOf(usable[a]), sb = startOf(usable[b])
        return sa != sb ? sa > sb : a < b
    }
    let anchor = newestFirst.first { endOf(usable[$0]) > now }
    return collapseOverlapping(usable, start: startOf, end: endOf, anchor: anchor)
}
