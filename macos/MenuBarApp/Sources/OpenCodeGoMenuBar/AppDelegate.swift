import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var proxy = ProxyController()
    private var statusLabel: NSMenuItem!
    private var portLabel: NSMenuItem!
    private var toggleItem: NSMenuItem!
    private var timer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "arrow.triangle.branch", accessibilityDescription: "OpenCode Go")
            button.image?.isTemplate = true
        }
        statusItem.menu = buildMenu()

        proxy.onStateChange = { [weak self] in self?.refresh() }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.proxy.refreshHealth()
        }
        timer?.tolerance = 1.0
    }

    private func buildMenu() -> NSMenu {
        let menu = NSMenu()

        let header = NSMenuItem(title: "OpenCode Go", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)

        statusLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        statusLabel.isEnabled = false
        menu.addItem(statusLabel)

        portLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        portLabel.isEnabled = false
        menu.addItem(portLabel)

        menu.addItem(.separator())

        toggleItem = NSMenuItem(title: "Start Proxy", action: #selector(toggleProxy), keyEquivalent: "")
        toggleItem.target = self
        menu.addItem(toggleItem)

        let openLogs = NSMenuItem(title: "Open Logs", action: #selector(openLogs), keyEquivalent: "")
        openLogs.target = self
        menu.addItem(openLogs)

        let copyPort = NSMenuItem(title: "Copy Port", action: #selector(copyPort), keyEquivalent: "")
        copyPort.target = self
        menu.addItem(copyPort)

        let revealLogs = NSMenuItem(title: "Reveal Log File", action: #selector(revealLogs), keyEquivalent: "")
        revealLogs.target = self
        menu.addItem(revealLogs)

        menu.addItem(.separator())

        let quit = NSMenuItem(title: "Quit OpenCode Go", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        return menu
    }

    private func refresh() {
        guard let statusItem, let button = statusItem.button else { return }
        let state = proxy.state
        let statusTitle = state.isRunning ? (state.isHealthy ? "Running" : "Starting") : "Stopped"

        let symbol = state.isRunning ? "arrow.triangle.branch.fill" : "arrow.triangle.branch"
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "OpenCode Go")
        button.image?.isTemplate = true

        if let statusLabel {
            statusLabel.title = statusTitle
        }
        if let portLabel {
            portLabel.title = state.isRunning && state.isHealthy ? "Port \(state.port)" : "Port \(state.port) (not listening)"
        }
        if let toggleItem {
            toggleItem.title = state.isRunning ? "Stop Proxy" : "Start Proxy"
            toggleItem.isEnabled = !state.isStarting
        }
    }

    @objc private func toggleProxy() {
        if proxy.state.isRunning {
            proxy.stop()
        } else {
            proxy.start()
        }
        refresh()
    }

    @objc private func openLogs() {
        proxy.openLogs()
    }

    @objc private func copyPort() {
        proxy.copyPort()
    }

    @objc private func revealLogs() {
        proxy.revealLogFile()
    }

    @objc private func quitApp() {
        proxy.stop()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        proxy.stop()
    }
}
