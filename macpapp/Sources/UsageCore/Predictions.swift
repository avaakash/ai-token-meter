import Foundation

private let minCompletedWeeksForHistory = 3
private let minCurrentWeekDaysForPace = 2
private let minElapsedHoursForPace = 18.0

private let exhaustionMinReferenceWeeks = 3
private let exhaustionMinElapsedHours = 12.0  // before this the shape denominator is tiny and the projection is noise
private let exhaustionMinShapeFraction = 0.05
private let exhaustionMinWeekFinalPct = 5.0   // a barely-used week teaches nothing about pacing
private let exhaustionMinWeekCoverageHours = 100.0
private let shapeHours = 168

private struct WeekSeries {
    var start: PyDateTime
    var end: PyDateTime
    var points: [(PyDateTime, Double)]
    var final: Double { points[points.count - 1].1 }
    var coverageHours: Double { points[points.count - 1].0.seconds(since: start) / 3600 }
}

private func median(_ values: [Double]) -> Double {
    let s = values.sorted()
    let n = s.count
    return n % 2 == 1 ? s[n / 2] : (s[n / 2 - 1] + s[n / 2]) / 2
}

extension UsageStore {
    /// How much of the weekly limit a source is on track to use, built only
    /// from the provider's own polled percentages — never from local cost,
    /// which misses usage from surfaces that don't write local logs.
    func predictWeeklyUsage(_ source: String) throws -> [String: Any] {
        guard self.source(named: source) != nil else { throw ValidationError(description: "unknown source: \(source)") }
        let windows = try db.query(
            "SELECT window_start, window_end FROM weekly_windows WHERE source = ? ORDER BY window_start DESC", [source])
        if windows.isEmpty {
            return [
                "available": false, "historical_avg_percentage": NSNull(), "historical_weeks_used": 0,
                "historical_weeks_needed": minCompletedWeeksForHistory, "pace_projection_percentage": NSNull(),
                "current_percentage": NSNull(), "current_window_start": NSNull(), "current_window_end": NSNull(),
            ]
        }
        let now = PyDateTime.now()

        var current: Row?
        var completed: [Row] = []
        for w in collapseOverlappingWindows(windows, now: now) {
            if current == nil, let end = PyDateTime.parse(w.string("window_end")), end > now {
                current = w
            } else {
                completed.append(w)
            }
        }

        // Peak (not latest): the reading right at a reset rolls back to 0.
        var historicalFinals: [Double] = []
        for w in completed {
            if let peak = pythonFloat(try weeklyPeak(source, w["window_start"], w["window_end"])) {
                historicalFinals.append(peak)
            }
        }
        let historicalAvg: Double? = historicalFinals.count >= minCompletedWeeksForHistory
            ? historicalFinals.reduce(0, +) / Double(historicalFinals.count) : nil

        var paceProjection: Double?
        var currentPercentage: Any = NSNull()
        if let current {
            let days = try db.query("""
                SELECT day, latest_percentage FROM weekly_daily_max_pct
                WHERE source = ? AND window_start = ? ORDER BY day ASC
                """, [source, current["window_start"]])
            if let last = days.last {
                currentPercentage = last["latest_percentage"]
                if let start = PyDateTime.parse(current.string("window_start")),
                   let end = PyDateTime.parse(current.string("window_end")),
                   let pct = last.double("latest_percentage") {
                    let elapsed = now.seconds(since: start)
                    let total = end.seconds(since: start)
                    if days.count >= minCurrentWeekDaysForPace && elapsed / 3600 >= minElapsedHoursForPace && elapsed > 0 {
                        paceProjection = min(100.0, pct / elapsed * total)
                    }
                }
            }
        }

        return [
            "available": historicalAvg != nil || paceProjection != nil,
            "historical_avg_percentage": orNull(historicalAvg),
            "historical_weeks_used": historicalFinals.count,
            "historical_weeks_needed": minCompletedWeeksForHistory,
            "pace_projection_percentage": orNull(paceProjection),
            "current_percentage": currentPercentage,
            "current_window_start": orNull(current?["window_start"]),
            "current_window_end": orNull(current?["window_end"]),
            "exhaustion": try predictWeeklyExhaustion(source),
        ]
    }

