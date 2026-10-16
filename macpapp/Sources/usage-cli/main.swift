import Foundation
import UsageCore

// Debug/parity tool: runs the app's backend against a chosen database
// without the UI.
//   usage-cli --db PATH --pricing DIR [--home DIR] [--web DIR] scan
//   usage-cli ... get '/api/usage?source=all'
//   usage-cli ... post '/api/remote-usage/poll?source=claude'
//   usage-cli ... --web Resources/web --port 4871 serve   (development preview)

var options: [String: String] = [:]
var positional: [String] = []
var args = CommandLine.arguments.dropFirst()
while let arg = args.popFirst() {
    if arg.hasPrefix("--"), let value = args.popFirst() {
        options[String(arg.dropFirst(2))] = value
    } else {
        positional.append(arg)
    }
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(2)
}

guard let dbPath = options["db"], let pricingDir = options["pricing"], let command = positional.first else {
    fail("usage: usage-cli --db PATH --pricing DIR [--home DIR] [--web DIR] (scan | get URL | post URL)")
}

signal(SIGPIPE, SIG_IGN)
let paths = AppPaths(databasePath: dbPath, pricingDirectory: pricingDir, home: options["home"] ?? AppPaths.defaultHome())
let store: UsageStore
do {
    store = try UsageStore(paths: paths)
    let started = Date()
    try store.prepare()
    FileHandle.standardError.write(Data(String(format: "prepared in %.2fs\n", Date().timeIntervalSince(started)).utf8))
} catch {
    fail("startup failed: \(error)")
}

/// Development only: the app's pages and API over plain HTTP on
/// 127.0.0.1, so the UI can be opened in a browser.
func serve(_ router: Router, port: UInt16) -> Never {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    var yes: Int32 = 1
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let bound = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
    }
    guard bound == 0, listen(fd, 16) == 0 else { fail("could not listen on 127.0.0.1:\(port)") }
    FileHandle.standardError.write(Data("serving http://127.0.0.1:\(port)/\n".utf8))
    while true {
        let client = accept(fd, nil, nil)
        guard client >= 0 else { continue }
        DispatchQueue.global().async {
            defer { close(client) }
            var request = Data()
            var buffer = [UInt8](repeating: 0, count: 8192)
            while !request.contains(Data("\r\n\r\n".utf8)) {
                let n = read(client, &buffer, buffer.count)
                if n <= 0 { return }
                request.append(buffer, count: n)
            }
            let line = String(decoding: request.prefix(while: { $0 != 0x0D }), as: UTF8.self).split(separator: " ")
            guard line.count >= 2, let url = URLComponents(string: String(line[1])) else { return }
            let response = router.handle(method: String(line[0]), path: url.path, query: url.percentEncodedQuery)
            var head = "HTTP/1.1 \(response.status) \(response.status == 200 ? "OK" : "Error")\r\n"
            head += "Content-Type: \(response.contentType)\r\nContent-Length: \(response.body.count)\r\n"
            head += "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
            let out = Data(head.utf8) + response.body
            _ = out.withUnsafeBytes { write(client, $0.baseAddress, out.count) }
        }
    }
}

switch command {
case "scan":
    break
case "serve":
    serve(Router(store: store, webRoot: options["web"] ?? "."), port: UInt16(options["port"] ?? "4871") ?? 4871)
case "get", "post":
    guard positional.count > 1, let url = URLComponents(string: positional[1]) else { fail("missing URL") }
    let router = Router(store: store, webRoot: options["web"] ?? ".")
    let response = router.handle(method: command.uppercased(), path: url.path, query: url.percentEncodedQuery)
    FileHandle.standardError.write(Data("HTTP \(response.status)\n".utf8))
    FileHandle.standardOutput.write(response.body)
    exit(response.status == 200 ? 0 : 1)
default:
    fail("unknown command: \(command)")
}
