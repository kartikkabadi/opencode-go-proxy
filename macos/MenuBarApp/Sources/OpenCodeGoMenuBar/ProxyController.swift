import AppKit
import Darwin
import Foundation

struct ProxyState: Equatable {
    var isRunning: Bool
    var isStarting: Bool
    var isHealthy: Bool
    var port: Int
}

final class ProxyController {
    var onStateChange: (() -> Void)?

    private(set) var state = ProxyState(isRunning: false, isStarting: false, isHealthy: false, port: 8787) {
        didSet { onStateChange?() }
    }

    private var childPID: pid_t = -1
    private var healthURL: URL {
        URL(string: "http://127.0.0.1:\(state.port)/health")!
    }

    private var logDir: URL {
        let base = FileManager.default.homeDirectoryForCurrentUser
        return base.appendingPathComponent(".codex/logs", isDirectory: true)
    }

    func start() {
        guard childPID < 0, !state.isStarting else { return }
        state = ProxyState(isRunning: false, isStarting: true, isHealthy: false, port: state.port)

        // Single-port guard: if another process already owns the port (the
        // launchd service, another proxy, anything else), refuse to spawn a
        // second one. One proxy per port; the menu bar is the control surface,
        // but it must not fight an existing process for the socket.
        if portIsInUse() {
            failStart("Port \(state.port) is already in use by another process. Stop the launchd service or the other proxy first (one proxy per port).")
            return
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
            "--from", "git+https://github.com/kartikkabadi/opencode-go-proxy",
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

    private func failStart(_ message: String) {
        state = ProxyState(isRunning: false, isStarting: false, isHealthy: false, port: state.port)
        presentError(message)
    }

    private func monitorChild(_ pid: pid_t) {
        DispatchQueue.global().async {
            var status: Int32 = 0
            waitpid(pid, &status, 0)
            DispatchQueue.main.async {
                guard self.childPID == pid else { return }
                self.childPID = -1
                self.state = ProxyState(isRunning: false, isStarting: false,
                                        isHealthy: false, port: self.state.port)
            }
        }
    }

    private func spawnInGroup(argv: [String], stdoutFD: Int32, stderrFD: Int32, env: [String]) -> pid_t {
        var cArgs = argv.map { strdup($0) }
        cArgs.append(nil)
        defer { cArgs.forEach { free($0) } }

        var fileActions: posix_spawn_file_actions_t?
        guard posix_spawn_file_actions_init(&fileActions) == 0 else { return -1 }
        defer { posix_spawn_file_actions_destroy(&fileActions) }
        posix_spawn_file_actions_adddup2(&fileActions, stdoutFD, STDOUT_FILENO)
        posix_spawn_file_actions_adddup2(&fileActions, stderrFD, STDERR_FILENO)

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

    private func presentError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "OpenCode Go Proxy"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}
