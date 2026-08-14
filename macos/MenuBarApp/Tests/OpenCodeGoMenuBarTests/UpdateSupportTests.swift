import XCTest
@testable import OpenCodeGoMenuBar

/// Covers the update-support contract: the lenient /state decode (old and
/// new payloads), the /version payload decode, and the pin-override logic
/// (UserDefaults "proxySource" beats the compiled pin).
final class UpdateSupportTests: XCTestCase {
    private let suiteName = "OpenCodeGoMenuBarTests-UpdateSupport"

    override func tearDown() {
        UserDefaults(suiteName: suiteName)?.removePersistentDomain(forName: suiteName)
        super.tearDown()
    }

    private func freshDefaults() -> UserDefaults {
        let suite = UserDefaults(suiteName: suiteName)!
        suite.removePersistentDomain(forName: suiteName)
        return suite
    }

    private func decodeState(_ json: String) throws -> ServerState {
        try JSONDecoder().decode(ServerState.self, from: Data(json.utf8))
    }

    // MARK: - Lenient /state decode

    func testOldStateWithoutVersionFieldsStillDecodes() throws {
        let oldState = """
        {"status":"ok","port":8787,"upstream":"https://opencode.ai/zen/go/v1",
         "quota":{"provider":"zen","remaining":100,"limit":null,"resetAt":null},
         "usage":{"todayTurns":3,"todayTokens":1200,
                  "last7d":[{"date":"2026-08-08","tokens":10}],
                  "go":null,"goLimits":null,"zen":null},
         "model":"gpt-5.2-codex"}
        """
        let decoded = try decodeState(oldState)
        XCTAssertEqual(decoded.status, "ok")
        XCTAssertEqual(decoded.port, 8787)
        XCTAssertEqual(decoded.upstream, "https://opencode.ai/zen/go/v1")
        XCTAssertEqual(decoded.usage.todayTurns, 3)
        XCTAssertNil(decoded.version)
        XCTAssertNil(decoded.update)
    }

    func testNewStateWithVersionFieldsDecodes() throws {
        let newState = """
        {"status":"ok","port":8787,"upstream":"https://opencode.ai/zen/go/v1",
         "quota":null,
         "usage":{"todayTurns":0,"todayTokens":0,"last7d":[]},
         "model":"gpt-5.2-codex",
         "version":"0.4.7",
         "update":{"available":true,"latest":"0.4.8",
                   "release_url":"https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.8",
                   "checked_at":"2026-08-14T12:00:00Z","error":null}}
        """
        let decoded = try decodeState(newState)
        XCTAssertEqual(decoded.version, "0.4.7")
        XCTAssertEqual(decoded.update?.available, true)
        XCTAssertEqual(decoded.update?.latest, "0.4.8")
        XCTAssertEqual(decoded.update?.releaseUrl,
                       "https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.8")
        XCTAssertEqual(decoded.update?.checkedAt, "2026-08-14T12:00:00Z")
        XCTAssertNil(decoded.update?.error)
    }

    func testVersionPayloadDecodes() throws {
        let payload = """
        {"version":"0.4.7","git_commit":"16636a3",
         "update":{"available":false,"latest":null,"release_url":null,
                   "checked_at":null,"error":null}}
        """
        let decoded = try JSONDecoder().decode(VersionInfo.self, from: Data(payload.utf8))
        XCTAssertEqual(decoded.version, "0.4.7")
        XCTAssertEqual(decoded.gitCommit, "16636a3")
        XCTAssertEqual(decoded.update?.available, false)
        XCTAssertNil(decoded.update?.latest)
    }

    // MARK: - Pin override

    func testDefaultsOverrideBeatsCompiledPin() {
        let defaults = freshDefaults()
        XCTAssertEqual(ProxySourceResolver.resolve(defaults: defaults, fallbackProxySource: ProxySourceResolver.compiledSource),
                       ProxySourceResolver.compiledSource)
        defaults.set("git+https://github.com/kartikkabadi/opencode-go-proxy@v0.4.8",
                     forKey: ProxySourceResolver.defaultsKey)
        XCTAssertEqual(ProxySourceResolver.resolve(defaults: defaults, fallbackProxySource: ProxySourceResolver.compiledSource),
                       "git+https://github.com/kartikkabadi/opencode-go-proxy@v0.4.8")
    }

    func testEmptyDefaultsValueFallsBackToCompiledPin() {
        let defaults = freshDefaults()
        defaults.set("", forKey: ProxySourceResolver.defaultsKey)
        XCTAssertEqual(ProxySourceResolver.resolve(defaults: defaults, fallbackProxySource: ProxySourceResolver.compiledSource),
                       ProxySourceResolver.compiledSource)
    }

    func testPinBuilderAndTagParser() {
        XCTAssertEqual(ProxySourceResolver.source(pinning: "0.4.8"),
                       "git+https://github.com/kartikkabadi/opencode-go-proxy@v0.4.8")
        XCTAssertEqual(ProxySourceResolver.source(pinning: "v0.4.8"),
                       "git+https://github.com/kartikkabadi/opencode-go-proxy@v0.4.8")
        XCTAssertEqual(ProxySourceResolver.pinTag(from: "git+https://github.com/kartikkabadi/opencode-go-proxy@v0.4.8"),
                       "v0.4.8")
        XCTAssertNil(ProxySourceResolver.pinTag(from: "git+https://github.com/kartikkabadi/opencode-go-proxy"))
    }
}