    /// Each tracked weekly window's (time, %) series, dropping readings below
    /// the running max (reset-instant 0% blips, stale low readings).
    private func weeklySnapshotSeries(_ source: String) throws -> [WeekSeries] {
        var series: [WeekSeries] = []
        for w in try db.query(
            "SELECT window_start, window_end FROM weekly_windows WHERE source = ? ORDER BY window_start ASC", [source]) {
            guard let start = PyDateTime.parse(w.string("window_start")),
                  let end = PyDateTime.parse(w.string("window_end")) else { continue }
            let rows = try db.query("""
                SELECT created_at, percentage FROM limit_snapshots
                WHERE source = ? AND window_type = 'weekly'
                  AND created_at >= ? AND created_at < ?
                ORDER BY created_at ASC
                """, [source, w["window_start"], w["window_end"]])
            var points: [(PyDateTime, Double)] = []
            var runningMax = 0.0
            for r in rows {
                guard let ts = PyDateTime.parse(r.string("created_at")), let pct = r.double("percentage"),
                      pct >= runningMax else { continue }
                runningMax = pct
                points.append((ts, pct))
            }
            if !points.isEmpty { series.append(WeekSeries(start: start, end: end, points: points)) }
        }
        return series
    }

    /// When the weekly quota would hit 100% at the current pace. Projects
    /// through the median cumulative "shape" of the user's past weeks, which
    /// backtested far better than flat wall-clock scaling.
    func predictWeeklyExhaustion(_ source: String) throws -> [String: Any] {
        func unavailable(_ reason: String, _ extra: [String: Any] = [:]) -> [String: Any] {
            var r: [String: Any] = [
                "available": false, "reason": reason, "method": NSNull(),
                "current_percentage": NSNull(), "elapsed_hours": NSNull(),
                "projected_final_percentage": NSNull(), "projected_final_low": NSNull(), "projected_final_high": NSNull(),
                "will_exhaust": NSNull(), "exhaustion_at": NSNull(),
                "exhaustion_at_earliest": NSNull(), "exhaustion_at_latest": NSNull(),
                "seconds_until_exhaustion": NSNull(), "burn_rate_pct_per_day": NSNull(),
                "reference_weeks": 0, "typical_error_pp": NSNull(), "window_end": NSNull(),
            ]
            r.merge(extra) { _, new in new }
            return r
        }

        let now = PyDateTime.now()
        let all = try weeklySnapshotSeries(source)
        guard let currentIndex = all.indices.reversed().first(where: { all[$0].start <= now && now < all[$0].end }) else {
            return unavailable("no weekly window in progress")
        }
        let current = all[currentIndex]

        // Keep track of which collapsed entry is `current` by position.
        let tagged = all.enumerated().map { ($0.offset, $0.element) }
        let distinct = collapseOverlapping(tagged, start: { $0.1.start }, end: { $0.1.end }, anchor: currentIndex)

        let elapsedHours = now.seconds(since: current.start) / 3600
        let currentPct = current.final
        let windowEnd = current.end.isoformat()
        let burnRate: Any = elapsedHours > 0 ? currentPct / elapsedHours * 24 : NSNull()

        if elapsedHours < exhaustionMinElapsedHours {
            return unavailable(String(format: "only %.0fh into the week — too early to project", elapsedHours), [
                "current_percentage": currentPct, "elapsed_hours": elapsedHours,
                "burn_rate_pct_per_day": burnRate, "window_end": windowEnd,
            ])
        }

        let reference = distinct.filter {
            $0.0 != currentIndex && $0.1.final >= exhaustionMinWeekFinalPct
                && $0.1.coverageHours >= exhaustionMinWeekCoverageHours
        }.map(\.1)

        let hourIndex = min(shapeHours, Int(elapsedHours))
        var method = "shape"
        var shape: [Double]? = reference.count >= exhaustionMinReferenceWeeks ? cumulativeShape(reference) : nil
        if shape == nil || shape![hourIndex] < exhaustionMinShapeFraction {
            // Too few comparable weeks: flat wall-clock scaling, which the UI
            // hedges via `method`.
            method = "flat"
            shape = (0...shapeHours).map { Double($0) / Double(shapeHours) }
        }
        let curve = shape!

        guard curve[hourIndex] > 0 else {
            return unavailable("no usable pace signal yet", [
                "current_percentage": currentPct, "elapsed_hours": elapsedHours,
                "burn_rate_pct_per_day": burnRate, "window_end": windowEnd,
            ])
        }
        let projectedFinal = currentPct / curve[hourIndex]

        // The spread of actual/predicted at this same point in past weeks is
        // the error bar, measured on the user's own history.
        var ratios: [Double] = [], errors: [Double] = []
        for entry in reference where entry.coverageHours >= Double(hourIndex) {
            guard let pctThen = pctAtHour(entry, Double(hourIndex)) else { continue }
            let predicted = pctThen / curve[hourIndex]
            guard predicted > 0 else { continue }
            ratios.append(entry.final / predicted)
            errors.append(abs(min(100.0, predicted) - entry.final))
        }
        var lowRatio: Double?, highRatio: Double?
        if !ratios.isEmpty {
            let ordered = ratios.sorted()
            lowRatio = ordered[Int(Double(ordered.count) * 0.2)]
            highRatio = ordered[min(ordered.count - 1, Int(Double(ordered.count) * 0.8))]
        }
        let projectedLow = lowRatio.map { projectedFinal * $0 }
        let projectedHigh = highRatio.map { projectedFinal * $0 }

        func crossing(_ final: Double?) -> String? {
            guard let final, let hour = shapeCrossingHour(curve, final, from: elapsedHours) else { return nil }
            // Hourly sampling can interpolate to just behind now; report now.
            let at = current.start.adding(hours: hour)
            return (at > now ? at : now).isoformat()
        }
        let exhaustionAt = crossing(projectedFinal)
        // A higher projected total burns the quota sooner.
        let earliest = crossing(projectedHigh)
        let latest = crossing(projectedLow)

        var secondsUntil: Any = NSNull()
        if let exhaustionAt, let eta = PyDateTime.parse(exhaustionAt) {
            secondsUntil = max(0.0, eta.seconds(since: now))
        }

        return [
            "available": true, "reason": NSNull(), "method": method,
            "current_percentage": currentPct, "elapsed_hours": elapsedHours,
            "projected_final_percentage": min(100.0, projectedFinal),
            // Uncapped, for drawing a projection that runs past the limit.
            "projected_final_raw": projectedFinal,
            "projected_low_raw": orNull(projectedLow),
            "projected_high_raw": orNull(projectedHigh),
            "projected_final_low": orNull(projectedLow.map { min(100.0, $0) }),
            "projected_final_high": orNull(projectedHigh.map { min(100.0, $0) }),
            "will_exhaust": exhaustionAt != nil,
            "exhaustion_at": orNull(exhaustionAt),
            "exhaustion_at_earliest": orNull(earliest),
            "exhaustion_at_latest": orNull(latest),
            "seconds_until_exhaustion": secondsUntil,
            "burn_rate_pct_per_day": burnRate,
            "reference_weeks": reference.count,
            "typical_error_pp": errors.isEmpty ? NSNull() : errors.reduce(0, +) / Double(errors.count),
            "window_end": windowEnd,
        ]
    }

