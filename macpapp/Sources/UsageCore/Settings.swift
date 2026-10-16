import Foundation

/// What the person set in Settings: their own per-model prices and spending
/// budgets. Kept in the database's meta table, so it survives app updates.
extension UsageStore {
    static let settingsKey = "user_settings"
    static let budgetPeriods = ["day", "week", "month"]
    static let priceFields = ["input", "output", "cached_input"]
    static let maxPrice = 100_000.0
    static let maxBudget = 10_000_000.0

    static func readSettings(_ db: SQLiteDB) throws -> [String: Any] {
        guard let value = try db.queryOne("SELECT value FROM meta WHERE key = ?", [settingsKey])?.string("value"),
              let settings = try? JSONSerialization.jsonObject(with: Data(value.utf8)) as? [String: Any] else { return [:] }
        return settings
    }

    static func priceOverrides(_ settings: [String: Any], _ source: String) -> [String: [String: Double]] {
        let saved = (settings["prices"] as? [String: Any])?[source] as? [String: Any] ?? [:]
        return saved.compactMapValues { fields in
            let rate = (fields as? [String: Any] ?? [:]).compactMapValues { ($0 as? NSNumber)?.doubleValue }
                .filter { priceFields.contains($0.key) && $0.value.isFinite && $0.value >= 0 }
            return rate.isEmpty ? nil : rate
        }
    }

    static func loadPricing(_ paths: AppPaths, _ source: String, _ settings: [String: Any]) throws -> Pricing {
        try Pricing(path: paths.pricingDirectory + "/pricing_\(source).json", overrides: priceOverrides(settings, source))
    }

    private func knownSource(_ name: String) throws -> UsageSource {
        guard let source = source(named: name) else { throw ValidationError(description: "unknown source: \(name)") }
        return source
    }

