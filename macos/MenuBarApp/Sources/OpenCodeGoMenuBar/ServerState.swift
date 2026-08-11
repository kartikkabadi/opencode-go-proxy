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
}

struct UsageDay: Decodable, Equatable {
    let date: String
    let tokens: Int
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
