import AppKit
import Darwin
import Foundation

struct ProxyState: Equatable {
    var isRunning: Bool
    var isStarting: Bool
    var isHealthy: Bool
    var port: Int
}

/// Outcome of a one-shot proxy CLI run (native-capture, refresh-catalog,
/// config enable/disable). `message` carries the CLI log tail on failure so
/// the menu can surface why the command failed.
struct CLIResult {
    var succeeded: Bool
    var message: String?
}

final class ProxyController {
    var onStateChange: (() -> Void)?

    private(set) var state = ProxyState(isRunning: false, isStarting: false, isHealthy: false, port: 8787) {
        didSet { onStateChange?() }
    }
    private(set) var serverState: ServerState?
    private(set) var isCLIRunning = false
    private(set) var catalogCount: Int?
    private(set) var configEnabled: Bool?

    private var childPID: pid_t = -1
    private var healthURL: URL {
        URL(string: "http://127.0.0.1:\(state.port)/health")!
    }

    private static let proxySource = "git+https://github.com/kartikkabadi/opencode-go-proxy@v0.3.0"
    private static let cliLogName = "opencode-go-proxy-cli.log"
    private static let cliLogTailLimit = 600
    private static let configMarker = "# BEGIN opencode-go-proxy-managed"

    private var logDir: URL {
        let base = FileManager.default.homeDirectoryForCurrentUser
        return base.appendingPathComponent(".codex/logs", isDirectory: true)
    }

    func start() {
        guard childPID < 0, !state.isStarting else { return }
        state = ProxyState(isRunning: false, isStarting: true, isHealthy: false, port: state.port)

        // Single-port guard: if another process already owns the port, refuse
        // to spawn a second one — except the legacy launchd agent this app
        // replaced, which is migrated (unloaded) automatically once.
        if portIsInUse() {
            if oldLaunchdJobLoaded() {
                unloadOldLaunchdJob()
                Thread.sleep(forTimeInterval: 0.5)
            }
            if portIsInUse() {
                failStart("Port \(state.port) is already in use by another process (one proxy per port).")
                return
            }
        }

        do {
            try FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        } catch {
            failStart("Could not create log directory: \(error.localizedDescription)")
            return
        }

        let logPath = logDir.appendingPathComponent("opencode-go-proxy.log").path
        let errPath = logDir.appendingPathComponent("opencode-go-proxy.err").path
        let logFD = open(logPath, O_WRONLY | O_CREAT | O_APPEND, 0o644)
        let errFD = open(errPath, O_WRONLY | O_CREAT | O_APPEND, 0o644)
        guard logFD >= 0, errFD >= 0 else {
            // Close whatever opened successfully so the failure path leaks no fd.
            if logFD >= 0 { close(logFD) }
            if errFD >= 0 { close(errFD) }
            failStart("Could not open log files under \(logDir.path)")
            return
        }
        defer {
            close(logFD)
            close(errFD)
        }

        guard let uvx = findUVX() else {
            failStart("uvx not found. Install uv (https://docs.astral.sh/uv) or set uvx in PATH.")
            return
        }

        let argv: [String] = [
            uvx,
            "--from", Self.proxySource,
            "opencode-go-proxy",
            "--bind", "127.0.0.1",
            "--port", "\(state.port)",
            "--chat-base-url", "https://opencode.ai/zen/go/v1",
        ]
        let pid = spawnInGroup(argv: argv, stdoutFD: logFD, stderrFD: errFD,
                               env: childEnvironment())
        guard pid > 0 else {
            failStart("Could not start proxy (posix_spawn failed).")
            return
        }

        childPID = pid
        state = ProxyState(isRunning: true, isStarting: false, isHealthy: false, port: state.port)
        monitorChild(pid)
        refreshHealth()
        refreshState()
        refreshCatalogCount()
    }

