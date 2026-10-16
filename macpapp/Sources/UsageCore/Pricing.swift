import CryptoKit
import Foundation

public struct Rate {
    public var input: Double
    public var output: Double
    public var cachedInput: Double?
    public var cacheWrite: Double?

    init(_ dict: [String: Any]) {
        input = (dict["input"] as? NSNumber)?.doubleValue ?? 0
        output = (dict["output"] as? NSNumber)?.doubleValue ?? 0
        cachedInput = (dict["cached_input"] as? NSNumber)?.doubleValue
        cacheWrite = (dict["cache_write"] as? NSNumber)?.doubleValue
    }

    mutating func overlay(_ dict: [String: Any]) {
        if let v = dict["input"] as? NSNumber { input = v.doubleValue }
        if let v = dict["output"] as? NSNumber { output = v.doubleValue }
        if let v = dict["cached_input"] as? NSNumber { cachedInput = v.doubleValue }
        if let v = dict["cache_write"] as? NSNumber { cacheWrite = v.doubleValue }
    }
}

/// A `pricing_<source>.json` table: dated per-model rates, prefix fallbacks,
/// and optional long-context tiers, plus the person's own per-model prices
/// layered on top.
public struct Pricing {
    public let raw: [String: Any]
    public let fileHash: String
    /// Model name → the fields the person set ("input", "output", "cached_input").
    public let overrides: [String: [String: Double]]
    private let models: [String: [[String: Any]]]
    private let namesLongestFirst: [String]
    private let prefixFallback: [(String, [String: Any])]
    private let unknownDefault: [String: Any]

    public init(path: String, overrides: [String: [String: Double]] = [:]) throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: path))
        guard let raw = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw SQLiteError(description: "\(path) is not a JSON object")
        }
        self.raw = raw
        self.overrides = overrides
        // Unchanged without overrides, so a database shared with the Python
        // dashboard isn't needlessly repriced.
        var hashed = data
        if !overrides.isEmpty, let extra = try? JSONSerialization.data(withJSONObject: overrides, options: [.sortedKeys]) {
            hashed.append(extra)
        }
        fileHash = SHA256.hash(data: hashed).map { String(format: "%02x", $0) }.joined()
        models = raw["models"] as? [String: [[String: Any]]] ?? [:]
        namesLongestFirst = models.keys.sorted { $0.count > $1.count }
        prefixFallback = (raw["prefix_fallback"] as? [[Any]] ?? []).compactMap { pair in
            guard pair.count == 2, let prefix = pair[0] as? String, let rate = pair[1] as? [String: Any] else { return nil }
            return (prefix, rate)
        }
        unknownDefault = raw["unknown_default"] as? [String: Any] ?? [:]
    }

    public var lastVerified: String? { raw["last_verified"] as? String }
    public var sourceNote: String? { raw["source"] as? String }
    public var modelNames: [String] { models.keys.sorted() }

    /// Whether the table has its own entry for this model (or the dated
    /// snapshot's base), rather than pricing it by family or default.
    public func isListed(_ model: String) -> Bool {
        models[model] != nil || namesLongestFirst.contains { model.hasPrefix($0 + "-") && isDateSuffix(String(model.dropFirst($0.count + 1))) }
    }

    public func tierMultiplier(_ tier: String?) -> Double {
        let multipliers = raw["tier_multipliers"] as? [String: Any] ?? [:]
        return (multipliers[tier ?? "default"] as? NSNumber)?.doubleValue ?? 1.0
    }

    /// pricing.resolve_rate: the latest dated entry at or before `timestamp`,
    /// with dated snapshots (`<model>-YYYYMMDD` / `-YYYY-MM-DD`) inheriting the
    /// base model's schedule, then the long-context tier if the prompt is long.
    /// A person's price for a model replaces the table's for every date and
    /// prompt length; fields they left alone keep the table's value.
    public func resolveRate(model: String?, timestamp: String?, promptTokens: Int = 0, useOverrides: Bool = true) -> Rate {
        let model = model ?? "unknown"
        var listedName: String? = models[model] == nil ? nil : model
        if listedName == nil {
            for name in namesLongestFirst where model.hasPrefix(name + "-") {
                if isDateSuffix(String(model.dropFirst(name.count + 1))) {
                    listedName = name
                    break
                }
            }
        }
        let schedule = listedName.flatMap { models[$0] }
        let override = useOverrides ? (overrides[model] ?? listedName.flatMap { overrides[$0] }) : nil

        let chosen: [String: Any]
        if let schedule, !schedule.isEmpty {
            let ts = PyDateTime.parse(timestamp, naiveIsUTC: true)
            let ordered = schedule.enumerated().sorted { a, b in
                let fa = a.element["from"] as? String ?? "0000-01-01"
                let fb = b.element["from"] as? String ?? "0000-01-01"
                return fa != fb ? fa < fb : a.offset < b.offset
            }.map(\.element)
            var rate = ordered[0]
            for entry in ordered {
                guard let start = entry["from"] as? String else {
                    rate = entry
                    continue
                }
                if let ts, let from = PyDateTime.parse(start + "T00:00:00+00:00"), ts >= from {
                    rate = entry
                }
            }
            chosen = rate
        } else {
            chosen = prefixFallback.first { model.hasPrefix($0.0) }?.1 ?? unknownDefault
        }

        var rate = Rate(chosen)
        if let override {
            rate.overlay(override as [String: Any])
            return rate
        }
        if let longContext = chosen["long_context"] as? [String: Any],
           let above = (longContext["above_tokens"] as? NSNumber)?.intValue, promptTokens > above {
            rate.overlay(longContext)
        }
        return rate
    }
}

private func isDateSuffix(_ s: String) -> Bool {
    let b = Array(s.utf8)
    let isDigit = { (c: UInt8) in c >= 48 && c <= 57 }
    if b.count == 8 { return b.allSatisfy(isDigit) }
    if b.count == 10 {
        return b.enumerated().allSatisfy { i, c in (i == 4 || i == 7) ? c == 45 : isDigit(c) }
    }
    return false
}
