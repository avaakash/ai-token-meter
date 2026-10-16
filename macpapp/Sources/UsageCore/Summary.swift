import Foundation

public struct WindowSummary {
    public var percent: Double?
    public var cost: Double
    public var resetsAt: Date?
    public var isRemote: Bool
}

/// A spending budget the person set, for the current day, week or month.
public struct BudgetSummary {
    public var period: String
    public var limit: Double
    public var cost: Double
    public var percent: Double
    public var resetsAt: Date?
}

public struct SourceSummary {
    public var source: String
    public var fiveHour: WindowSummary?
    public var weekly: WindowSummary?
    public var lastError: String?
    public var budgets: [BudgetSummary]
}

extension UsageStore {
    /// What the menu bar shows: each source's current windows, read from
    /// the same data the dashboard's rate-limit cards use.
    public func limitSummaries() throws -> [SourceSummary] {
        try sync {
            try sources.map { source in
                let limits = try buildLimitWindows(source.name)
                func window(_ key: String) -> WindowSummary? {
                    guard let w = limits[key] as? [String: Any] else { return nil }
                    return WindowSummary(
                        percent: pythonFloat(w["remote_pct"]),
                        cost: w["cost"] as? Double ?? 0,
                        resetsAt: PyDateTime.parse(w["window_end"] as? String)?.date,
                        isRemote: w["source"] as? String == "remote")
                }
                let status = limits["remote_status"] as? [String: Any]
                let budgets = try budgetStatus(source.name).map { b in
                    BudgetSummary(period: b["period"] as? String ?? "", limit: b["limit"] as? Double ?? 0,
                                  cost: b["cost"] as? Double ?? 0, percent: b["pct"] as? Double ?? 0,
                                  resetsAt: PyDateTime.parse(b["end"] as? String)?.date)
                }
                return SourceSummary(source: source.name, fiveHour: window("five_hour"), weekly: window("weekly"),
                                     lastError: status?["last_error"] as? String, budgets: budgets)
            }
        }
    }
}
