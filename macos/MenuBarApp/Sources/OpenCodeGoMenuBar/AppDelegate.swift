import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var proxy = ProxyController()
    private var statusLabel: NSMenuItem!
    private var portLabel: NSMenuItem!
    private var upstreamLabel: NSMenuItem!
    private var quotaLabel: NSMenuItem!
    private var resetLabel: NSMenuItem!
    private var todayLabel: NSMenuItem!
    private var modelLabel: NSMenuItem!
    private var catalogLabel: NSMenuItem!
    private var refreshCatalogItem: NSMenuItem!
    private var configLabel: NSMenuItem!
    private var configToggleItem: NSMenuItem!
    private var usageBarItems: [NSMenuItem] = []
    private var toggleItem: NSMenuItem!
    private var timer: Timer?

    private static let dayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter
    }()

    private static let weekdayFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "EEE"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter
    }()

    private static let resetFormatters: [ISO8601DateFormatter] = {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let withoutFraction = ISO8601DateFormatter()
        withoutFraction.formatOptions = [.withInternetDateTime]
        return [withFraction, withoutFraction]
    }()

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
        proxy.refreshConfigStatus()
        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.proxy.refreshHealth()
            self?.proxy.refreshState()
            self?.proxy.refreshCatalogCount()
            self?.proxy.refreshConfigStatus()
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

        upstreamLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        upstreamLabel.isEnabled = false
        menu.addItem(upstreamLabel)

        menu.addItem(.separator())

        let quotaSection = NSMenuItem(title: "Quota", action: nil, keyEquivalent: "")
        quotaSection.isEnabled = false
        menu.addItem(quotaSection)

        quotaLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        quotaLabel.isEnabled = false
        menu.addItem(quotaLabel)

        resetLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        resetLabel.isEnabled = false
        menu.addItem(resetLabel)

        todayLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        todayLabel.isEnabled = false
        menu.addItem(todayLabel)

        let usageSection = NSMenuItem(title: "Last 7 days", action: nil, keyEquivalent: "")
        usageSection.isEnabled = false
        menu.addItem(usageSection)

        for _ in 0..<7 {
            let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
            item.isEnabled = false
            usageBarItems.append(item)
            menu.addItem(item)
        }

        menu.addItem(.separator())

        modelLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        modelLabel.isEnabled = false
        menu.addItem(modelLabel)

        menu.addItem(.separator())

        let catalogSection = NSMenuItem(title: "Catalog", action: nil, keyEquivalent: "")
        catalogSection.isEnabled = false
        menu.addItem(catalogSection)

        catalogLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        catalogLabel.isEnabled = false
        menu.addItem(catalogLabel)

        refreshCatalogItem = NSMenuItem(title: "Refresh Catalog", action: #selector(refreshCatalog), keyEquivalent: "")
        refreshCatalogItem.target = self
        menu.addItem(refreshCatalogItem)

        menu.addItem(.separator())

        let configSection = NSMenuItem(title: "Config", action: nil, keyEquivalent: "")
        configSection.isEnabled = false
        menu.addItem(configSection)

        configLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        configLabel.isEnabled = false
        menu.addItem(configLabel)

        configToggleItem = NSMenuItem(title: "Enable Config", action: #selector(toggleConfig), keyEquivalent: "")
        configToggleItem.target = self
        menu.addItem(configToggleItem)

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
        let serverState = proxy.serverState
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
        if let upstreamLabel {
            upstreamLabel.title = "Upstream \(Self.displayUpstream(serverState?.upstream))"
        }
        if let quotaLabel {
            quotaLabel.title = Self.quotaText(serverState?.quota)
        }
        if let resetLabel {
            let text = Self.resetText(serverState?.quota?.resetAt)
            resetLabel.title = text ?? ""
            resetLabel.isHidden = text == nil
        }
        if let todayLabel {
            todayLabel.title = Self.todayText(serverState?.usage)
        }
        if let modelLabel {
            modelLabel.title = "Model \(serverState?.model ?? "—")"
        }
        if let catalogLabel {
            if let count = proxy.catalogCount {
                catalogLabel.title = count == 1 ? "1 model" : "\(count) models"
            } else {
                catalogLabel.title = "—"
            }
        }
        if let refreshCatalogItem {
            refreshCatalogItem.isEnabled = state.isRunning && !proxy.isCLIRunning
        }
        if let configLabel {
            switch proxy.configEnabled {
            case .some(true):
                configLabel.title = "Config enabled"
            case .some(false):
                configLabel.title = "Config disabled"
            case .none:
                configLabel.title = "Config —"
            }
        }
        if let configToggleItem {
            configToggleItem.title = proxy.configEnabled == true ? "Disable Config" : "Enable Config"
            configToggleItem.isEnabled = proxy.configEnabled != nil && !proxy.isCLIRunning
        }
        updateUsageBars(serverState?.usage)
        if let toggleItem {
            toggleItem.title = state.isRunning ? "Stop Proxy" : "Start Proxy"
            toggleItem.isEnabled = !state.isStarting
        }
    }

    private func updateUsageBars(_ usage: UsageSummary?) {
        guard let usage else {
            for item in usageBarItems {
                item.title = "—"
            }
            return
        }
        let maxTokens = usage.last7d.map(\.tokens).max() ?? 0
        for (item, day) in zip(usageBarItems, usage.last7d) {
            let weekday = Self.weekdayName(for: day.date)
            var bar = ""
            if day.tokens > 0 {
                let count = max(1, Int(ceil(Double(day.tokens) / Double(max(1, maxTokens)) * 8)))
                bar = String(repeating: "█", count: count)
            }
            item.title = "\(weekday) \(bar) \(Self.formatTokens(day.tokens))"
        }
    }

    private static func displayUpstream(_ raw: String?) -> String {
        guard let raw, !raw.isEmpty else { return "—" }
        var value = raw
        for prefix in ["https://", "http://"] where value.hasPrefix(prefix) {
            value.removeFirst(prefix.count)
        }
        return value
    }

    private static func quotaText(_ quota: QuotaSnapshot?) -> String {
        guard let quota else { return "Quota n/a" }
        if let limit = quota.limit {
            return "Quota \(quota.remaining) / \(limit) · \(quota.provider)"
        }
        return "Quota \(quota.remaining) · \(quota.provider)"
    }

    private static func todayText(_ usage: UsageSummary?) -> String {
        guard let usage else { return "Today —" }
        return "Today \(usage.todayTurns) turns · \(formatTokens(usage.todayTokens)) tok"
    }

    private static func resetText(_ resetAt: String?) -> String? {
        guard let resetAt else { return nil }
        let date = resetFormatters.compactMap { $0.date(from: resetAt) }.first
        guard let date else { return nil }
        let seconds = max(0, Int(date.timeIntervalSinceNow))
        let hours = seconds / 3600
        let minutes = (seconds % 3600) / 60
        if hours > 0 { return "Reset in \(hours)h \(minutes)m" }
        if minutes > 0 { return "Reset in \(minutes)m" }
        return "Resets now"
    }

    private static func weekdayName(for date: String) -> String {
        guard let day = dayFormatter.date(from: date) else { return date }
        return weekdayFormatter.string(from: day)
    }

    private static func formatTokens(_ n: Int) -> String {
        if n >= 1_000_000 { return String(format: "%.1fM", Double(n) / 1_000_000) }
        if n >= 1_000 { return String(format: "%.1fk", Double(n) / 1_000) }
        return "\(n)"
    }

    @objc private func toggleProxy() {
        if proxy.state.isRunning {
            proxy.stop()
        } else {
            proxy.start()
        }
        refresh()
    }

    @objc private func refreshCatalog() {
        proxy.refreshCatalog { [weak self] result in
            self?.refresh()
            guard let message = result.message else { return }
            if result.succeeded {
                self?.proxy.presentError("Catalog refreshed, but \(message)")
            } else {
                self?.proxy.presentError(message)
            }
        }
    }

    @objc private func toggleConfig() {
        let enabled = proxy.configEnabled != true
        proxy.setConfigEnabled(enabled) { [weak self] result in
            self?.refresh()
            if !result.succeeded, let message = result.message {
                self?.proxy.presentError(message)
            }
        }
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
