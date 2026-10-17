import Foundation

/// What the menu bar item shows: up to three limit windows, as used or
/// left, optionally with the time until each resets.
public struct MenuBarConfig: Equatable {
    public struct Item: Equatable {
        public var source: String
        public var window: String  // "five_hour" or "weekly"
        var key: String { "\(source):\(window)" }

        public init(source: String, window: String) {
            self.source = source
            self.window = window
        }
    }

    public var items: [Item]
    public var showLeft: Bool
    public var showReset: Bool
    /// False until the person changes anything, so the default can still
    /// fall back to Codex when Claude reports no limits.
    public var customized: Bool

    static let defaultItems = [Item(source: "claude", window: "five_hour"), Item(source: "claude", window: "weekly")]
    static let allKeys = ["claude:five_hour", "claude:weekly", "codex:five_hour", "codex:weekly"]
    public static let maxItems = 3

    public static let `default` = MenuBarConfig(items: defaultItems, showLeft: false, showReset: false, customized: false)

    var dict: [String: Any] {
        ["items": items.map(\.key), "value": showLeft ? "left" : "used", "reset": showReset]
    }

    init(items: [Item], showLeft: Bool, showReset: Bool, customized: Bool) {
        self.items = items
        self.showLeft = showLeft
        self.showReset = showReset
        self.customized = customized
    }

    init(saved: [String: Any]?) {
        guard let saved else { self = .default; return }
        let keys = (saved["items"] as? [String] ?? []).filter { Self.allKeys.contains($0) }
        items = Self.allKeys.filter { keys.contains($0) }.prefix(Self.maxItems).map { key in
            let parts = key.split(separator: ":")
            return Item(source: String(parts[0]), window: String(parts[1]))
        }
        showLeft = saved["value"] as? String == "left"
        showReset = saved["reset"] as? Bool ?? false
        customized = true
    }
}

/// One item as the menu bar draws it.
public struct MenuBarEntry: Equatable {
    public var source: String
    public var label: String  // "5h", "W", "Day", "Wk", "Mo"
    public var name: String   // "5-hour", "weekly", "daily budget"…
    public var used: Double
    public var shown: Int     // used, or what's left, as chosen
    public var resetsAt: Date?
}

extension MenuBarConfig {
    static let budgetLabels = ["day": ("Day", "daily budget"), "week": ("Wk", "weekly budget"), "month": ("Mo", "monthly budget")]

    /// The chosen items that have a reported percentage. Billed per token,
    /// with no plan percentages at all, the budgets stand in.
    public func entries(_ summaries: [SourceSummary]) -> [MenuBarEntry] {
        var items = self.items
        let claude = summaries.first { $0.source == "claude" }
        if !customized, claude?.fiveHour?.percent == nil, claude?.weekly?.percent == nil,
           summaries.contains(where: { $0.source == "codex" && $0.weekly?.percent != nil }) {
            items = items.map { Item(source: "codex", window: $0.window) }
        }
        func entry(_ source: String, _ label: String, _ name: String, _ used: Double, _ resets: Date?) -> MenuBarEntry {
            MenuBarEntry(source: source, label: label, name: name, used: used,
                         shown: Int((showLeft ? max(0, 100 - used) : used).rounded()), resetsAt: resets)
        }
        var out: [MenuBarEntry] = items.compactMap { item in
            guard let s = summaries.first(where: { $0.source == item.source }) else { return nil }
            let fiveHour = item.window == "five_hour"
            guard let window = fiveHour ? s.fiveHour : s.weekly, let pct = window.percent else { return nil }
            return entry(item.source, fiveHour ? "5h" : "W", fiveHour ? "5-hour" : "weekly", pct, window.resetsAt)
        }
        if out.isEmpty, let budgeted = summaries.filter({ !$0.budgets.isEmpty })
            .max(by: { ($0.budgets.map(\.percent).max() ?? 0) < ($1.budgets.map(\.percent).max() ?? 0) }) {
            out = budgeted.budgets.prefix(Self.maxItems).map { b in
                let (label, name) = Self.budgetLabels[b.period] ?? (b.period, b.period)
                return entry(budgeted.source, label, name, b.percent, b.resetsAt)
            }
        }
        return out
    }
}

extension UsageStore {
    func menuBarConfigUnsynced() throws -> MenuBarConfig {
        MenuBarConfig(saved: try Self.readSettings(db)["menubar"] as? [String: Any])
    }

    public func menuBarConfig() throws -> MenuBarConfig {
        try sync { try menuBarConfigUnsynced() }
    }

    /// `items` are "source:window" keys, shown in a fixed order (Claude
    /// first, 5-hour before weekly) whatever order they arrive in.
    public func setMenuBar(items: [String], value: String, reset: Bool) throws {
        guard !items.isEmpty, items.count <= MenuBarConfig.maxItems else {
            throw ValidationError(description: "choose between 1 and \(MenuBarConfig.maxItems) items for the menu bar")
        }
        if let unknown = items.first(where: { !MenuBarConfig.allKeys.contains($0) }) {
            throw ValidationError(description: "unknown menu bar item: \(unknown)")
        }
        guard Set(items).count == items.count else { throw ValidationError(description: "each item can appear once") }
        guard value == "used" || value == "left" else { throw ValidationError(description: "value must be used or left") }
        var settings = try Self.readSettings(db)
        settings["menubar"] = ["items": items, "value": value, "reset": reset]
        try apply(settings)
    }

    public func resetMenuBar() throws {
        var settings = try Self.readSettings(db)
        settings["menubar"] = nil
        try apply(settings)
    }

    func menuBarPayload() throws -> [String: Any] {
        let config = try menuBarConfigUnsynced()
        var d = config.dict
        d["customized"] = config.customized
        d["max"] = MenuBarConfig.maxItems
        return d
    }
}
