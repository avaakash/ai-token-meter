import Foundation
@testable import UsageCore

// Run with: swift run usage-core-checks   (from macpapp/)

var failures = 0
var checks = 0

func check(_ condition: @autoclosure () throws -> Bool, _ message: String, line: Int = #line) {
    checks += 1
    do {
        if try !condition() {
            failures += 1
            print("FAIL (line \(line)): \(message)")
        }
    } catch {
        failures += 1
        print("FAIL (line \(line)): \(message) — threw \(error)")
    }
}

func close(_ a: Double?, _ b: Double, _ tolerance: Double = 1e-9) -> Bool {
    guard let a else { return false }
    return abs(a - b) <= tolerance * max(1, abs(b))
}

let repoRoot = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
    .appendingPathComponent("../../..").standardized.path
let ts = "2026-10-14T00:00:00Z"

func withTempStore(_ body: (UsageStore, String) throws -> Void) {
    let dir = NSTemporaryDirectory() + "usage-checks-" + UUID().uuidString
    try? FileManager.default.createDirectory(atPath: dir + "/home", withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(atPath: dir) }
    do {
        let store = try UsageStore(paths: AppPaths(databasePath: dir + "/usage.sqlite3", pricingDirectory: repoRoot + "/macpapp/Resources",
                                                   home: dir + "/home"))
        try body(store, dir + "/home")
    } catch {
        failures += 1
        print("FAIL: store setup threw \(error)")
    }
}

func writeLines(_ path: String, _ records: [[String: Any]], trailing: String = "") throws {
    try FileManager.default.createDirectory(atPath: (path as NSString).deletingLastPathComponent,
                                            withIntermediateDirectories: true)
    var text = ""
    for r in records { text += String(decoding: try JSONSerialization.data(withJSONObject: r), as: UTF8.self) + "\n" }
    try (text + trailing).write(toFile: path, atomically: true, encoding: .utf8)
}

// MARK: PyDateTime matches Python's datetime string handling

do {
    let dt = PyDateTime.parse("2026-10-16T07:38:06.481Z")!
    check(dt.isoformat() == "2026-10-16T07:38:06.481000+00:00", "millisecond Z parses and re-formats like Python")
    check(PyDateTime.parse("2026-10-15T06:58:46+00:00")!.isoformat() == "2026-10-15T06:58:46+00:00", "no fraction when zero")
    let ist = PyDateTime.parse("2026-07-15T15:30:00+05:30")!
    check(ist.isoformat() == "2026-07-15T15:30:00+05:30", "offset is preserved")
    check(ist == PyDateTime.parse("2026-07-15T10:00:00Z")!, "equality compares instants")
    check(PyDateTime.parse("2026-02-30T00:00:00Z") == nil, "invalid dates are rejected")
    check(PyDateTime.parse("garbage") == nil && PyDateTime.parse(nil) == nil, "garbage is rejected")
    check(PyDateTime.parse("2026-10-16")!.wall.hour == 0, "date-only parses as midnight")
    check(PyDateTime.fromTimestamp(1_792_652_326).isoformat() == "2026-10-22T06:58:46+00:00", "epoch seconds")
    check(PyDateTime.parse("2026-10-16T09:59:59.005548+00:00")!.adding(micros: -7 * Micros.day).isoformat()
          == "2026-10-09T09:59:59.005548+00:00", "microseconds survive arithmetic")
    check(UsageStore.floorWindowKey("2026-10-21T09:59:59.005548+00:00") as? String == "2026-10-21T10:00:00+00:00",
          "window keys round up from :59:59")
    check(UsageStore.floorWindowKey("2026-10-21T10:00:29.9+00:00") as? String == "2026-10-21T10:00:00+00:00",
          "window keys round down below :30")
    check(ClaudeSource.floorTo10Min(PyDateTime.parse("2026-07-10T12:25:33Z")!).isoformat() == "2026-07-10T12:20:00+00:00",
          "five-hour starts floor to 10 minutes")
}

// MARK: Pricing (ported from test_pricing.py)

