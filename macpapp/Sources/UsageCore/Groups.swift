import Foundation

/// A named set of models for adding up cost. Built-in groups match by rule
/// (a source, model-name prefixes), so models released later join them on
/// their own; on top of that the person can add models to any group or take
/// them out. A model can be in any number of groups.
struct ModelGroup {
    var id: String
    var name: String
    var source: String?
    var prefixes: [String] = []
    var models: [String] = []
    var excluded: [String] = []

    var hasRule: Bool { source != nil || !prefixes.isEmpty }

    func matchesRule(_ model: String, source: String) -> Bool {
        guard hasRule else { return false }
        if let required = self.source, required != source { return false }
        return prefixes.isEmpty || prefixes.contains { model.hasPrefix($0) }
    }

    func contains(_ model: String, source: String) -> Bool {
        if excluded.contains(model) { return false }
        return models.contains(model) || matchesRule(model, source: source)
    }

    /// What the rule picks up, in words, for the Settings page.
    var ruleDescription: String? {
        guard hasRule else { return nil }
        if prefixes.isEmpty { return source == "codex" ? "Every Codex model" : "Every Claude model" }
        return "Models named " + prefixes.map { $0 + "…" }.joined(separator: ", ")
    }

    init(id: String, name: String, source: String? = nil, prefixes: [String] = []) {
        self.id = id
        self.name = name
        self.source = source
        self.prefixes = prefixes
    }

    init?(_ dict: [String: Any]) {
        guard let id = dict["id"] as? String, let name = dict["name"] as? String else { return nil }
        self.id = id
        self.name = name
        source = dict["source"] as? String
        prefixes = dict["prefixes"] as? [String] ?? []
        models = dict["models"] as? [String] ?? []
        excluded = dict["excluded"] as? [String] ?? []
    }

    var dict: [String: Any] {
        var d: [String: Any] = ["id": id, "name": name, "prefixes": prefixes, "models": models, "excluded": excluded]
        if let source { d["source"] = source }
        return d
    }

    static let defaults: [ModelGroup] = [
        ModelGroup(id: "anthropic", name: "Anthropic", source: "claude"),
        ModelGroup(id: "openai", name: "OpenAI", source: "codex"),
        ModelGroup(id: "opus", name: "Opus", source: "claude", prefixes: ["claude-opus", "claude-3-opus"]),
        ModelGroup(id: "sonnet", name: "Sonnet", source: "claude", prefixes: ["claude-sonnet", "claude-3-5-sonnet", "claude-3-7-sonnet"]),
        ModelGroup(id: "haiku", name: "Haiku", source: "claude", prefixes: ["claude-haiku", "claude-3-5-haiku", "claude-3-haiku"]),
        ModelGroup(id: "fable", name: "Fable", source: "claude", prefixes: ["claude-fable"]),
        ModelGroup(id: "mythos", name: "Mythos", source: "claude", prefixes: ["claude-mythos"]),
        ModelGroup(id: "gpt-6", name: "GPT-6", source: "codex", prefixes: ["gpt-6"]),
        ModelGroup(id: "gpt-5.6", name: "GPT-5.6", source: "codex", prefixes: ["gpt-5.6"]),
        ModelGroup(id: "gpt-5.5", name: "GPT-5.5", source: "codex", prefixes: ["gpt-5.5"]),
        ModelGroup(id: "gpt-5.4", name: "GPT-5.4", source: "codex", prefixes: ["gpt-5.4"]),
    ]
}

extension UsageStore {
    static let maxGroups = 60
    static let maxGroupName = 40

    func modelGroups() throws -> [ModelGroup] {
        guard let saved = try Self.readSettings(db)["groups"] as? [[String: Any]] else { return ModelGroup.defaults }
        return saved.compactMap(ModelGroup.init)
    }

    /// Every model the person has used, then the rest of both price tables.
    private func knownModels() throws -> [(model: String, source: String, used: Bool)] {
        var out: [(String, String, Bool)] = []
        var seen = Set<String>()
        for r in try db.query("""
            SELECT source, model, SUM(cost) AS cost FROM messages
            WHERE model IS NOT NULL AND model NOT IN ('<synthetic>', 'unknown') GROUP BY source, model ORDER BY cost DESC
            """) {
            guard let model = r.string("model"), let source = r.string("source"), seen.insert(model).inserted else { continue }
            out.append((model, source, true))
        }
        for source in sources {
            for model in source.pricing.modelNames + source.pricing.overrides.keys.sorted() where seen.insert(model).inserted {
                out.append((model, source.name, false))
            }
        }
        return out
    }

