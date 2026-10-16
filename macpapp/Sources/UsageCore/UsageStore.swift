import CryptoKit
import Foundation

public struct AppPaths {
    public var databasePath: String
    public var pricingDirectory: String
    public var home: String

    public init(databasePath: String, pricingDirectory: String, home: String = defaultHome()) {
        self.databasePath = databasePath
        self.pricingDirectory = pricingDirectory
        self.home = home
    }

    public static func defaultHome() -> String {
        ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
    }
}

public struct TokenFields {
    public var input = 0
    public var output = 0
    public var cacheRead = 0
    public var cache5m = 0
    public var cache1h = 0
    public var reasoningOutput = 0
    public var serviceTier: String?
}

public struct Costs {
    public var cost = 0.0
    public var costNoCache = 0.0
    public var costInput = 0.0
    public var costOutput = 0.0
    public var costCache = 0.0
    public var costCacheRead = 0.0
    public var costCacheWrite = 0.0
}

struct FileState {
    var mtime: Double
    var size: Int
    var offset: Int
    var carryModel: String?
    var carryServiceTier: String?
    var carrySessionId: String?
    var carryCwd: String?
}

struct WindowBounds {
    var start: PyDateTime
    var end: PyDateTime
    var remotePct: Any  // number or NSNull, passed straight through to JSON
}

struct RemoteWindows {
    var fiveHour: WindowBounds?
    var weekly: WindowBounds?
    var remoteStatus: [String: Any]
    var remotePolledAt: Any
}

protocol UsageSource: AnyObject {
    var name: String { get }
    var pricing: Pricing { get set }
    func logFiles(home: String) -> [String]
    func scanFile(_ db: SQLiteDB, path: String, state: [String: FileState], home: String) throws
    func computeCosts(model: String?, timestamp: String?, tokens: TokenFields) -> Costs
    func tokenFields(from row: Row) -> TokenFields
    func remoteWindows(_ db: SQLiteDB) throws -> RemoteWindows
    func heuristicWindow(_ db: SQLiteDB, type: String, now: PyDateTime) throws -> (PyDateTime, PyDateTime)?
    /// Names kept outside the transcripts (an app's own metadata).
    func syncSessionTitles(_ db: SQLiteDB, home: String) throws
    func backfillFirstPrompts(_ db: SQLiteDB, home: String) throws
}

public struct ValidationError: Error, CustomStringConvertible {
    public let description: String
}