withTempStore { store, _ in
    let claude = store.claude, codex = store.codex
    func claudeCost(_ model: String, _ t: TokenFields) -> Costs { claude.computeCosts(model: model, timestamp: ts, tokens: t) }
    func codexCost(_ model: String, _ t: TokenFields) -> Costs { codex.computeCosts(model: model, timestamp: ts, tokens: t) }

    for (model, expected) in [("claude-fable-5-1", (10.0, 50.0)), ("claude-opus-5-5", (4.0, 20.0)),
                              ("claude-sonnet-5-5", (2.0, 10.0)), ("claude-haiku-5-5", (0.1, 0.5))] {
        let rate = claude.pricing.resolveRate(model: model, timestamp: ts)
        check(rate.input == expected.0 && rate.output == expected.1, "\(model) rates")
    }
    let sol = codex.pricing.resolveRate(model: "gpt-6.1-sol-2026-09-29", timestamp: ts)
    check(sol.input == 2 && sol.output == 10 && sol.cachedInput == 0.1, "dated codex snapshot inherits base rates")
    check(close(claudeCost("claude-sonnet-5-5-20260928", TokenFields(cacheRead: 1_000_000)).costCacheRead, 0.1),
          "dated claude snapshot inherits cache discount")
    let sonnet5 = claude.pricing.resolveRate(model: "claude-sonnet-5-20260901", timestamp: ts)
    check(sonnet5.input == 2 && sonnet5.output == 10, "older model isn't captured by a longer name")
    let opus4 = claude.pricing.resolveRate(model: "claude-opus-4-20250514", timestamp: ts)
    check(opus4.input == 15 && opus4.output == 75, "dated opus 4 snapshot isn't priced by the opus fallback")
    let opus41 = claude.pricing.resolveRate(model: "claude-opus-4-1-20250805", timestamp: ts)
    let opus45 = claude.pricing.resolveRate(model: "claude-opus-4-5-20251101", timestamp: ts)
    check(opus41.input == 15 && opus45.input == 5, "opus 4.x snapshots aren't captured by claude-opus-4")

    for (model, cached) in [("claude-fable-5-1", 0.25), ("claude-opus-5-5", 0.2), ("claude-sonnet-5-5", 0.1),
                            ("claude-opus-4-8", 0.5)] {
        check(close(claudeCost(model, TokenFields(cacheRead: 1_000_000)).costCacheRead, cached), "\(model) cache read")
    }

    let short = claudeCost("claude-haiku-5-5", TokenFields(input: 1000, output: 1000, cacheRead: 97000, cache5m: 1000, cache1h: 1000))
    let long = claudeCost("claude-haiku-5-5", TokenFields(input: 1001, output: 1000, cacheRead: 97000, cache5m: 1000, cache1h: 1000))
    check(close(short.costInput, 0.0001) && close(long.costInput, 0.0005005), "haiku long-context threshold")
    check(close(long.costCacheRead, short.costCacheRead * 5) && close(long.costOutput, short.costOutput * 5),
          "long-context tier scales every rate")

    check(codex.pricing.resolveRate(model: "gpt-6.1-sol", timestamp: ts, promptTokens: 272_000).input == 2, "codex below threshold")
    check(codex.pricing.resolveRate(model: "gpt-6.1-sol", timestamp: ts, promptTokens: 272_001).input == 4, "codex above threshold")
    let lc = codexCost("gpt-6.1-sol", TokenFields(input: 300_000, output: 1000, cacheRead: 250_000, cache5m: 20000))
    check(close(lc.costInput, 0.12) && close(lc.costCacheRead, 0.05) && close(lc.costCacheWrite, 0.1)
          && close(lc.costOutput, 0.015), "codex long-context costs")

    let fields = TokenFields(input: 100_000, output: 1000, cacheRead: 70000, cache5m: 20000)
    let base = codexCost("gpt-6.1-sol", fields)
    check(close(base.cost, 0.02 + 0.007 + 0.05 + 0.01) && close(base.costNoCache, 0.21), "codex writes replace uncached input")
    for (tier, mult) in [("fast", 2.0), ("priority", 2.0), ("ultrafast", 6.0), ("batch", 0.5), ("flex", 0.5)] {
        var t = fields
        t.serviceTier = tier
        check(close(codexCost("gpt-6.1-sol", t).cost, base.cost * mult), "\(tier) tier multiplier")
    }
}

