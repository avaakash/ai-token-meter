import Foundation
import UsageCore
import WebKit

/// Serves the dashboard pages and their `/api/...` calls under `app://`,
/// replacing the Python HTTP server: no port is opened, so nothing else on
/// the machine can reach the API.
final class AppSchemeHandler: NSObject, WKURLSchemeHandler {
    static let scheme = "app"
    static let startURL = URL(string: "app://dashboard/index.html")!

    private let webRoot: String
    private let router: () -> Router?
    private let onSync: () -> Void
    private var activeTasks = Set<ObjectIdentifier>()

    /// `router` blocks until the store has finished its startup scan;
    /// `onSync` runs on the main thread after the page triggers a sync.
    init(webRoot: String, router: @escaping () -> Router?, onSync: @escaping () -> Void) {
        self.webRoot = webRoot
        self.router = router
        self.onSync = onSync
    }

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        let request = task.request
        guard let url = request.url else {
            task.didFailWithError(URLError(.badURL))
            return
        }
        let id = ObjectIdentifier(task)
        activeTasks.insert(id)
        let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        let method = request.httpMethod ?? "GET"
        let path = url.path.isEmpty ? "/" : url.path

        DispatchQueue.global(qos: .userInitiated).async {
            let response = Router.staticPage(path: path, webRoot: self.webRoot)
                ?? self.router()?.handle(method: method, path: path, query: components?.percentEncodedQuery)
                ?? APIResponse(status: 503, contentType: "application/json",
                               body: encodeJSON(["error": "dashboard backend failed to start"]))
            DispatchQueue.main.async {
                if method == "POST" { self.onSync() }
                // WebKit raises if a task is answered after it was stopped.
                guard self.activeTasks.remove(id) != nil else { return }
                let headers = ["Content-Type": response.contentType, "Cache-Control": "no-store",
                               "Content-Length": String(response.body.count)]
                task.didReceive(HTTPURLResponse(url: url, statusCode: response.status, httpVersion: "HTTP/1.1",
                                                headerFields: headers)!)
                task.didReceive(response.body)
                task.didFinish()
            }
        }
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {
        activeTasks.remove(ObjectIdentifier(task))
    }
}