    func groupsPayload() throws -> [String: Any] {
        let known = try knownModels()
        let sourceOf = Dictionary(known.map { ($0.model, $0.source) }, uniquingKeysWith: { a, _ in a })
        let groups: [[String: Any]] = try modelGroups().map { g in
            // Members among the models we know of, plus anything added by hand.
            var members = known.filter { g.contains($0.model, source: $0.source) }.map(\.model)
            members += g.models.filter { !members.contains($0) && !g.excluded.contains($0) }
            return [
                "id": g.id, "name": g.name, "rule": orNull(g.ruleDescription),
                "members": members.map { ["model": $0, "source": orNull(sourceOf[$0])] },
            ]
        }
        let models: [[String: Any]] = known.map { ["model": $0.model, "source": $0.source, "used": $0.used] }
        return ["list": groups, "models": models, "customized": try Self.readSettings(db)["groups"] != nil]
    }

    /// Group totals for the dashboard, from its per-model rows for the period.
    func groupTotals(_ byModel: [[String: Any]]) throws -> [[String: Any]] {
        try modelGroups().compactMap { g in
            let rows = byModel.filter { r in
                guard let model = r["model"] as? String, let source = r["source"] as? String else { return false }
                return g.contains(model, source: source)
            }
            guard !rows.isEmpty else { return nil }
            let sum = { (key: String) in rows.reduce(0.0) { $0 + ((($1[key] as? NSNumber)?.doubleValue) ?? 0) } }
            return [
                "id": g.id, "name": g.name, "cost": sum("cost"), "message_count": Int(sum("message_count")),
                "models": rows.compactMap { $0["model"] as? String },
            ]
        }.sorted { ($0["cost"] as? Double ?? 0) > ($1["cost"] as? Double ?? 0) }
    }

    /// One change to the groups. The first change saves the built-in groups
    /// as the person's own list; resetting goes back to the built-in ones.
    public func editGroups(action: String, id: String?, name: String?, model: String?) throws {
        var groups = try modelGroups()
        func index() throws -> Int {
            guard let id, let i = groups.firstIndex(where: { $0.id == id }) else { throw ValidationError(description: "no such group") }
            return i
        }
        func cleanName() throws -> String {
            let trimmed = (name ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty, trimmed.count <= Self.maxGroupName else {
                throw ValidationError(description: "a group name needs 1 to \(Self.maxGroupName) characters")
            }
            if groups.contains(where: { $0.id != id && $0.name.caseInsensitiveCompare(trimmed) == .orderedSame }) {
                throw ValidationError(description: "there's already a group called \(trimmed)")
            }
            return trimmed
        }
        func cleanModel() throws -> String {
            let trimmed = (model ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty, trimmed.count <= 200 else { throw ValidationError(description: "a model name is required") }
            return trimmed
        }
        func sourceOf(_ model: String) throws -> String {
            try knownModels().first { $0.model == model }?.source ?? (model.hasPrefix("claude") ? "claude" : "codex")
        }

        var settings = try Self.readSettings(db)
        switch action {
        case "create":
            guard groups.count < Self.maxGroups else { throw ValidationError(description: "at most \(Self.maxGroups) groups") }
            groups.append(ModelGroup(id: "g-" + UUID().uuidString.prefix(8).lowercased(), name: try cleanName()))
        case "rename":
            let i = try index()
            groups[i].name = try cleanName()
        case "delete":
            groups.remove(at: try index())
        case "add":
            let i = try index(), m = try cleanModel()
            groups[i].excluded.removeAll { $0 == m }
            if !groups[i].contains(m, source: try sourceOf(m)) { groups[i].models.append(m) }
        case "remove":
            let i = try index(), m = try cleanModel()
            groups[i].models.removeAll { $0 == m }
            if groups[i].matchesRule(m, source: try sourceOf(m)) && !groups[i].excluded.contains(m) { groups[i].excluded.append(m) }
        case "reset":
            settings["groups"] = nil
            try apply(settings)
            return
        default:
            throw ValidationError(description: "unknown group action: \(action)")
        }
        settings["groups"] = groups.map(\.dict)
        try apply(settings)
    }
}