do {
    let dir = NSTemporaryDirectory() + "pricing-" + UUID().uuidString
    try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(atPath: dir) }
    let table: [String: Any] = [
        "models": ["example": [["from": NSNull(), "input": 5, "output": 25], ["from": "2026-10-01", "input": 4, "output": 20]]],
        "prefix_fallback": [], "unknown_default": ["input": 3, "output": 15],
    ]
    try JSONSerialization.data(withJSONObject: table).write(to: URL(fileURLWithPath: dir + "/p.json"))
    let pricing = try Pricing(path: dir + "/p.json")
    check(pricing.resolveRate(model: "example", timestamp: "2026-09-30T23:59:59Z").input == 5, "before a dated entry")
    check(pricing.resolveRate(model: "example", timestamp: "2026-10-01T00:00:00Z").input == 4, "on a dated entry")
    check(pricing.resolveRate(model: "example", timestamp: nil).input == 5, "no timestamp keeps the undated rate")
    check(pricing.resolveRate(model: "other", timestamp: ts).input == 3, "unknown default")
} catch {
    check(false, "dated pricing setup threw \(error)")
}

// MARK: Scanning

withTempStore { store, home in
    let path = home + "/.codex/sessions/2026/10/14/rollout.jsonl"
    try writeLines(path, [
        ["type": "turn_context", "payload": ["model": "gpt-6.1-sol", "cwd": "/repo/.claude/worktrees/x/sub"]],
        ["type": "event_msg", "timestamp": ts, "payload": [
            "type": "token_count",
            "info": ["last_token_usage": ["input_tokens": 100_000, "cached_input_tokens": 70000,
                                          "cache_write_input_tokens": 20000, "output_tokens": 1000]]]],
    ])
    try store.sync { try store.scanAll() }
    var row = try store.db.queryOne("SELECT * FROM messages")!
    check(row.int("cache_5m_tokens") == 20000 && close(row.double("cost_cache_write"), 0.05), "codex scan stores cache writes")
    check(row.string("project") == "repo", "worktree cwd groups under its repo")
    check(row.string("message_id")?.hasPrefix("codex:\(path):") == true, "codex ids use path and byte offset")

    try store.db.run("UPDATE messages SET cost = 99, cost_cache_write = 99")
    try store.db.run("UPDATE meta SET value = 'stale' WHERE key = 'pricing_hash'")
    try store.prepare()
    row = try store.db.queryOne("SELECT * FROM messages")!
    check(close(row.double("cost"), 0.087) && close(row.double("cost_cache_write"), 0.05), "pricing change reprices rows")

    // Codex moves finished sessions to archived_sessions/: adopt, don't duplicate.
    let archived = home + "/.codex/archived_sessions/rollout.jsonl"
    try FileManager.default.createDirectory(atPath: home + "/.codex/archived_sessions", withIntermediateDirectories: true)
    try FileManager.default.moveItem(atPath: path, toPath: archived)
    try store.sync { try store.scanAll() }
    check(try store.db.queryOne("SELECT COUNT(*) AS n FROM messages")?.int("n") == 1, "archived move doesn't duplicate rows")
    check(try store.db.queryOne("SELECT COUNT(*) AS n FROM file_state WHERE path = ?", [path])?.int("n") == 0,
          "stale pre-move file_state row is dropped")
}