    func stop() {
        guard childPID > 0 else { return }
        let pid = childPID
        childPID = -1
        kill(-pid, SIGTERM)
        // Grace period, then force-kill the process group if it survives.
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
            if kill(-pid, 0) == 0 {
                kill(-pid, SIGKILL)
            }
        }
        state = ProxyState(isRunning: false, isStarting: false, isHealthy: false, port: state.port)
        serverState = nil
        catalogCount = nil
    }

    /// Fetch the /state contract (quota card, usage bars, provider row). Only
    /// meaningful while the child process is alive; failures keep the last
    /// good state, and stop() clears it so the menu never shows stale numbers.
    func refreshState() {
        let pid = childPID
        guard pid > 0 else { return }
        StateFetcher.fetch(port: state.port) { [weak self] serverState in
            DispatchQueue.main.async {
                guard let self, self.childPID == pid else { return }
                // A transient fetch failure must not clear the last good state;
                // stop() clears serverState explicitly when the child exits.
                guard let serverState else { return }
                if self.serverState != serverState {
                    self.serverState = serverState
                    self.onStateChange?()
                }
            }
        }
    }

    func refreshHealth() {
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            let healthy = (response as? HTTPURLResponse)?.statusCode == 200 && data != nil
            DispatchQueue.main.async {
                guard let self else { return }
                let processAlive = self.childPID > 0
                let next = ProxyState(isRunning: processAlive, isStarting: false,
                                      isHealthy: processAlive && healthy, port: self.state.port)
                if next != self.state {
                    self.state = next
                }
            }
        }.resume()
    }

    /// GET /v1/models and count the entries; nil keeps the last good count.
    func refreshCatalogCount() {
        let pid = childPID
        guard pid > 0 else { return }
        CatalogFetcher.fetchCount(port: state.port) { [weak self] count in
            DispatchQueue.main.async {
                guard let self, self.childPID == pid, let count else { return }
                if self.catalogCount != count {
                    self.catalogCount = count
                    self.onStateChange?()
                }
            }
        }
    }

    /// Scan ~/.codex/config.toml for the managed-block marker; nil when the
    /// file is unreadable so the menu can show "n/a" instead of guessing.
    func refreshConfigStatus() {
        DispatchQueue.global().async { [weak self] in
            let enabled = Self.readConfigMarker()
            DispatchQueue.main.async {
                guard let self, enabled != self.configEnabled else { return }
                self.configEnabled = enabled
                self.onStateChange?()
            }
        }
    }

    /// Run `opencode-go-proxy config enable|disable` through the uvx child.
    func setConfigEnabled(_ enabled: Bool, completion: @escaping (CLIResult) -> Void) {
        guard !isCLIRunning else {
            completion(CLIResult(succeeded: false, message: "Another CLI action is still running."))
            return
        }
        isCLIRunning = true
        onStateChange?()
        runCLIOnce(arguments: ["config", enabled ? "enable" : "disable"]) { [weak self] result in
            guard let self else { return }
            self.isCLIRunning = false
            self.onStateChange?()
            self.refreshConfigStatus()
            completion(result)
        }
    }

    /// Refresh the runtime catalog the proxy serves: one CLI run that
    /// re-captures native models and force-renders the state-dir catalogs.
    func refreshCatalog(completion: @escaping (CLIResult) -> Void) {
        guard !isCLIRunning else {
            completion(CLIResult(succeeded: false, message: "Another CLI action is still running."))
            return
        }
        isCLIRunning = true
        onStateChange?()
        runCLIOnce(arguments: ["refresh-runtime"]) { [weak self] result in
            guard let self else { return }
            self.isCLIRunning = false
            self.onStateChange?()
            self.refreshCatalogCount()
            completion(result)
        }
    }

    func openLogs() {
        NSWorkspace.shared.open(logDir)
    }

    func revealLogFile() {
        NSWorkspace.shared.activateFileViewerSelecting([logDir.appendingPathComponent("opencode-go-proxy.log")])
    }

    func copyPort() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString("\(state.port)", forType: .string)
    }

    // MARK: - Private

    /// True when some process is already listening on 127.0.0.1:<port>.
    /// Used by the single-port guard so the menu bar never spawns a second
    /// proxy that would fail to bind (or fight a launchd service for the port).
    private func portIsInUse() -> Bool {
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else { return false }
        defer { close(sock) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(state.port).bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let rc = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                connect(sock, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return rc == 0
    }

    /// True when the legacy launchd agent (pre-0.3.0) is still loaded.
    private func oldLaunchdJobLoaded() -> Bool {
        launchctl(["print", "gui/\(getuid())/com.opencode.go.proxy"]) == 0
    }

    /// Unload the legacy launchd agent so the menu bar can own the port.
    private func unloadOldLaunchdJob() {
        _ = launchctl(["bootout", "gui/\(getuid())/com.opencode.go.proxy"])
    }

    /// Run launchctl; returns its exit status, or -1 when it cannot run.
    private func launchctl(_ arguments: [String]) -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = arguments
        let sink = Pipe()
        process.standardOutput = sink
        process.standardError = sink
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus
        } catch {
            return -1
        }
    }

    private func failStart(_ message: String) {
        state = ProxyState(isRunning: false, isStarting: false, isHealthy: false, port: state.port)
        presentError(message)
    }

    /// Spawn one short-lived proxy CLI command (uvx child, same pattern as the
    /// long-lived proxy) and report success plus the CLI log tail on failure.
    /// Does not touch isCLIRunning; callers own the busy flag for a sequence.
    private func runCLIOnce(arguments: [String], completion: @escaping (CLIResult) -> Void) {
        guard let uvx = findUVX() else {
            completion(CLIResult(succeeded: false,
                                 message: "uvx not found. Install uv (https://docs.astral.sh/uv) or set uvx in PATH."))
            return
        }
        do {
            try FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        } catch {
            completion(CLIResult(succeeded: false, message: "Could not create log directory: \(error.localizedDescription)"))
            return
        }

        let logPath = logDir.appendingPathComponent(Self.cliLogName).path
        let logFD = open(logPath, O_WRONLY | O_CREAT | O_APPEND, 0o644)
        guard logFD >= 0 else {
            completion(CLIResult(succeeded: false, message: "Could not open CLI log at \(logPath)"))
            return
        }
        defer { close(logFD) }

        let argv = [uvx, "--from", Self.proxySource, "opencode-go-proxy"] + arguments
        let pid = spawnInGroup(argv: argv, stdoutFD: logFD, stderrFD: logFD,
                               env: childEnvironment(), cwd: stateDirectoryIfPresent())
        guard pid > 0 else {
            completion(CLIResult(succeeded: false, message: "Could not start the proxy CLI (posix_spawn failed)."))
            return
        }

        DispatchQueue.global().async {
            let succeeded = Self.waitForExit(pid)
            let tail = Self.tailOfFile(at: logPath, limit: Self.cliLogTailLimit)
            DispatchQueue.main.async {
                completion(CLIResult(succeeded: succeeded, message: tail))
            }
        }
    }

    private static func waitForExit(_ pid: pid_t) -> Bool {
        var status: Int32 = 0
        guard waitpid(pid, &status, 0) >= 0 else { return false }
        // wait(2) macros (WIFEXITED/WEXITSTATUS) are not importable into Swift;
        // decode the raw status: low 7 bits signal termination, high 8 exit code.
        return status & 0x7f == 0 && (status >> 8) & 0xff == 0
    }

    private static func tailOfFile(at path: String, limit: Int) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: URL(fileURLWithPath: path)) else { return nil }
        defer { try? handle.close() }
        let size = Int64((try? handle.seekToEnd()) ?? 0)
        let offset = max(0, size - Int64(limit))
        try? handle.seek(toOffset: UInt64(offset))
        guard let data = try? handle.readToEnd(),
              let text = String(data: data, encoding: .utf8) else { return nil }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// Run CLI one-shots from the proxy state dir when it exists; the CLI
    /// resolves its own state dir either way, so a missing dir just means no
    /// explicit cwd.
    private func stateDirectoryIfPresent() -> String? {
        let environment = ProcessInfo.processInfo.environment
        let path: String
        if let override = environment["OPENCODE_GO_PROXY_STATE_DIR"], !override.isEmpty {
            path = override
        } else {
            path = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".codex/opencode-go-proxy").path
        }
        return FileManager.default.fileExists(atPath: path) ? path : nil
    }

    private static func readConfigMarker() -> Bool? {
        let configPath = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".codex/config.toml")
        guard let contents = try? String(contentsOf: configPath, encoding: .utf8) else { return nil }
        return contents.contains(configMarker)
    }

    private func monitorChild(_ pid: pid_t) {
        DispatchQueue.global().async {
            var status: Int32 = 0
            waitpid(pid, &status, 0)
            DispatchQueue.main.async {
                guard self.childPID == pid else { return }
                self.childPID = -1
                self.serverState = nil
                self.catalogCount = nil
                self.state = ProxyState(isRunning: false, isStarting: false,
                                        isHealthy: false, port: self.state.port)
            }
        }
    }

    private func spawnInGroup(argv: [String], stdoutFD: Int32, stderrFD: Int32, env: [String], cwd: String? = nil) -> pid_t {
        var cArgs = argv.map { strdup($0) }
        cArgs.append(nil)
        defer { cArgs.forEach { free($0) } }

        var fileActions: posix_spawn_file_actions_t?
        guard posix_spawn_file_actions_init(&fileActions) == 0 else { return -1 }
        defer { posix_spawn_file_actions_destroy(&fileActions) }
        posix_spawn_file_actions_adddup2(&fileActions, stdoutFD, STDOUT_FILENO)
        posix_spawn_file_actions_adddup2(&fileActions, stderrFD, STDERR_FILENO)
        if let cwd, posix_spawn_file_actions_addchdir_np(&fileActions, cwd) != 0 {
            return -1
        }

        var attributes: posix_spawnattr_t?
        guard posix_spawnattr_init(&attributes) == 0 else { return -1 }
        defer { posix_spawnattr_destroy(&attributes) }
        posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP))

        var pid: pid_t = 0
        let envp = env.map { strdup($0) }
        defer { envp.forEach { free($0) } }
        let result = posix_spawn(&pid, cArgs[0], &fileActions, &attributes, &cArgs, envp)
        return result == 0 ? pid : -1
    }

    private func childEnvironment() -> [String] {
        // Menu bar apps launch without the shell PATH; give the child the same
        // PATH shape as the launchd plist plus a stable HOME.
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let processInfo = ProcessInfo.processInfo
        var vars = processInfo.environment
        vars["HOME"] = home
        vars["PATH"] = "\(home)/.local/bin:/usr/local/bin:/usr/bin:/bin"
        vars["PYTHONUNBUFFERED"] = "1"
        return vars.map { "\($0.key)=\($0.value)" }
    }

    private func findUVX() -> String? {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            "\(home)/.local/bin/uvx",
            "/opt/homebrew/bin/uvx",
            "/usr/local/bin/uvx",
        ]
        for candidate in candidates where FileManager.default.isExecutableFile(atPath: candidate) {
            return candidate
        }
        return nil
    }

    func presentError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "OpenCode Go Proxy"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}