    private func pctAtHour(_ entry: WeekSeries, _ hour: Double) -> Double? {
        let target = entry.start.adding(hours: hour)
        return entry.points.filter { $0.0 <= target }.map(\.1).max()
    }

    /// Median cumulative share of a week's final usage reached by each
    /// elapsed hour. Monotone by construction.
    private func cumulativeShape(_ reference: [WeekSeries]) -> [Double] {
        var shape: [Double] = []
        var previous = 0.0
        for hour in 0...shapeHours {
            var shares: [Double] = []
            for entry in reference where entry.coverageHours >= Double(hour) {
                if let pct = pctAtHour(entry, Double(hour)) { shares.append(min(1.0, pct / entry.final)) }
            }
            let value = shares.isEmpty ? previous : median(shares)
            previous = max(previous, value)
            shape.append(previous)
        }
        return shape
    }

    /// First elapsed hour at or after `from` where the projected curve reaches
    /// 100%, interpolated between hourly samples.
    private func shapeCrossingHour(_ shape: [Double], _ projectedFinal: Double, from: Double) -> Double? {
        guard projectedFinal > 100 else { return nil }
        let start = max(0, Int(from))
        guard start < shapeHours else { return nil }
        for hour in start..<shapeHours {
            let low = projectedFinal * shape[hour], high = projectedFinal * shape[hour + 1]
            if high >= 100 {
                if high == low { return Double(hour + 1) }
                return Double(hour) + (100 - low) / (high - low)
            }
        }
        return nil
    }
}
