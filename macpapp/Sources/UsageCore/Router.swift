import Foundation

public struct APIResponse {
    public var status: Int
    public var contentType: String
    public var body: Data

    public init(status: Int, contentType: String, body: Data) {
        self.status = status
        self.contentType = contentType
        self.body = body
    }
}

/// The Python server's HTTP routes, minus the HTTP server: the app feeds it
/// requests from its custom URL scheme. Must be called off the main thread
/// (polling does network I/O) and outside `UsageStore.sync`.
public final class Router {
    let store: UsageStore
    let webRoot: String

    public init(store: UsageStore, webRoot: String) {
        self.store = store
        self.webRoot = webRoot
    }

    public func handle(method: String, path: String, query: String?) -> APIResponse {
        let qs = parseQuery(query)
        let sources = store.sourceNames
        let unknownWithAll = "unknown source; expected one of: all, \(sources.joined(separator: ", "))"
        let unknown = "unknown source; expected one of: \(sources.joined(separator: ", "))"

        func validSource() -> String? {
            guard let value = qs["source"] else { return "all" }
            return value == "all" || sources.contains(value) ? value : nil
        }

        if method == "POST" {
            guard path == "/api/remote-usage/poll" else { return notFound }
            let source = qs["source"] ?? "claude"
            guard sources.contains(source) else { return error(400, unknown) }
            return json { try self.store.pollRemoteUsage(source) }
        }

        if let page = Router.staticPage(path: path, webRoot: webRoot) { return page }

        switch path {
        case "/api/usage":
            guard let source = validSource() else { return error(400, unknownWithAll) }
            return json { try self.store.sync { try self.store.buildUsagePayload(source) } }
        case "/api/app/dashboard":
            guard let source = validSource() else { return error(400, unknownWithAll) }
            let days = min(3650, max(1, qs["days"].flatMap { Int($0) } ?? 7))
            return json { try self.store.sync { try self.store.buildAppDashboard(source, days: days) } }
        case "/api/app/transcript":
            guard let id = qs["id"] else { return error(400, "missing id") }
            return json { try self.store.sync { try self.store.buildTranscript(sessionId: id) } }
        case "/api/session":
            guard let source = validSource() else { return error(400, unknownWithAll) }
            let id = qs["id"] ?? ""
            return json { ["query": id, "results": try self.store.sync { try self.store.findSessions(id, source: source) }] }
        case "/api/all-sessions":
            guard let source = validSource() else { return error(400, unknownWithAll) }
            return json { ["sessions": try self.store.sync { try self.store.listAllSessions(source) }] }
        case "/api/limit-snapshots":
            let source = qs["source"]
            if let source, !sources.contains(source) { return error(400, unknown) }
            return json { ["snapshots": try self.store.sync { try self.store.listLimitSnapshots(source) }] }
        case "/api/weekly-usage":
            let source = qs["source"]
            if let source, !sources.contains(source) { return error(400, unknown) }
            let limit = qs["limit"].flatMap { Int($0.trimmingCharacters(in: .whitespaces)) } ?? 20
            return json {
                try self.store.sync {
                    var result: [String: Any] = ["weeks": try self.store.listWeeklyUsage(source, limit: limit)]
                    if let source { result["prediction"] = try self.store.predictWeeklyUsage(source) }
                    return result
                }
            }
        default:
            return notFound
        }
    }

    private var notFound: APIResponse { APIResponse(status: 404, contentType: "text/plain", body: Data()) }

    /// The HTML pages, which don't need the store — so they can be shown
    /// while the first log index is still running.
    public static func staticPage(path: String, webRoot: String) -> APIResponse? {
        let name: String
        switch path {
        case "/", "/index.html": name = "index.html"
        case "/sessions", "/sessions.html": name = "sessions.html"
        default: return nil
        }
        guard let data = FileManager.default.contents(atPath: webRoot + "/" + name) else {
            return APIResponse(status: 404, contentType: "text/plain", body: Data())
        }
        return APIResponse(status: 200, contentType: "text/html; charset=utf-8", body: data)
    }

    private func error(_ status: Int, _ message: String) -> APIResponse {
        APIResponse(status: status, contentType: "application/json", body: encodeJSON(["error": message]))
    }

    private func json(_ producer: () throws -> Any) -> APIResponse {
        do {
            return APIResponse(status: 200, contentType: "application/json", body: encodeJSON(try producer()))
        } catch {
            return self.error(500, errorText(error))
        }
    }
}

/// `parse_qs` semantics that matter here: '+' is a space, blank values are
/// dropped, and the first value of a repeated key wins.
func parseQuery(_ query: String?) -> [String: String] {
    var result: [String: String] = [:]
    for pair in (query ?? "").split(separator: "&") {
        let parts = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2 else { continue }
        func decode(_ s: Substring) -> String {
            let spaced = s.replacingOccurrences(of: "+", with: " ")
            return spaced.removingPercentEncoding ?? spaced
        }
        let key = decode(parts[0]), value = decode(parts[1])
        if !value.isEmpty && result[key] == nil { result[key] = value }
    }
    return result
}

/// JSONSerialization raises (an uncatchable ObjC exception) on NaN/inf, so
/// those become null first.
public func encodeJSON(_ value: Any) -> Data {
    func sanitize(_ v: Any) -> Any {
        switch v {
        case let d as [String: Any]: return d.mapValues(sanitize)
        case let a as [Any]: return a.map(sanitize)
        case let x as Double where !x.isFinite: return NSNull()
        default: return v
        }
    }
    let clean = sanitize(value)
    guard JSONSerialization.isValidJSONObject(clean),
          let data = try? JSONSerialization.data(withJSONObject: clean, options: [.fragmentsAllowed]) else {
        return Data(#"{"error": "response could not be encoded as JSON"}"#.utf8)
    }
    return data
}