let messageInsertSQL = """
    INSERT OR IGNORE INTO messages
    (message_id, source, model, timestamp, day, weekday, hour, project, cwd, session_id,
     input_tokens, output_tokens, cache_read_tokens, cache_5m_tokens,
     cache_1h_tokens, reasoning_output_tokens, service_tier, cost, cost_no_cache,
     cost_input, cost_output, cost_cache, cost_cache_read, cost_cache_write)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

/// Owns the SQLite cache. Every database touch runs on `queue`; call
/// `sync` from outside, never from inside another `sync` block.
public final class UsageStore {
    public let paths: AppPaths
    let db: SQLiteDB
    let claude: ClaudeSource
    let codex: CodexSource
    let sources: [UsageSource]
    private let queue = DispatchQueue(label: "usage-store")

    static let schemaVersion = 6
    static let dayFieldsVersion = 1
    static let projectFieldVersion = 2
    static let firstPromptVersion = 1

    public init(paths: AppPaths) throws {
        self.paths = paths
        try FileManager.default.createDirectory(
            atPath: (paths.databasePath as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        db = try SQLiteDB(path: paths.databasePath)
        try db.exec("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        let settings = try Self.readSettings(db)
        claude = ClaudeSource(pricing: try Self.loadPricing(paths, "claude", settings))
        codex = CodexSource(pricing: try Self.loadPricing(paths, "codex", settings))
        sources = [claude, codex]
        try initDB()
    }

    public func sync<T>(_ body: () throws -> T) rethrows -> T {
        try queue.sync(execute: body)
    }

    func source(named name: String) -> UsageSource? { sources.first { $0.name == name } }
    public var sourceNames: [String] { sources.map(\.name) }

    /// Startup work the Python server does in main() before serving.
    public func prepare() throws {
        try sync {
            for source in sources { try backfillWeeklyUsageFromSnapshots(source.name) }
            try db.transaction { try ensurePricingCurrent() }
            try scanAll()
        }
    }

    // MARK: Schema

    private func ensureColumns(_ table: String, _ columns: [(String, String)]) throws {
        let existing = Set(try db.query("PRAGMA table_info(\(table))").compactMap { $0["name"] as? String })
        for (name, type) in columns where !existing.contains(name) {
            try db.exec("ALTER TABLE \(table) ADD COLUMN \(name) \(type)")
        }
    }

    private func initDB() throws {
        try db.exec("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        let current = Int(try db.queryOne("SELECT value FROM meta WHERE key = 'schema_version'")?.string("value") ?? "") ?? 0
        if current < Self.schemaVersion {
            try db.exec("DROP TABLE IF EXISTS messages")
            try db.exec("DROP TABLE IF EXISTS file_state")
            try db.exec("DROP TABLE IF EXISTS session_titles")
            try db.run("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", [String(Self.schemaVersion)])
        }
        try db.exec("""
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                source TEXT NOT NULL DEFAULT 'claude',
                model TEXT,
                timestamp TEXT,
                day TEXT,
                weekday INTEGER,
                hour INTEGER,
                project TEXT,
                cwd TEXT,
                session_id TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_5m_tokens INTEGER,
                cache_1h_tokens INTEGER,
                reasoning_output_tokens INTEGER,
                service_tier TEXT,
                cost REAL,
                cost_no_cache REAL,
                cost_input REAL,
                cost_output REAL,
                cost_cache REAL,
                cost_cache_read REAL,
                cost_cache_write REAL
            )
            """)
        try db.exec("""
            CREATE TABLE IF NOT EXISTS file_state (
                path TEXT PRIMARY KEY,
                mtime REAL,
                size INTEGER,
                offset INTEGER,
                carry_model TEXT,
                carry_service_tier TEXT,
                carry_session_id TEXT,
                carry_cwd TEXT
            )
            """)
        try ensureColumns("file_state", [("carry_model", "TEXT"), ("carry_service_tier", "TEXT"),
                                         ("carry_session_id", "TEXT"), ("carry_cwd", "TEXT")])
        try db.exec("""
            CREATE TABLE IF NOT EXISTS session_titles (
                session_id TEXT PRIMARY KEY,
                ai_title TEXT,
                custom_title TEXT
            )
            """)
        // app_title: a name kept outside the transcripts (the Claude desktop
        // app's session metadata, Codex's session_index.jsonl), with
        // app_title_source "user" when the person renamed it there.
        try ensureColumns("session_titles", [("app_title", "TEXT"), ("app_title_source", "TEXT"), ("first_prompt", "TEXT")])
        // limit_snapshots, remote_usage_polls and the weekly tables hold
        // remote-polled history that can't be rebuilt from logs, so they are
        // never dropped on a schema bump.
        try db.exec("""
            CREATE TABLE IF NOT EXISTS limit_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_type TEXT NOT NULL,
                percentage REAL NOT NULL,
                dollar_value REAL NOT NULL,
                window_start TEXT,
                window_end TEXT,
                created_at TEXT NOT NULL
            )
            """)
        try ensureColumns("limit_snapshots", [("source", "TEXT NOT NULL DEFAULT 'claude'")])
        try db.exec("""
            CREATE TABLE IF NOT EXISTS remote_usage_polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                polled_at TEXT NOT NULL,
                five_hour_pct REAL,
                five_hour_resets_at TEXT,
                weekly_pct REAL,
                weekly_resets_at TEXT,
                raw_json TEXT
            )
            """)
        try db.exec("""
            CREATE TABLE IF NOT EXISTS weekly_windows (
                source TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (source, window_start)
            )
            """)
        try db.exec("""
            CREATE TABLE IF NOT EXISTS weekly_daily_max_pct (
                source TEXT NOT NULL,
                window_start TEXT NOT NULL,
                day TEXT NOT NULL,
                latest_percentage REAL NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, window_start, day)
            )
            """)
        let dailyCols = Set(try db.query("PRAGMA table_info(weekly_daily_max_pct)").compactMap { $0["name"] as? String })
        if dailyCols.contains("max_percentage") && !dailyCols.contains("latest_percentage") {
            try db.exec("ALTER TABLE weekly_daily_max_pct RENAME COLUMN max_percentage TO latest_percentage")
        }
        try db.exec("CREATE INDEX IF NOT EXISTS idx_messages_day ON messages(day)")
        try db.exec("CREATE INDEX IF NOT EXISTS idx_messages_model ON messages(model)")
        try db.exec("CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project)")
        try db.exec("CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)")
        try db.exec("CREATE INDEX IF NOT EXISTS idx_messages_weekday_hour ON messages(weekday, hour)")
        try db.exec("CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source)")
        try db.exec("CREATE INDEX IF NOT EXISTS idx_limit_snapshots_type ON limit_snapshots(source, window_type, created_at)")
        try db.exec("CREATE INDEX IF NOT EXISTS idx_remote_usage_polls_polled_at ON remote_usage_polls(polled_at)")
    }

    // MARK: Incremental log indexing

    func scanAll() throws {
        try db.transaction {
            var state: [String: FileState] = [:]
            for row in try db.query("""
                SELECT path, mtime, size, offset, carry_model, carry_service_tier, carry_session_id, carry_cwd
                FROM file_state
                """) {
                guard let path = row.string("path") else { continue }
                state[path] = FileState(
                    mtime: row.double("mtime") ?? 0, size: row.int("size") ?? 0, offset: row.int("offset") ?? 0,
                    carryModel: row.string("carry_model"), carryServiceTier: row.string("carry_service_tier"),
                    carrySessionId: row.string("carry_session_id"), carryCwd: row.string("carry_cwd"))
            }
            for source in sources {
                for path in source.logFiles(home: paths.home) {
                    try source.scanFile(db, path: path, state: state, home: paths.home)
                }
                try source.syncSessionTitles(db, home: paths.home)
            }
            try ensureFirstPromptsCurrent()
            try ensureDayFieldsCurrent()
            try ensureProjectFieldCurrent()
            try recomputeCosts(whereSQL: "WHERE cost_cache_read IS NULL")
        }
    }

    private func recomputeCosts(whereSQL: String) throws {
        let rows = try db.query("""
            SELECT message_id, source, model, timestamp, input_tokens, output_tokens,
                   cache_read_tokens, cache_5m_tokens, cache_1h_tokens,
                   reasoning_output_tokens, service_tier
            FROM messages \(whereSQL)
            """)
        var updates: [[Any?]] = []
        for row in rows {
            guard let name = row.string("source"), let source = source(named: name) else { continue }
            let c = source.computeCosts(model: row.string("model"), timestamp: row.string("timestamp"),
                                        tokens: source.tokenFields(from: row))
            updates.append([c.cost, c.costNoCache, c.costInput, c.costOutput, c.costCache, c.costCacheRead,
                            c.costCacheWrite, row["message_id"]])
        }
        if !updates.isEmpty {
            try db.runMany("""
                UPDATE messages
                SET cost = ?, cost_no_cache = ?, cost_input = ?, cost_output = ?, cost_cache = ?,
                    cost_cache_read = ?, cost_cache_write = ?
                WHERE message_id = ?
                """, updates)
        }
    }

    /// Full recompute when either pricing file changed since the last run
    /// (same combined hash as the Python dashboard, so a shared database
    /// isn't needlessly recomputed). Startup only — it walks every row.
    func ensurePricingCurrent() throws {
        let joined = sources.map(\.pricing.fileHash).joined()
        let combined = SHA256.hash(data: Data(joined.utf8)).map { String(format: "%02x", $0) }.joined()
        let stored = try db.queryOne("SELECT value FROM meta WHERE key = 'pricing_hash'")?.string("value")
        var needsRecompute = stored != combined
        if !needsRecompute {
            needsRecompute = (try db.queryOne("SELECT COUNT(*) AS n FROM messages WHERE cost_cache_read IS NULL")?.int("n") ?? 0) > 0
        }
        guard needsRecompute else { return }
        try recomputeCosts(whereSQL: "")
        try db.run("INSERT OR REPLACE INTO meta (key, value) VALUES ('pricing_hash', ?)", [combined])
    }

    private func metaInt(_ key: String) throws -> Int {
        Int(try db.queryOne("SELECT value FROM meta WHERE key = ?", [key])?.string("value") ?? "") ?? 0
    }

    /// On a version bump each log is re-read from the top only as far as its
    /// first prompt.
    private func ensureFirstPromptsCurrent() throws {
        guard try metaInt("first_prompt_version") < Self.firstPromptVersion else { return }
        for source in sources { try source.backfillFirstPrompts(db, home: paths.home) }
        try db.run("INSERT OR REPLACE INTO meta (key, value) VALUES ('first_prompt_version', ?)", [String(Self.firstPromptVersion)])
    }

    private func ensureDayFieldsCurrent() throws {
        guard try metaInt("day_fields_version") < Self.dayFieldsVersion else { return }
        let updates: [[Any?]] = try db.query("SELECT message_id, timestamp FROM messages").map { row in
            let f = dayFields(row.string("timestamp"))
            return [f.day, f.weekday, f.hour, row["message_id"]]
        }
        try db.runMany("UPDATE messages SET day = ?, weekday = ?, hour = ? WHERE message_id = ?", updates)
        try db.run("INSERT OR REPLACE INTO meta (key, value) VALUES ('day_fields_version', ?)", [String(Self.dayFieldsVersion)])
    }

    private func ensureProjectFieldCurrent() throws {
        guard try metaInt("project_field_version") < Self.projectFieldVersion else { return }
        let updates: [[Any?]] = try db.query("SELECT message_id, cwd FROM messages").map { row in
            [projectName(fromCwd: row.string("cwd")), row["message_id"]]
        }
        try db.runMany("UPDATE messages SET project = ? WHERE message_id = ?", updates)
        try db.run("INSERT OR REPLACE INTO meta (key, value) VALUES ('project_field_version', ?)", [String(Self.projectFieldVersion)])
    }

    func setMeta(_ key: String, _ value: String) throws {
        try db.run("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", [key, value])
    }
}

// MARK: Shared helpers

/// (session_id, text) rows; keeps the first prompt ever recorded.
func storeFirstPrompts(_ db: SQLiteDB, _ rows: [[Any?]]) throws {
    guard !rows.isEmpty else { return }
    try db.runMany("""
        INSERT INTO session_titles (session_id, first_prompt) VALUES (?, ?)
        ON CONFLICT(session_id) DO UPDATE SET first_prompt = excluded.first_prompt
        WHERE session_titles.first_prompt IS NULL
        """, rows)
}

/// (session_id, title, source) rows; writes only rows that changed.
func storeAppTitles(_ db: SQLiteDB, _ rows: [[Any?]]) throws {
    guard !rows.isEmpty else { return }
    try db.runMany("""
        INSERT INTO session_titles (session_id, app_title, app_title_source) VALUES (?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            app_title = excluded.app_title, app_title_source = excluded.app_title_source
        WHERE session_titles.app_title IS NOT excluded.app_title
           OR session_titles.app_title_source IS NOT excluded.app_title_source
        """, rows)
}

/// (day, weekday, hour) in local time, or ("unknown", nil, nil).
func dayFields(_ timestamp: String?) -> (day: String, weekday: Int?, hour: Int?) {
    guard let dt = PyDateTime.parse(timestamp) else { return ("unknown", nil, nil) }
    let f = dt.localDayFields()
    return (f.day, f.weekday, f.hour)
}

private let worktreeMarkers = ["/.claude/worktrees/", "/.worktrees/"]

/// Groups a worktree checkout (or a subdirectory of one) under its repo.
func projectName(fromCwd cwd: String?) -> String {
    guard let cwd, !cwd.isEmpty else { return "unknown" }
    var normalized = cwd
    while normalized.hasSuffix("/") { normalized.removeLast() }
    for marker in worktreeMarkers {
        if let range = normalized.range(of: marker) {
            normalized = String(normalized[..<range.lowerBound])
            break
        }
    }
    let base = normalized.split(separator: "/", omittingEmptySubsequences: false).last.map(String.init) ?? normalized
    return base.isEmpty ? normalized : base
}

func fileStat(_ path: String) -> (mtime: Double, size: Int)? {
    var st = stat()
    guard stat(path, &st) == 0 else { return nil }
    return (Double(st.st_mtimespec.tv_sec) + Double(st.st_mtimespec.tv_nsec) * 1e-9, Int(st.st_size))
}

/// Python's `glob(root + "/**/*.jsonl", recursive=True)` (or just
/// `root/*.jsonl`), which skips hidden files and directories.
func jsonlFiles(under root: String, recursive: Bool) -> [String] {
    let fm = FileManager.default
    var results: [String] = []
    if recursive {
        guard let walker = fm.enumerator(atPath: root) else { return [] }
        while let rel = walker.nextObject() as? String {
            let name = (rel as NSString).lastPathComponent
            if name.hasPrefix(".") {
                if (walker.fileAttributes?[.type] as? FileAttributeType) == .typeDirectory { walker.skipDescendants() }
                continue
            }
            if name.hasSuffix(".jsonl") { results.append(root + "/" + rel) }
        }
    } else {
        for name in (try? fm.contentsOfDirectory(atPath: root)) ?? [] where !name.hasPrefix(".") && name.hasSuffix(".jsonl") {
            results.append(root + "/" + name)
        }
    }
    return results.sorted()
}

/// Calls `body` with each complete ("\n"-terminated) line after `offset` and
/// the byte offset where that line started; returns the offset just past
/// the last complete line. A trailing partial line is left for the next scan.
func forEachLine(path: String, from offset: Int, _ body: (Data, Int) throws -> Void) throws -> Int? {
    // Read, not mmap: a log truncated mid-scan would SIGBUS a mapped read.
    guard let handle = FileHandle(forReadingAtPath: path) else { return nil }
    defer { try? handle.close() }
    let data: Data
    do {
        try handle.seek(toOffset: UInt64(offset))
        data = try handle.readToEnd() ?? Data()
    } catch {
        return nil
    }
    var newOffset = offset
    try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
        guard let base = raw.bindMemory(to: UInt8.self).baseAddress else { return }
        var lineStart = 0
        while lineStart < data.count {
            guard let nl = memchr(base + lineStart, 0x0A, data.count - lineStart) else { break }
            let lineEnd = base.distance(to: nl.assumingMemoryBound(to: UInt8.self))
            newOffset = offset + lineEnd + 1
            try body(Data(bytesNoCopy: UnsafeMutableRawPointer(mutating: base + lineStart),
                          count: lineEnd - lineStart, deallocator: .none), offset + lineStart)
            lineStart = lineEnd + 1
        }
    }
    return newOffset
}

func contains(_ line: Data, _ needle: String) -> Bool {
    let n = Array(needle.utf8)
    return line.withUnsafeBytes { raw in
        memmem(raw.baseAddress, raw.count, n, n.count) != nil
    }
}

/// `json.loads(line.strip())` for an object, nil for anything else.
func parseJSONObject(_ line: Data) -> [String: Any]? {
    guard !line.isEmpty else { return nil }
    if let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] { return obj }
    // Python decodes with errors="replace" and accepts lone UTF-16 surrogate
    // escapes (e.g. a truncated emoji, "\ud83d"); JSONSerialization rejects
    // both, so repair them before giving up on the line.
    var text = String(decoding: line, as: UTF8.self)
    text = text.replacingOccurrences(of: #"\\u[dD][89abAB][0-9a-fA-F]{2}(?!\\u[dD][c-fC-F][0-9a-fA-F]{2})"#,
                                     with: #"\\ufffd"#, options: .regularExpression)
    text = text.replacingOccurrences(of: #"(?<!\\u[dD][89abAB][0-9a-fA-F]{2})\\u[dD][c-fC-F][0-9a-fA-F]{2}"#,
                                     with: #"\\ufffd"#, options: .regularExpression)
    let repaired = Data(text.utf8)
    guard repaired != line else { return nil }
    return try? JSONSerialization.jsonObject(with: repaired) as? [String: Any]
}

func intValue(_ v: Any?) -> Int {
    switch v {
    case let n as NSNumber: return n.intValue
    case let s as String: return Int(s.trimmingCharacters(in: .whitespaces)) ?? 0
    default: return 0
    }
}

/// Python truthiness for `x or fallback` on JSON-ish values.
func truthy(_ v: Any?) -> Bool {
    switch v {
    case nil, is NSNull: return false
    case let s as String: return !s.isEmpty
    case let n as NSNumber: return n.doubleValue != 0
    case let d as [String: Any]: return !d.isEmpty
    case let a as [Any]: return !a.isEmpty
    default: return true
    }
}

func nonEmptyString(_ v: Any?) -> String? {
    guard let s = v as? String, !s.isEmpty else { return nil }
    return s
}

func orNull(_ v: Any?) -> Any { v ?? NSNull() }
