import Foundation

public struct WindowSummary {
    public var percent: Double?
    public var cost: Double
    public var resetsAt: Date?
    public var isRemote: Bool
}

public struct SourceSummary {
    public var source: String
    public var fiveHour: WindowSummary?
    public var weekly: WindowSummary?
    public var lastError: String?
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
                return SourceSummary(source: source.name, fiveHour: window("five_hour"), weekly: window("weekly"),
                                     lastError: status?["last_error"] as? String)
            }
        }
    }
}