    /// Saves the settings, reloads both price tables and reprices every
    /// stored message if a price changed.
    private func apply(_ settings: [String: Any]) throws {
        let data = try JSONSerialization.data(withJSONObject: settings, options: [.sortedKeys])
        let previous = sources.map(\.pricing)
        let updated = try sources.map { try Self.loadPricing(paths, $0.name, settings) }
        zip(sources, updated).forEach { $0.pricing = $1 }
        do {
            try db.transaction {
                try db.run("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [Self.settingsKey, String(decoding: data, as: UTF8.self)])
                try ensurePricingCurrent()
            }
        } catch {
            zip(sources, previous).forEach { $0.pricing = $1 }
            throw error
        }
    }

    /// The rate as the cost code applies it, with the cache-read price
    /// filled in where the table leaves it to a default.
    private func effectiveRate(_ rate: Rate, _ source: String) -> [String: Double] {
        ["input": rate.input, "output": rate.output,
         "cached_input": rate.cachedInput ?? (source == "claude" ? rate.input * 0.1 : rate.input)]
    }

    // MARK: Prices

    /// One source's price list for the Settings page: models the person has
    /// used (most expensive first), then their own additions, then the rest
    /// of the table.
    public func priceList(_ sourceName: String) throws -> [String: Any] {
        let pricing = try knownSource(sourceName).pricing
        let now = PyDateTime.now().isoformat()
        let used = try db.query("""
            SELECT model, COUNT(*) AS message_count, COALESCE(SUM(cost), 0) AS cost FROM messages
            WHERE source = ? AND model IS NOT NULL AND model NOT IN ('<synthetic>', 'unknown')
            GROUP BY model ORDER BY cost DESC
            """, [sourceName])
        let usage = Dictionary(used.compactMap { r in r.string("model").map { ($0, r) } }, uniquingKeysWith: { a, _ in a })
        var names = used.compactMap { $0.string("model") }
        names += pricing.overrides.keys.sorted().filter { !names.contains($0) }
        names += pricing.modelNames.filter { !names.contains($0) }

        let models: [[String: Any]] = names.map { model in
            [
                "model": model,
                "listed": pricing.isListed(model),
                "edited": pricing.overrides[model] != nil,
                "price": effectiveRate(pricing.resolveRate(model: model, timestamp: now), sourceName),
                "built_in": effectiveRate(pricing.resolveRate(model: model, timestamp: now, useOverrides: false), sourceName),
                "message_count": usage[model]?["message_count"] ?? 0,
                "cost": usage[model]?["cost"] ?? 0.0,
            ]
        }
        return [
            "source": sourceName, "models": models, "edited_count": pricing.overrides.count,
            "verified": orNull(pricing.lastVerified), "reference": orNull(pricing.sourceNote),
        ]
    }

    /// Sets a model's price (dollars per million tokens), keeping only the
    /// fields that differ from the built-in table; `nil` restores the table's.
    public func setPrice(_ sourceName: String, model rawModel: String, fields: [String: Double]?) throws {
        let source = try knownSource(sourceName)
        let model = rawModel.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !model.isEmpty, model.count <= 200 else { throw ValidationError(description: "a model name is required") }
        var settings = try Self.readSettings(db)
        var prices = settings["prices"] as? [String: Any] ?? [:]
        var forSource = prices[sourceName] as? [String: Any] ?? [:]
        if let fields {
            for (key, value) in fields {
                guard Self.priceFields.contains(key) else { throw ValidationError(description: "unknown price field: \(key)") }
                guard value.isFinite, value >= 0, value <= Self.maxPrice else {
                    throw ValidationError(description: "prices must be between $0 and $\(Int(Self.maxPrice)) per million tokens")
                }
            }
            let builtIn = effectiveRate(source.pricing.resolveRate(model: model, timestamp: PyDateTime.now().isoformat(),
                                                                    useOverrides: false), sourceName)
            // A model the table doesn't list keeps every field it was given,
            // so adding one at the fallback price still records it.
            let kept = source.pricing.isListed(model)
                ? fields.filter { abs($0.value - (builtIn[$0.key] ?? -1)) > 1e-9 } : fields
            forSource[model] = kept.isEmpty ? nil : kept
        } else {
            forSource[model] = nil
        }
        prices[sourceName] = forSource.isEmpty ? nil : forSource
        settings["prices"] = prices.isEmpty ? nil : prices
        try apply(settings)
    }

    /// Back to the built-in prices for one source, or for both.
    public func resetPrices(_ sourceName: String?) throws {
        if let sourceName { _ = try knownSource(sourceName) }
        var settings = try Self.readSettings(db)
        var prices = settings["prices"] as? [String: Any] ?? [:]
        if let sourceName { prices[sourceName] = nil } else { prices = [:] }
        settings["prices"] = prices.isEmpty ? nil : prices
        try apply(settings)
    }

    // MARK: Budgets

    /// Spending limits for people billed per token, in dollars per calendar
    /// day, week (from Monday) and month; a missing period has no budget.
    public func setBudgets(_ sourceName: String, _ amounts: [String: Double]) throws {
        _ = try knownSource(sourceName)
        for (period, amount) in amounts {
            guard Self.budgetPeriods.contains(period) else { throw ValidationError(description: "unknown budget period: \(period)") }
            guard amount.isFinite, amount > 0, amount <= Self.maxBudget else {
                throw ValidationError(description: "a budget must be more than $0 and at most $\(Int(Self.maxBudget))")
            }
        }
        var settings = try Self.readSettings(db)
        var budgets = settings["budgets"] as? [String: Any] ?? [:]
        budgets[sourceName] = amounts.isEmpty ? nil : amounts
        settings["budgets"] = budgets.isEmpty ? nil : budgets
        try apply(settings)
    }

    func budgetAmounts(_ sourceName: String) throws -> [String: Double] {
        let saved = (try Self.readSettings(db)["budgets"] as? [String: Any])?[sourceName] as? [String: Any] ?? [:]
        return saved.compactMapValues { ($0 as? NSNumber)?.doubleValue }.filter { Self.budgetPeriods.contains($0.key) && $0.value > 0 }
    }

    /// The calendar day, week (from Monday) or month that `now` falls in,
    /// and what one source spent in it.
    private func periodSpend(_ sourceName: String, _ period: String, now: Date) throws -> (interval: DateInterval, cost: Double)? {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .current
        calendar.firstWeekday = 2
        calendar.minimumDaysInFirstWeek = 4
        let unit: Calendar.Component = period == "day" ? .day : period == "week" ? .weekOfYear : .month
        guard let interval = calendar.dateInterval(of: unit, for: now) else { return nil }
        let cost = try db.queryOne("""
            SELECT COALESCE(SUM(cost), 0) AS cost FROM messages WHERE source = ? AND timestamp >= ? AND timestamp < ?
            """, [sourceName, utcStamp(interval.start), utcStamp(interval.end)])?.double("cost") ?? 0
        return (interval, cost)
    }

    /// Each budget's current period and what's been spent in it so far.
    func budgetStatus(_ sourceName: String, now: Date = Date()) throws -> [[String: Any]] {
        let amounts = try budgetAmounts(sourceName)
        return try Self.budgetPeriods.compactMap { period in
            guard let limit = amounts[period], let spend = try periodSpend(sourceName, period, now: now) else { return nil }
            return [
                "period": period, "limit": limit, "cost": spend.cost, "pct": spend.cost / limit * 100,
                "start": PyDateTime.fromTimestamp(spend.interval.start.timeIntervalSince1970).isoformat(),
                "end": PyDateTime.fromTimestamp(spend.interval.end.timeIntervalSince1970).isoformat(),
            ]
        }
    }

    public func settingsPayload(_ sourceName: String) throws -> [String: Any] {
        var budgets: [String: Any] = [:]
        for source in sources {
            var spent: [String: Double] = [:]
            for period in Self.budgetPeriods { spent[period] = try periodSpend(source.name, period, now: Date())?.cost }
            budgets[source.name] = ["amounts": try budgetAmounts(source.name), "spent": spent]
        }
        return ["prices": try priceList(sourceName), "budgets": budgets]
    }
}

/// The stored timestamps' own format (UTC, millisecond "Z").
private func utcStamp(_ date: Date) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = TimeZone(identifier: "UTC")
    formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'"
    return formatter.string(from: date)
}