withTempStore { store, home in
    let path = home + "/.claude/projects/-repo/session.jsonl"
    let assistant: [String: Any] = [
        "type": "assistant", "timestamp": "2026-10-14T10:00:00.000Z", "cwd": "/repo", "sessionId": "s1",
        "message": ["id": "msg_1", "model": "claude-sonnet-5-5",
                    "usage": ["input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30,
                              "cache_creation": ["ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 6]]],
    ]
    let partial = #"{"type": "assistant", "message": {"id": "msg_2", "usage": {"input_tokens": 1}"#
    try writeLines(path, [
        assistant, assistant,
        ["type": "ai-title", "sessionId": "s1", "aiTitle": "First"],
        ["type": "custom-title", "sessionId": "s1", "customTitle": "Mine"],
        ["type": "user", "message": ["content": "no usage here"]],
    ], trailing: partial)
    try store.sync { try store.scanAll() }
    check(try store.db.queryOne("SELECT COUNT(*) AS n FROM messages")?.int("n") == 1, "duplicate ids are inserted once")
    let row = try store.db.queryOne("SELECT * FROM messages")!
    check(row.int("cache_5m_tokens") == 4 && row.int("cache_1h_tokens") == 6, "cache TTL buckets")
    check(row.string("day") == PyDateTime.parse("2026-10-14T10:00:00Z")!.localDayFields().day, "day uses local time")
    let title = try store.db.queryOne("SELECT ai_title, custom_title FROM session_titles WHERE session_id = 's1'")
    check(title?.string("ai_title") == "First" && title?.string("custom_title") == "Mine", "session titles")
    let state = try store.db.queryOne("SELECT offset, size FROM file_state")!
    check(state.int("offset")! < state.int("size")!, "a trailing partial line is left for the next scan")

    // Finish the partial line: only the new message gets added.
    let handle = FileHandle(forWritingAtPath: path)!
    handle.seekToEndOfFile()
    handle.write(Data("}}\n".utf8))
    handle.closeFile()
    try store.sync { try store.scanAll() }
    check(try store.db.queryOne("SELECT COUNT(*) AS n FROM messages")?.int("n") == 2, "resumes from the saved offset")

    let dash = try store.sync { try store.buildAppDashboard("all", days: 3650) }
    let period = dash["period"] as? [String: Any]
    check(period?["message_count"] as? Int == 1 && (dash["recent_sessions"] as? [[String: Any]])?.first?["title"] as? String == "Mine",
          "app dashboard counts timestamped messages and names sessions")
    let periods = try store.sync { try store.buildPeriodTotals("all", now: PyDateTime.parse("2026-10-14T12:00:00Z")!) }
    let day = periods["1d"] as? [String: Any]
    check(day?["message_count"] as? Int == 1 && day?["since"] as? String == "2026-10-13T12:00:00.000Z",
          "period totals use a rolling UTC cutoff")
}

// MARK: Session names

do {
    func user(_ content: Any, _ extra: [String: Any] = [:]) -> [String: Any] {
        ["type": "user", "sessionId": "s1", "message": ["content": content]].merging(extra) { _, new in new }
    }
    check(ClaudeSource.firstPromptText(user("  fix the\n flaky test ")) == "fix the flaky test", "plain prompt is normalized")
    check(ClaudeSource.firstPromptText(user([["type": "text", "text": "hello"]])) == "hello", "text block prompt")
    check(ClaudeSource.firstPromptText(user("<command-message>work-issue</command-message>\n<command-name>/work-issue</command-name>\n<command-args>5364</command-args>")) == "/work-issue 5364",
          "slash command with arguments reads as typed")
    for skipped: [String: Any] in [user([["type": "tool_result", "content": "ok"]]), user("skill body", ["isMeta": true]),
                                   user("task", ["isSidechain": true]), user("<local-command-caveat>x</local-command-caveat>"),
                                   user("/code-review"), user("<command-name>/clear</command-name>")] {
        check(ClaudeSource.firstPromptText(skipped) == nil, "injected or bare-command record is not a prompt")
    }
}

withTempStore { store, home in
    for sid in ["a", "b", "c", "d"] {
        try store.db.run("INSERT INTO messages (message_id, source, session_id, timestamp, cost) VALUES (?, 'claude', ?, '2026-10-16T00:00:00.000Z', 1)", [sid, sid])
    }
    try store.db.runMany("INSERT INTO session_titles (session_id, ai_title, custom_title, app_title, app_title_source, first_prompt) VALUES (?, ?, ?, ?, ?, ?)", [
        ["a", "ai", "custom", "app rename", "user", "p"], ["b", "ai", "custom", "app auto", "auto", nil],
        ["c", "ai", nil, "app auto", "auto", nil], ["d", nil, nil, nil, nil, "first prompt"],
    ])
    var titles = Dictionary(uniqueKeysWithValues: try store.sync { try store.listAllSessions() }.map { ($0["session_id"] as! String, ($0["title"] as? String, $0["first_prompt"] as? String)) })
    check(titles["a"]?.0 == "app rename" && titles["b"]?.0 == "custom" && titles["c"]?.0 == "app auto", "title precedence")
    check(titles["d"]?.0 == nil && titles["d"]?.1 == "first prompt", "first prompt returned separately")

    let appDir = home + "/Library/Application Support/Claude/claude-code-sessions/x/y"
    try FileManager.default.createDirectory(atPath: appDir, withIntermediateDirectories: true)
    try #"{"cliSessionId":"c","title":"Renamed in app","titleSource":"user"}"#.write(toFile: appDir + "/local_1.json", atomically: true, encoding: .utf8)
    try "not json".write(toFile: appDir + "/local_2.json", atomically: true, encoding: .utf8)
    try FileManager.default.createDirectory(atPath: home + "/.codex", withIntermediateDirectories: true)
    try (#"{"id":"d","thread_name":"Old name","updated_at":"2026-10-02T00:00:00Z"}"# + "\n" +
         #"{"id":"d","thread_name":"Older","updated_at":"2026-10-01T00:00:00Z"}"# + "\n")
        .write(toFile: home + "/.codex/session_index.jsonl", atomically: true, encoding: .utf8)
    titles = Dictionary(uniqueKeysWithValues: try store.sync { try store.listAllSessions() }.map { ($0["session_id"] as! String, ($0["title"] as? String, $0["first_prompt"] as? String)) })
    check(titles["c"]?.0 == "Renamed in app", "desktop app rename is picked up")
    check(titles["d"]?.0 == "Old name", "latest Codex thread name wins")
}

// MARK: Transcripts

withTempStore { store, home in
    let usage: [String: Any] = ["input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30]
    func assistant(_ id: String, _ ts: String, _ block: [String: Any], sidechain: Bool = false) -> [String: Any] {
        ["type": "assistant", "timestamp": ts, "sessionId": "s2", "cwd": "/repo", "isSidechain": sidechain,
         "message": ["id": id, "model": "claude-sonnet-5-5", "usage": usage, "content": [block]]]
    }
    try writeLines(home + "/.claude/projects/-repo/s2.jsonl", [
        ["type": "user", "timestamp": "2026-10-14T10:00:00.000Z", "sessionId": "s2",
         "message": ["content": [["type": "text", "text": "<system-reminder>injected</system-reminder>"], ["type": "text", "text": "fix it"]]]],
        assistant("msg_a", "2026-10-14T10:00:05.000Z", ["type": "text", "text": "Looking."]),
        assistant("msg_a", "2026-10-14T10:00:06.000Z", ["type": "tool_use", "name": "Bash"]),
        ["type": "user", "timestamp": "2026-10-14T10:00:07.000Z", "sessionId": "s2", "message": ["content": [["type": "tool_result", "content": "ok"]]]],
        ["type": "user", "timestamp": "2026-10-14T10:05:00.000Z", "sessionId": "s2", "message": ["content": "<command-name>/clear</command-name>"]],
        ["type": "user", "timestamp": "2026-10-14T10:06:00.000Z", "sessionId": "s2", "message": ["content": "second prompt"]],
        assistant("msg_b", "2026-10-14T10:06:05.000Z", ["type": "text", "text": "Done."]),
    ])
    try writeLines(home + "/.claude/projects/-repo/s2/subagents/agent-x.jsonl", [
        assistant("msg_c", "2026-10-14T10:01:00.000Z", ["type": "text", "text": "sub"], sidechain: true),
    ])
    let t = try store.sync { try store.buildTranscript(sessionId: "s2") }
    let turns = t["turns"] as? [[String: Any]] ?? []
    check(turns.map { $0["prompt"] as? String ?? "-" } == ["fix it", "/clear", "second prompt"], "turn boundaries skip reminders and tool results")
    check(turns.first?["message_count"] as? Int == 2 && turns.first?["subagent_messages"] as? Int == 1,
          "a streamed message counts once and sub-agent work joins its turn")
    check((turns.first?["tools"] as? [[String: Any]])?.first?["name"] as? String == "Bash", "tool use is tallied per turn")
    let turnTotal = turns.reduce(0.0) { $0 + ($1["cost"] as? Double ?? 0) }
    check(close(turnTotal, (t["session"] as? [String: Any])?["cost"] as? Double ?? -1), "turn costs add up to the session")
}

// MARK: Windows, collapsing, router

do {
    let s = { (h: Int) in PyDateTime(micros: Int64(h) * Micros.hour) }
    // (start, end) windows; index 2 is the current one and overlaps index 1.
    let windows = [(s(0), s(168)), (s(170), s(338)), (s(171), s(339)), (s(100), s(200))]
    let kept = collapseOverlapping(windows, start: { $0.0 }, end: { $0.1 }, anchor: 2)
    check(kept.map(\.0) == [s(171), s(0)], "overlapping fragments collapse onto the anchor")
}

do {
    check(parseQuery("source=claude&limit=7")["limit"] == "7", "query parsing")
    check(parseQuery("source=")["source"] == nil, "blank values are dropped like parse_qs")
    check(parseQuery("id=a+b%20c")["id"] == "a b c", "plus and percent decoding")
    check(parseJSONObject(Data(#"{"a":"x\ud83d","b":1}"#.utf8))?["b"] as? Int == 1, "lone surrogate escapes don't drop the line")
    check(parseJSONObject(Data(#"{"a":"😀"}"#.utf8))?["a"] as? String == "😀", "surrogate pairs stay intact")
    let encoded = String(decoding: encodeJSON(["x": Double.nan, "y": [1.5]]), as: UTF8.self)
    check(encoded.contains("\"x\":null") && encoded.contains("1.5"), "non-finite numbers become null")
}

withTempStore { store, _ in
    let router = Router(store: store, webRoot: repoRoot + "/macpapp/Resources/web")
    check(router.handle(method: "GET", path: "/api/usage", query: "source=bogus").status == 400, "unknown source is a 400")
    check(router.handle(method: "GET", path: "/nope", query: nil).status == 404, "unknown path is a 404")
    check(router.handle(method: "GET", path: "/", query: nil).status == 200, "serves index.html")
    let usage = router.handle(method: "GET", path: "/api/usage", query: "source=all")
    let payload = try? JSONSerialization.jsonObject(with: usage.body) as? [String: Any]
    check(usage.status == 200 && payload?["periods"] != nil && payload?["limits"] != nil, "usage payload on an empty store")
    let weekly = router.handle(method: "GET", path: "/api/weekly-usage", query: "source=claude&limit=7")
    check(weekly.status == 200, "weekly usage on an empty store")
    let dash = router.handle(method: "GET", path: "/api/app/dashboard", query: "source=all&days=7")
    let body = try? JSONSerialization.jsonObject(with: dash.body) as? [String: Any]
    check(dash.status == 200 && body?["period"] != nil && (body?["limits"] as? [String: Any])?["codex"] != nil
          && (body?["by_source"] as? [Any])?.count == 2, "app dashboard payload on an empty store")
}

// MARK: Settings: the person's own prices and budgets

withTempStore { store, home in
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let stamp = formatter.string(from: Date())
    try writeLines(home + "/.claude/projects/-repo/s.jsonl", [[
        "type": "assistant", "timestamp": stamp, "cwd": "/repo", "sessionId": "s1",
        "message": ["id": "m1", "model": "claude-sonnet-5-5", "usage": ["input_tokens": 1_000_000, "output_tokens": 1_000_000]],
    ]])
    try store.sync { try store.scanAll() }
    func cost() throws -> Double { try store.db.queryOne("SELECT cost FROM messages")?.double("cost") ?? -1 }
    func models() throws -> [[String: Any]] { try store.sync { try store.priceList("claude") }["models"] as? [[String: Any]] ?? [] }

    let first = try models().first
    let builtIn = first?["built_in"] as? [String: Double] ?? [:]
    check(first?["model"] as? String == "claude-sonnet-5-5" && first?["edited"] as? Bool == false, "used models lead the price list")
    let base = try cost()
    check(close(base, (builtIn["input"] ?? 0) + (builtIn["output"] ?? 0)), "built-in price applies by default")

    try store.sync {
        try store.setPrice("claude", model: "claude-sonnet-5-5",
                           fields: ["input": 1, "output": 2, "cached_input": builtIn["cached_input"] ?? 0])
    }
    check(close(try cost(), 3), "an edited price reprices stored messages")
    let edited = try models().first
    check((edited?["price"] as? [String: Double])?["input"] == 1 && edited?["edited"] as? Bool == true, "the list shows the edited price")
    let saved = (try UsageStore.readSettings(store.db)["prices"] as? [String: Any])?["claude"] as? [String: Any]
    check(saved?["claude-sonnet-5-5"] as? [String: Double] == ["input": 1, "output": 2], "fields equal to the built-in price aren't stored")
    let reopened = try UsageStore(paths: store.paths)
    check(close(reopened.claude.pricing.resolveRate(model: "claude-sonnet-5-5-20261001", timestamp: stamp).input, 1),
          "edited prices survive a restart and cover dated snapshots")

    try store.sync { try store.setPrice("claude", model: "claude-sonnet-5-5", fields: nil) }
    check(close(try cost(), base), "resetting a model restores the built-in price")
    try store.sync { try store.setPrice("claude", model: "claude-new-model", fields: ["input": 3, "output": 15]) }
    check(try models().contains { $0["model"] as? String == "claude-new-model" && $0["edited"] as? Bool == true },
          "an added model is kept even at the fallback price")
    try store.sync { try store.resetPrices("claude") }
    check(try UsageStore.readSettings(store.db)["prices"] == nil, "reset clears the source's prices")
    check((try? store.sync { try store.setPrice("claude", model: "x", fields: ["input": -1]) }) == nil, "negative prices are rejected")

    try store.sync { try store.setBudgets("claude", ["day": base / 2, "month": base * 4]) }
    let status = try store.sync { try store.budgetStatus("claude") }
    check(status.count == 2 && close(status[0]["pct"] as? Double, 200) && close(status[1]["pct"] as? Double, 25),
          "budgets measure the current period's spend")
    check(try store.limitSummaries().first { $0.source == "claude" }?.budgets.count == 2, "menu bar summaries carry budgets")
    check((try? store.sync { try store.setBudgets("claude", ["day": 0]) }) == nil, "a zero budget is rejected")

    let router = Router(store: store, webRoot: repoRoot + "/macpapp/Resources/web")
    check(router.handle(method: "POST", path: "/api/app/prices", query: "source=claude&model=claude-sonnet-5-5&input=abc").status == 400,
          "a non-numeric price is a 400")
    let saveBudget = router.handle(method: "POST", path: "/api/app/budgets", query: "source=codex&week=50")
    let body = try? JSONSerialization.jsonObject(with: saveBudget.body) as? [String: Any]
    let codexAmounts = ((body?["budgets"] as? [String: Any])?["codex"] as? [String: Any])?["amounts"] as? [String: Double]
    check(saveBudget.status == 200 && codexAmounts == ["week": 50], "the budgets route saves and returns settings")
    check(router.handle(method: "GET", path: "/api/app/settings", query: "source=codex").status == 200, "settings route")
}

// MARK: Model groups

withTempStore { store, home in
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let stamp = formatter.string(from: Date())
    func message(_ id: String, _ model: String) -> [String: Any] {
        ["type": "assistant", "timestamp": stamp, "cwd": "/repo", "sessionId": "s1",
         "message": ["id": id, "model": model, "usage": ["input_tokens": 1_000_000, "output_tokens": 0]]]
    }
    try writeLines(home + "/.claude/projects/-repo/s.jsonl", [message("a", "claude-opus-5-5"), message("b", "claude-sonnet-5-5")])
    try store.sync { try store.scanAll() }
    func groups() throws -> [[String: Any]] { try store.sync { try store.buildAppDashboard("all", days: 7) }["by_group"] as? [[String: Any]] ?? [] }
    func group(_ id: String) throws -> [String: Any]? { try groups().first { $0["id"] as? String == id } }
    func members(_ id: String) throws -> [String] {
        let list = try store.sync { try store.groupsPayload() }["list"] as? [[String: Any]] ?? []
        return (list.first { $0["id"] as? String == id }?["members"] as? [[String: Any]] ?? []).compactMap { $0["model"] as? String }
    }
    let total = try store.db.queryOne("SELECT SUM(cost) AS c FROM messages")?.double("c") ?? 0

    check(try store.sync { try store.modelGroups() }.map(\.id).prefix(3) == ["anthropic", "openai", "opus"], "built-in groups come first")
    check(close(try group("anthropic")?["cost"] as? Double, total), "Anthropic adds up every Claude model")
    check(try group("opus")?["models"] as? [String] == ["claude-opus-5-5"] && group("openai") == nil,
          "family groups match by name; groups without usage are left out")
    check(try members("opus").contains("claude-opus-4-8"), "built-in groups list unused models from the price table")

    try store.sync { try store.editGroups(action: "create", id: nil, name: "Daily drivers", model: nil) }
    let mine = try store.sync { try store.modelGroups() }.last!
    check(mine.name == "Daily drivers" && mine.models.isEmpty, "create adds an empty group")
    try store.sync { try store.editGroups(action: "add", id: mine.id, name: nil, model: "claude-sonnet-5-5") }
    try store.sync { try store.editGroups(action: "add", id: mine.id, name: nil, model: "claude-opus-5-5") }
    check(close(try group(mine.id)?["cost"] as? Double, total), "a model can be in several groups")

    try store.sync { try store.editGroups(action: "remove", id: "opus", name: nil, model: "claude-opus-5-5") }
    check(try group("opus") == nil && !members("opus").contains("claude-opus-5-5"), "removing a rule-matched model excludes it")
    try store.sync { try store.editGroups(action: "add", id: "opus", name: nil, model: "claude-opus-5-5") }
    check(try group("opus") != nil, "adding it back undoes the exclusion")

    check((try? store.sync { try store.editGroups(action: "rename", id: mine.id, name: "opus", model: nil) }) == nil,
          "group names are unique, ignoring case")
    try store.sync { try store.editGroups(action: "rename", id: mine.id, name: "Favourites", model: nil) }
    check(try store.sync { try store.modelGroups() }.contains { $0.name == "Favourites" }, "rename")
    try store.sync { try store.editGroups(action: "delete", id: "sonnet", name: nil, model: nil) }
    check(try !store.sync { try store.modelGroups() }.contains { $0.id == "sonnet" }, "a built-in group can be deleted")
    try store.sync { try store.editGroups(action: "reset", id: nil, name: nil, model: nil) }
    check(try store.sync { try store.modelGroups() }.map(\.id) == ModelGroup.defaults.map(\.id)
          && UsageStore.readSettings(store.db)["groups"] == nil, "reset restores the built-in groups")

    let router = Router(store: store, webRoot: repoRoot + "/macpapp/Resources/web")
    check(router.handle(method: "POST", path: "/api/app/groups", query: "action=create&name=").status == 400, "an empty group name is a 400")
    check(router.handle(method: "POST", path: "/api/app/groups", query: "action=add&id=nope&model=x").status == 400, "an unknown group is a 400")
    check(router.handle(method: "POST", path: "/api/app/groups", query: "action=create&name=Team").status == 200, "groups route")
}

print("\(checks - failures)/\(checks) checks passed")
exit(failures == 0 ? 0 : 1)
