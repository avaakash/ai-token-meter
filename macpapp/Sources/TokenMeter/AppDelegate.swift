import AppKit
import ServiceManagement
import UsageCore

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem!
    private var windowController: DashboardWindowController?
    private var store: UsageStore?
    private var router: Router?
    private var startupError: String?
    private var summaries: [SourceSummary] = []
    private var polling = false
    private let ready = DispatchGroup()
    private let background = DispatchQueue(label: "token-meter.background", qos: .utility)
    private var pollTimer: Timer?
    private var summaryTimer: Timer?
    // App Nap would otherwise stretch the 10-minute poll timer arbitrarily
    // while the app has no visible window.
    private var activity: NSObjectProtocol?

    func applicationDidFinishLaunching(_ notification: Notification) {
        signal(SIGPIPE, SIG_IGN)
        NSApp.mainMenu = buildMainMenu()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "gauge.with.dots.needle.50percent",
                                           accessibilityDescription: "Token Meter")
        statusItem.button?.imagePosition = .imageLeading
        statusItem.button?.title = " …"
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.delegate = self
        statusItem.menu = menu
        activity = ProcessInfo.processInfo.beginActivity(options: .userInitiatedAllowingIdleSystemSleep,
                                                         reason: "Polling usage limits")
        startBackend()
    }

    // MARK: Backend

    /// The first launch indexes every log file, which can take a while, so it
    /// runs off the main thread. `router` is only read by the scheme handler
    /// after waiting on `ready`; everything else is main-thread state.
    private func startBackend() {
        ready.enter()
        background.async {
            var result: Result<UsageStore, Error>
            do {
                let resources = Bundle.main.resourcePath!
                let supportRoot = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                let support = supportRoot.appendingPathComponent("TokenMeter")
                // Builds before the rename kept their data under "UsageDashboard";
                // carry it over rather than re-indexing and losing limit history.
                let legacy = supportRoot.appendingPathComponent("UsageDashboard")
                if !FileManager.default.fileExists(atPath: support.path), FileManager.default.fileExists(atPath: legacy.path) {
                    try? FileManager.default.moveItem(at: legacy, to: support)
                }
                let dbPath = ProcessInfo.processInfo.environment["TOKEN_METER_DB"]
                    ?? support.appendingPathComponent("usage.sqlite3").path
                let store = try UsageStore(paths: AppPaths(databasePath: dbPath, pricingDirectory: resources))
                try store.prepare()
                self.router = Router(store: store, webRoot: resources + "/web")
                result = .success(store)
            } catch {
                NSLog("Token Meter failed to start: %@", "\(error)")
                result = .failure(error)
            }
            self.ready.leave()
            DispatchQueue.main.async { self.backendReady(result) }
        }
    }

    private func backendReady(_ result: Result<UsageStore, Error>) {
        switch result {
        case .success(let store): self.store = store
        case .failure(let error):
            startupError = "\(error)"
            updateStatusTitle()
            return
        }
        pollNow()
        pollTimer = Timer.scheduledTimer(withTimeInterval: remotePollInterval, repeats: true) { [weak self] _ in
            self?.pollNow()
        }
        summaryTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.refreshSummaries()
        }
    }

    /// Polls both sources (Claude's API, then `codex app-server`), then
    /// refreshes the menu and any open dashboard.
    @objc private func pollNow() {
        guard let store, !polling else { return }
        polling = true
        updateStatusTitle()
        background.async {
            for source in store.sourceNames {
                do {
                    _ = try store.pollRemoteUsage(source)
                } catch {
                    NSLog("poll %@ failed: %@", source, "\(error)")
                }
            }
            DispatchQueue.main.async {
                self.polling = false
                self.refreshSummaries()
                self.windowController?.refreshPage()
            }
        }
    }

    private func refreshSummaries() {
        guard let store else { return }
        background.async {
            let summaries = (try? store.limitSummaries()) ?? []
            DispatchQueue.main.async {
                self.summaries = summaries
                self.updateStatusTitle()
            }
        }
    }

    // MARK: Status item

    private func updateStatusTitle() {
        guard let button = statusItem.button else { return }
        if startupError != nil {
            button.title = " !"
            return
        }
        let claude = summaries.first { $0.source == "claude" }
        let primary = (claude?.fiveHour?.percent != nil || claude?.weekly?.percent != nil)
            ? claude : summaries.first { $0.weekly?.percent != nil }
        var parts: [String] = []
        var highest = 0.0
        if let primary {
            if let pct = primary.fiveHour?.percent { parts.append("5h \(Int(pct.rounded()))%"); highest = max(highest, pct) }
            if let pct = primary.weekly?.percent { parts.append("W \(Int(pct.rounded()))%"); highest = max(highest, pct) }
        }
        let text = " " + (parts.isEmpty ? (summaries.isEmpty ? "…" : "–") : parts.joined(separator: " · "))
        let color: NSColor? = highest >= 90 ? .systemRed : highest >= 75 ? .systemOrange : nil
        button.attributedTitle = NSAttributedString(
            string: text, attributes: color.map { [.foregroundColor: $0] } ?? [:])
        button.toolTip = "Token Meter — 5-hour and weekly limit usage" + (primary.map { " (\(label($0.source)))" } ?? "")
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        if let startupError {
            menu.addItem(disabled("Failed to start: \(startupError)"))
        } else if store == nil {
            menu.addItem(disabled("Indexing logs…"))
        }
        for s in summaries where s.fiveHour != nil || s.weekly != nil || s.lastError != nil {
            menu.addItem(heading(label(s.source)))
            if let w = s.fiveHour { menu.addItem(disabled("5-hour: " + describe(w))) }
            if let w = s.weekly { menu.addItem(disabled("Weekly: " + describe(w))) }
            if let error = s.lastError { menu.addItem(disabled("Sync failed: \(error)")) }
        }
        menu.addItem(.separator())
        menu.addItem(item("Open Dashboard", #selector(openDashboard), "o"))
        let sync = item(polling ? "Syncing…" : "Sync Now", #selector(pollNow), "R")
        sync.isEnabled = store != nil && !polling
        menu.addItem(sync)
        let loginStatus = SMAppService.mainApp.status
        let login = item(loginStatus == .requiresApproval ? "Open at Login (approve in System Settings…)" : "Open at Login",
                         #selector(toggleLoginItem), "")
        login.state = loginStatus == .enabled ? .on : .off
        menu.addItem(login)
        menu.addItem(.separator())
        menu.addItem(item("Quit Token Meter", #selector(NSApplication.terminate(_:)), "q", target: NSApp))
    }

    private func label(_ source: String) -> String { source == "codex" ? "Codex" : "Claude" }

    private func describe(_ w: WindowSummary) -> String {
        let used = w.percent.map { "\(Int($0.rounded()))% used" } ?? String(format: "$%.2f (no %% reported)", w.cost)
        // Reported reset times jitter by about a second around the minute.
        guard let resets = w.resetsAt?.addingTimeInterval(30) else { return used }
        let formatter = DateFormatter()
        formatter.dateFormat = Calendar.current.isDateInToday(resets) ? "h:mm a" : "EEE h:mm a"
        return "\(used) · resets \(formatter.string(from: resets))"
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func heading(_ title: String) -> NSMenuItem {
        let item = disabled(title)
        item.attributedTitle = NSAttributedString(string: title, attributes: [.font: NSFont.boldSystemFont(ofSize: 13)])
        return item
    }

    private func item(_ title: String, _ action: Selector, _ key: String, target: AnyObject? = nil) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = target ?? self
        return item
    }

    // MARK: Actions

    @objc private func openDashboard() {
        if windowController == nil {
            let handler = AppSchemeHandler(
                webRoot: Bundle.main.resourcePath! + "/web",
                router: { [weak self] in
                    guard let self else { return nil }
                    self.ready.wait()
                    return self.router
                },
                onSync: { [weak self] in self?.refreshSummaries() })
            let controller = DashboardWindowController(schemeHandler: handler)
            controller.onClose = { [weak self] in
                self?.windowController = nil
                NSApp.setActivationPolicy(.accessory)
            }
            windowController = controller
        }
        NSApp.setActivationPolicy(.regular)
        windowController?.show()
    }

    @objc private func reloadDashboard(_ sender: Any?) { windowController?.reloadPage(sender) }

    @objc private func toggleLoginItem() {
        do {
            switch SMAppService.mainApp.status {
            case .enabled: try SMAppService.mainApp.unregister()
            case .requiresApproval: SMAppService.openSystemSettingsLoginItems()
            default: try SMAppService.mainApp.register()
            }
        } catch {
            // A menu bar app isn't frontmost, so the alert would open behind other windows.
            NSApp.activate(ignoringOtherApps: true)
            let alert = NSAlert()
            alert.messageText = "Couldn't change the login item"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openDashboard()
        return true
    }

    // MARK: Main menu (shown while the dashboard window is open)

    private func buildMainMenu() -> NSMenu {
        let main = NSMenu()
        func submenu(_ title: String, _ items: [NSMenuItem]) {
            let holder = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let menu = NSMenu(title: title)
            items.forEach(menu.addItem)
            holder.submenu = menu
            main.addItem(holder)
        }
        let selectAll = NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        submenu("Token Meter", [
            NSMenuItem(title: "Hide Token Meter", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h"),
            .separator(),
            NSMenuItem(title: "Quit Token Meter", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"),
        ])
        submenu("Edit", [
            NSMenuItem(title: "Undo", action: Selector(("undo:")), keyEquivalent: "z"),
            NSMenuItem(title: "Redo", action: Selector(("redo:")), keyEquivalent: "Z"),
            .separator(),
            NSMenuItem(title: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"),
            NSMenuItem(title: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"),
            NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"),
            selectAll,
        ])
        let reload = NSMenuItem(title: "Reload", action: #selector(reloadDashboard(_:)), keyEquivalent: "r")
        reload.target = self
        let sync = NSMenuItem(title: "Sync Now", action: #selector(pollNow), keyEquivalent: "R")
        sync.target = self
        submenu("View", [reload, sync])
        submenu("Window", [
            NSMenuItem(title: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m"),
            NSMenuItem(title: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w"),
        ])
        return main
    }
}
