import Foundation

/// One /state response from the proxy (plan 013 contract).
struct ServerState: Decodable, Equatable {
    let status: String
    let port: Int
    let upstream: String
    let quota: QuotaSnapshot?
    let usage: UsageSummary
    let model: String
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
