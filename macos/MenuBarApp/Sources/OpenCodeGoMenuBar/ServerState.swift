import Foundation

/// One /state response from the proxy (plan 013 contract).
struct ServerState: Decodable, Equatable {
    let status: String
    let port: Int
    let upstream: String
    let quota: QuotaSnapshot?
    let usage: UsageSummary
    let model: String
    /// Update-support additions (0.4.7). Optional so a pre-0.4.5 proxy whose
    /// /state omits these keys still decodes; the version row then falls
    /// back to a separate GET /version.
    let version: String?
    let update: UpdateInfo?
}

struct QuotaSnapshot: Decodable, Equatable {
    let provider: String
    let remaining: Int
    let limit: Int?
    let resetAt: String?
}

struct UsageSummary: Decodable, Equatable {
    let todayTurns: Int
    let todayTokens: Int
    let last7d: [UsageDay]
    /// Zen contract additions (0.4.0). Optional so a pre-0.4.0 proxy that
    /// omits these keys still decodes: synthesized Decodable treats a missing
    /// or null optional key as nil.
    let go: GoUsage?
    let goLimits: GoLimits?
    let zen: ZenUsage?
}

struct UsageDay: Decodable, Equatable {
    let date: String
    let tokens: Int
}

/// Go subscription windows from the Zen usage endpoint
/// ({"rolling": {...}, "weekly": {...}, "monthly": {...}}).
struct GoUsage: Decodable, Equatable {
    let rolling: GoWindow?
    let weekly: GoWindow?
    let monthly: GoWindow?
}

struct GoWindow: Decodable, Equatable {
    let status: String
    let percent: Int?
    let resetsAt: String?
}

/// Go plan dollar limits (fixed per the Zen contract).
struct GoLimits: Decodable, Equatable {
    let monthlyDollars: Int
    let weeklyDollars: Int
    let rolling5hDollars: Int
    let subscriptionMonthlyDollars: Int
}

/// Zen-metered usage: today's rollup plus seven plain token counts
/// (oldest first, today last).
struct ZenUsage: Decodable, Equatable {
    let todayTurns: Int
    let todayTokens: Int
    let last7d: [Int]
}

/// Update availability as reported by the proxy (updates.py contract:
/// version_payload()["update"] and build_state()["update"]). All fields
/// optional so partial or pre-contract payloads still decode.
struct UpdateInfo: Decodable, Equatable {
    let available: Bool?
    let latest: String?
    let releaseUrl: String?
    let checkedAt: String?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case available, latest, error
        case releaseUrl = "release_url"
        case checkedAt = "checked_at"
    }
}

/// GET /version payload: {"version", "git_commit", "update": {...}}.
struct VersionInfo: Decodable, Equatable {
    let version: String
    let gitCommit: String?
    let update: UpdateInfo?

    enum CodingKeys: String, CodingKey {
        case version, update
        case gitCommit = "git_commit"
    }
}

enum StateFetcher {
    /// GET /state on the local proxy; completion(nil) on any failure so the
    /// menu degrades to "n/a" rows instead of stale numbers.
    static func fetch(port: Int, completion: @escaping (ServerState?) -> Void) {
        let url = URL(string: "http://127.0.0.1:\(port)/state")!
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { data, response, _ in
            guard let data,
                  let response = response as? HTTPURLResponse,
                  response.statusCode == 200 else {
                completion(nil)
                return
            }
            completion(try? JSONDecoder().decode(ServerState.self, from: data))
        }.resume()
    }
}

/// GET /version on the local proxy (update-support contract); nil on any
/// failure so the menu keeps the last good version row. `force` hits
/// ?force=1 so the proxy bypasses its update-check TTL cache.
enum VersionFetcher {
    static func fetch(port: Int, force: Bool, completion: @escaping (VersionInfo?) -> Void) {
        var components = URLComponents(string: "http://127.0.0.1:\(port)/version")!
        if force {
            components.queryItems = [URLQueryItem(name: "force", value: "1")]
        }
        guard let url = components.url else {
            completion(nil)
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { data, response, _ in
            guard let data,
                  let response = response as? HTTPURLResponse,
                  response.statusCode == 200 else {
                completion(nil)
                return
            }
            completion(try? JSONDecoder().decode(VersionInfo.self, from: data))
        }.resume()
    }
}

/// GET /v1/models count on the local proxy; completion(nil) on any failure so
/// the menu shows "—" instead of a stale number.
enum CatalogFetcher {
    private struct ModelList: Decodable {
        let data: [ModelRef]

        struct ModelRef: Decodable {
            let id: String
        }
    }

    static func fetchCount(port: Int, completion: @escaping (Int?) -> Void) {
        let url = URL(string: "http://127.0.0.1:\(port)/v1/models")!
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { data, response, _ in
            guard let data,
                  let response = response as? HTTPURLResponse,
                  response.statusCode == 200,
                  let list = try? JSONDecoder().decode(ModelList.self, from: data) else {
                completion(nil)
                return
            }
            completion(list.data.count)
        }.resume()
    }
}
