import AppKit
import WebKit

/// Routes the pages' copy buttons through NSPasteboard so they don't depend
/// on WebKit's async-clipboard permission rules for custom-scheme pages.
private let clipboardBridge = """
    (function () {
      const writeText = (text) => {
        window.webkit.messageHandlers.clipboard.postMessage(String(text));
        return Promise.resolve();
      };
      try { Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true }); } catch (e) {}
    })();
    """

/// Tells the page it's inside the native shell (vibrant sidebar, system
/// colours, room for the traffic lights).
private let nativeShell = "document.documentElement.classList.add('native');"

/// Lets the window be moved from the transparent title bar area, which the
/// web view would otherwise swallow, and zooms or minimizes on double-click
/// per the user's Dock setting.
private final class WindowDragStrip: NSView {
    override func mouseDown(with event: NSEvent) {
        guard event.clickCount == 2 else {
            window?.performDrag(with: event)
            return
        }
        switch UserDefaults.standard.string(forKey: "AppleActionOnDoubleClick") {
        case "Minimize": window?.performMiniaturize(nil)
        case "None": break
        default: window?.performZoom(nil)
        }
    }
}

final class DashboardWindowController: NSWindowController, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
    WKScriptMessageHandler {
    private let webView: WKWebView
    private var appearanceObservation: NSKeyValueObservation?
    var onClose: (() -> Void)?

    init(schemeHandler: AppSchemeHandler) {
        let config = WKWebViewConfiguration()
        config.setURLSchemeHandler(schemeHandler, forURLScheme: AppSchemeHandler.scheme)
        config.userContentController.addUserScript(
            WKUserScript(source: clipboardBridge, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        config.userContentController.addUserScript(
            WKUserScript(source: nativeShell, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView = WKWebView(frame: .zero, configuration: config)

        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 900),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                              backing: .buffered, defer: false)
        window.title = "Token Meter"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.minSize = NSSize(width: 900, height: 560)
        window.isReleasedWhenClosed = false

        // The sidebar material shows through wherever the page is transparent
        // (its sidebar); the content area paints the window background itself.
        let material = NSVisualEffectView()
        material.material = .sidebar
        material.blendingMode = .behindWindow
        material.state = .followsWindowActiveState
        window.contentView = material
        webView.frame = material.bounds
        webView.autoresizingMask = [.width, .height]
        webView.setValue(false, forKey: "drawsBackground")
        webView.underPageBackgroundColor = .clear
        material.addSubview(webView)
        let strip = WindowDragStrip(frame: NSRect(x: 0, y: material.bounds.height - 38, width: material.bounds.width, height: 38))
        strip.autoresizingMask = [.width, .minYMargin]
        material.addSubview(strip)
        window.setFrameAutosaveName("DashboardWindow")
        if !window.setFrameUsingName("DashboardWindow") { window.center() }
        super.init(window: window)

        window.delegate = self
        webView.navigationDelegate = self
        webView.uiDelegate = self
        config.userContentController.add(self, name: "clipboard")
        webView.load(URLRequest(url: AppSchemeHandler.startURL))
        appearanceObservation = NSApp.observe(\.effectiveAppearance) { [weak self] _, _ in
            DispatchQueue.main.async { self?.applySystemColors() }
        }
        NotificationCenter.default.addObserver(self, selector: #selector(applySystemColors),
                                               name: NSColor.systemColorsDidChangeNotification, object: nil)
    }

    /// WebKit exposes most AppKit colours as CSS keywords but not the window
    /// background, so that one is handed to the page directly; setting it
    /// also makes WebKit re-resolve the keyword colours after an accent or
    /// appearance change.
    @objc private func applySystemColors() {
        var hex = "#ececec"
        webView.effectiveAppearance.performAsCurrentDrawingAppearance {
            if let c = NSColor.windowBackgroundColor.usingColorSpace(.sRGB) {
                hex = String(format: "#%02x%02x%02x", Int((c.redComponent * 255).rounded()),
                             Int((c.greenComponent * 255).rounded()), Int((c.blueComponent * 255).rounded()))
            }
        }
        webView.evaluateJavaScript("document.documentElement.style.setProperty('--native-window-bg', '\(hex)')")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        pageLoaded = true
        applySystemColors()
        if let view = pendingView {
            pendingView = nil
            showView(view)
        }
    }

    private var pageLoaded = false
    private var pendingView: String?

    /// Switches the page to one of its views, e.g. "settings".
    func showView(_ view: String) {
        guard pageLoaded else { pendingView = view; return }
        webView.evaluateJavaScript("location.hash = '#\(view)'")
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    func show() {
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Re-runs the page's own refresh so it picks up a sync started from the menu.
    func refreshPage() {
        webView.evaluateJavaScript("typeof refresh === 'function' && refresh()")
    }

    @objc func reloadPage(_ sender: Any?) { webView.reload() }

    /// Breaks the message-handler retain cycle so the owner can release this
    /// controller: a page left running would keep rescanning logs on its
    /// 60-second refresh with no window to show the result in.
    func windowWillClose(_ notification: Notification) {
        webView.stopLoading()
        webView.configuration.userContentController.removeScriptMessageHandler(forName: "clipboard")
        onClose?()
    }


    // Only app:// stays in the window; real links open in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url, url.scheme != AppSchemeHandler.scheme else {
            decisionHandler(.allow)
            return
        }
        if url.scheme == "http" || url.scheme == "https" { NSWorkspace.shared.open(url) }
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, url.scheme == "http" || url.scheme == "https" { NSWorkspace.shared.open(url) }
        return nil
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "clipboard", let text = message.body as? String else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }
}
