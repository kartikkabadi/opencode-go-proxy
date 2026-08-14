import AppKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    /// Build version of this menu bar app. The proxy auto-updates itself,
    /// but this binary is a manual rebuild; bumped at release time in this
    /// one place (shown as the version row's tooltip).
    private static let appVersion = "menu bar v0.4.8"

    private var statusItem: NSStatusItem!
    private var proxy = ProxyController()
    private var statusLabel: NSMenuItem!
    private var versionLabel: NSMenuItem!
    private var updateAvailableItem: NSMenuItem!
    private var checkForUpdatesItem: NSMenuItem!
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
    private var usageSectionItem: NSMenuItem!
    private var goLimitsLabel: NSMenuItem!
    private var goUsageLabel: NSMenuItem!
    private var zenTodayLabel: NSMenuItem!
    private var zenBarItems: [NSMenuItem] = []
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

        versionLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        versionLabel.isEnabled = false
        menu.addItem(versionLabel)

        updateAvailableItem = NSMenuItem(title: "", action: #selector(applyUpdate), keyEquivalent: "")
        updateAvailableItem.target = self
        updateAvailableItem.isHidden = true
        menu.addItem(updateAvailableItem)

        checkForUpdatesItem = NSMenuItem(title: "Check for Updates", action: #selector(checkForUpdates), keyEquivalent: "")
        checkForUpdatesItem.target = self
        menu.addItem(checkForUpdatesItem)

        menu.addItem(.separator())

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

        usageSectionItem = NSMenuItem(title: "Usage", action: nil, keyEquivalent: "")
        usageSectionItem.isEnabled = false
        menu.addItem(usageSectionItem)

        goLimitsLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        goLimitsLabel.isEnabled = false
        menu.addItem(goLimitsLabel)

        goUsageLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        goUsageLabel.isEnabled = false
        menu.addItem(goUsageLabel)

        zenTodayLabel = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        zenTodayLabel.isEnabled = false
        menu.addItem(zenTodayLabel)

        for _ in 0..<7 {
            let item = NSMenuItem(title: "", action: nil, keyEquivalent: "")
            item.isEnabled = false
            zenBarItems.append(item)
            menu.addItem(item)
        }

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
        if let versionLabel {
            versionLabel.title = Self.versionRowTitle(runningVersion: proxy.runningVersion, pin: proxy.activePin)
            versionLabel.toolTip = Self.appVersion
        }
        if let updateAvailableItem {
            let update = proxy.updateInfo
            let latest = update?.latest
            let available = update?.available == true && latest != nil
            updateAvailableItem.title = latest.map { "Update Available: \(Self.versionText($0))" } ?? ""
            updateAvailableItem.isHidden = !available
            updateAvailableItem.isEnabled = state.isRunning
        }
        if let checkForUpdatesItem {
            checkForUpdatesItem.isEnabled = state.isRunning
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
        let usage = serverState?.usage
        if let usageSectionItem {
            usageSectionItem.isHidden = usage?.go == nil && usage?.goLimits == nil && usage?.zen == nil
        }
        if let goLimitsLabel {
            goLimitsLabel.title = Self.goLimitsText(usage?.goLimits) ?? ""
            goLimitsLabel.isHidden = usage?.goLimits == nil
        }
        if let goUsageLabel {
            goUsageLabel.title = Self.goUsageText(usage?.go) ?? ""
            goUsageLabel.isHidden = usage?.go == nil
        }
        if let zenTodayLabel {
            zenTodayLabel.title = Self.zenTodayText(usage?.zen) ?? ""
            zenTodayLabel.isHidden = usage?.zen == nil
        }
        updateZenBars(usage?.zen)
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

    /// Zen 7-day bars. Zen usage carries no per-day dates, so weekdays are
    /// computed relative to today (oldest-first counts, today last).
    private func updateZenBars(_ zen: ZenUsage?) {
        guard let zen else {
            for item in zenBarItems {
                item.title = "—"
            }
            return
        }
        let days = zen.last7d
        let maxTokens = days.max() ?? 0
        for (index, item) in zenBarItems.enumerated() {
            let offset = days.count - 1 - index
            guard offset >= 0, offset < days.count else {
                item.title = "—"
                continue
            }
            let tokens = days[offset]
            let date = Calendar.current.date(byAdding: .day, value: -offset, to: Date()) ?? Date()
            let weekday = Self.weekdayFormatter.string(from: date)
            var bar = ""
            if tokens > 0 {
                let count = max(1, Int(ceil(Double(tokens) / Double(max(1, maxTokens)) * 8)))
                bar = String(repeating: "█", count: count)
            }
            item.title = "\(weekday) \(bar) \(Self.formatTokens(tokens))"
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

    /// "0.4.8" → "v0.4.8"; keeps an already-prefixed version as-is.
    private static func versionText(_ version: String) -> String {
        version.hasPrefix("v") ? version : "v" + version
    }

    /// "proxy v0.4.8 (pin v0.4.8)" — the child's running version plus the
    /// tag this app launches it with.
    private static func versionRowTitle(runningVersion: String?, pin: String?) -> String {
        guard let runningVersion else { return "proxy —" }
        let pinText = pin.map { "pin \($0)" } ?? "unpinned"
        return "proxy \(versionText(runningVersion)) (\(pinText))"
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

    private static func goLimitsText(_ limits: GoLimits?) -> String? {
        guard let limits else { return nil }
        return "Go limits: $\(limits.monthlyDollars)/mo · $\(limits.weeklyDollars)/wk · $\(limits.rolling5hDollars)/5h"
    }

    private static func goUsageText(_ go: GoUsage?) -> String? {
        guard let go else { return nil }
        var parts: [String] = []
        if let rolling = go.rolling {
            parts.append(Self.windowText(rolling, label: "rolling"))
            if let reset = resetText(rolling.resetsAt) {
                parts.append(Self.lowercasedFirst(reset))
            }
        }
        if let weekly = go.weekly {
            parts.append(Self.windowText(weekly, label: "wk"))
        }
        if let monthly = go.monthly {
            parts.append(Self.windowText(monthly, label: "mo"))
        }
        return "Go usage: " + parts.joined(separator: " · ")
    }

    private static func windowText(_ window: GoWindow, label: String) -> String {
        var text = window.percent.map { "\($0)% \(label)" } ?? "\(label) —"
        if !window.status.isEmpty && window.status != "ok" {
            text += " (\(window.status))"
        }
        return text
    }

    private static func zenTodayText(_ zen: ZenUsage?) -> String? {
        guard let zen else { return nil }
        return "Zen today: \(zen.todayTurns) turns · \(formatTokens(zen.todayTokens)) tok"
    }

    private static func lowercasedFirst(_ text: String) -> String {
        guard let first = text.first else { return text }
        return first.lowercased() + text.dropFirst()
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

    @objc private func checkForUpdates() {
        // Force a fresh check (bypasses the proxy's TTL cache) and re-fetch
        // /state so its version/update piggyback converges immediately.
        proxy.refreshVersion(force: true)
        proxy.refreshState()
        refresh()
    }

    @objc private func applyUpdate() {
        let update = proxy.updateInfo
        guard let latest = update?.latest, update?.available == true else { return }
        let alert = NSAlert()
        alert.messageText = "Update proxy to \(Self.versionText(latest))?"
        alert.informativeText = "The proxy restarts."
        alert.alertStyle = .informational
        alert.addButton(withTitle: "Update")
        alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        proxy.applyUpdate(to: latest)
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
