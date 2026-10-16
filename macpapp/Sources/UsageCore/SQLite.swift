import Foundation
import SQLite3

public struct SQLiteError: Error, CustomStringConvertible {
    public let description: String
}

private let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

/// One result row. Values are Int, Double, String, Data or NSNull — the same
/// shapes Python's sqlite3 module hands back, so rows serialize to the same JSON.
public struct Row {
    public let columns: [String]
    public let values: [Any]

    public subscript(_ name: String) -> Any {
        guard let i = columns.firstIndex(of: name) else { return NSNull() }
        return values[i]
    }
    public subscript(_ index: Int) -> Any { values[index] }

    public func string(_ name: String) -> String? { self[name] as? String }
    public func double(_ name: String) -> Double? {
        switch self[name] {
        case let v as Double: return v
        case let v as Int: return Double(v)
        default: return nil
        }
    }
    public func int(_ name: String) -> Int? {
        switch self[name] {
        case let v as Int: return v
        case let v as Double: return Int(v)
        default: return nil
        }
    }
    public var dict: [String: Any] { Dictionary(uniqueKeysWithValues: zip(columns, values)) }
}

/// Thin wrapper over the system libsqlite3. Not thread-safe: the owning
/// `UsageStore` serializes every call onto one queue.
public final class SQLiteDB {
    private var handle: OpaquePointer?

    public init(path: String) throws {
        if sqlite3_open_v2(path, &handle, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nil) != SQLITE_OK {
            let message = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "unknown error"
            sqlite3_close(handle)
            throw SQLiteError(description: "could not open \(path): \(message)")
        }
        sqlite3_busy_timeout(handle, 5000)
        try exec("PRAGMA journal_mode=WAL")
    }

    deinit { sqlite3_close(handle) }

    private var errorMessage: String { String(cString: sqlite3_errmsg(handle)) }

    public func exec(_ sql: String) throws {
        if sqlite3_exec(handle, sql, nil, nil, nil) != SQLITE_OK {
            throw SQLiteError(description: "\(errorMessage) — in: \(sql.prefix(200))")
        }
    }

    private func prepare(_ sql: String) throws -> OpaquePointer {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else {
            throw SQLiteError(description: "\(errorMessage) — in: \(sql.prefix(200))")
        }
        return stmt
    }

    private func bind(_ stmt: OpaquePointer, _ params: [Any?]) throws {
        for (i, param) in params.enumerated() {
            let idx = Int32(i + 1)
            let rc: Int32
            switch param {
            case nil, is NSNull: rc = sqlite3_bind_null(stmt, idx)
            case let v as Int: rc = sqlite3_bind_int64(stmt, idx, Int64(v))
            case let v as Int64: rc = sqlite3_bind_int64(stmt, idx, v)
            case let v as Bool: rc = sqlite3_bind_int64(stmt, idx, v ? 1 : 0)
            case let v as Double: rc = sqlite3_bind_double(stmt, idx, v)
            case let v as String: rc = sqlite3_bind_text(stmt, idx, v, -1, SQLITE_TRANSIENT)
            default: throw SQLiteError(description: "unsupported parameter type: \(type(of: param!))")
            }
            if rc != SQLITE_OK { throw SQLiteError(description: errorMessage) }
        }
    }

    public func query(_ sql: String, _ params: [Any?] = []) throws -> [Row] {
        let stmt = try prepare(sql)
        defer { sqlite3_finalize(stmt) }
        try bind(stmt, params)
        let count = sqlite3_column_count(stmt)
        let columns = (0..<count).map { String(cString: sqlite3_column_name(stmt, $0)) }
        var rows: [Row] = []
        while true {
            let rc = sqlite3_step(stmt)
            if rc == SQLITE_DONE { break }
            guard rc == SQLITE_ROW else { throw SQLiteError(description: "\(errorMessage) — in: \(sql.prefix(200))") }
            var values: [Any] = []
            values.reserveCapacity(Int(count))
            for i in 0..<count {
                switch sqlite3_column_type(stmt, i) {
                case SQLITE_INTEGER: values.append(Int(sqlite3_column_int64(stmt, i)))
                case SQLITE_FLOAT: values.append(sqlite3_column_double(stmt, i))
                case SQLITE_TEXT: values.append(String(cString: sqlite3_column_text(stmt, i)))
                case SQLITE_BLOB:
                    let n = Int(sqlite3_column_bytes(stmt, i))
                    values.append(n > 0 ? Data(bytes: sqlite3_column_blob(stmt, i), count: n) : Data())
                default: values.append(NSNull())
                }
            }
            rows.append(Row(columns: columns, values: values))
        }
        return rows
    }

    public func queryOne(_ sql: String, _ params: [Any?] = []) throws -> Row? {
        try query(sql, params).first
    }

    /// Runs a statement and returns the number of rows it changed.
    @discardableResult
    public func run(_ sql: String, _ params: [Any?] = []) throws -> Int {
        try runMany(sql, [params])
    }

    @discardableResult
    public func runMany(_ sql: String, _ rows: [[Any?]]) throws -> Int {
        let stmt = try prepare(sql)
        defer { sqlite3_finalize(stmt) }
        var changes = 0
        for params in rows {
            sqlite3_reset(stmt)
            sqlite3_clear_bindings(stmt)
            try bind(stmt, params)
            let rc = sqlite3_step(stmt)
            guard rc == SQLITE_DONE || rc == SQLITE_ROW else {
                throw SQLiteError(description: "\(errorMessage) — in: \(sql.prefix(200))")
            }
            changes += Int(sqlite3_changes(handle))
        }
        return changes
    }

    /// All-or-nothing, like Python's `with conn:` block. IMMEDIATE takes the
    /// write lock up front: a deferred transaction that reads first fails
    /// outright (no busy wait) if another process commits before its first write.
    public func transaction<T>(_ body: () throws -> T) throws -> T {
        try exec("BEGIN IMMEDIATE")
        do {
            let result = try body()
            try exec("COMMIT")
            return result
        } catch {
            try? exec("ROLLBACK")
            throw error
        }
    }
}
