import Foundation

public enum DesktopPage: Hashable {
    case execution, routing, usage
}

public struct RoutingCandidate: Decodable, Sendable {
    public let profile: String
    public let profileOrigin: String?
    public let driver: String?
    public let harness: String?
    public let model: String?
    public let tier: String?
    public let legacyClass: String?
    public let effort: String?
    public let nativeEffort: String?
    public let error: String?
}

public struct RoutingGroup: Decodable, Sendable {
    public let origin: String
    public let candidates: [RoutingCandidate]
}

public struct RoutingSelection: Decodable, Sendable {
    public struct Controller: Decodable, Sendable {
        public let origin: String
        public let candidate: RoutingCandidate
    }

    public struct Planner: Decodable, Sendable {
        public let depth: String
        public let depthOrigin: String
        public let depths: [String: RoutingGroup]
    }

    public let controller: Controller
    public let planner: Planner
    public let worker: RoutingGroup
    public let verifier: RoutingGroup
}

public enum RoutingValue: Codable, Equatable, Sendable {
    case text(String)
    case profiles([String])

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if let text = try? value.decode(String.self) {
            self = .text(text)
        } else {
            self = .profiles(try value.decode([String].self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .text(let text): try container.encode(text)
        case .profiles(let names): try container.encode(names)
        }
    }
}

public struct RoutingEntry: Decodable, Sendable {
    public let key: String
    public let value: RoutingValue
    public let origin: String
}

public struct RoutingRequest: Encodable, Sendable {
    public let revision: String
    public let changes: [String: RoutingValue]

    public init(revision: String, changes: [String: RoutingValue]) {
        self.revision = revision
        self.changes = changes
    }
}

public struct RoutingSnapshot: Decodable, Sendable {
    public let contract: Int
    public let orxVersion: String
    public let scope: String
    public let projectRoot: String?
    public let targetPath: String
    public let revision: String
    public let availableProfiles: [String]
    public let values: [RoutingEntry]
    public let modelSelection: RoutingSelection
}

public enum ORXRoutingError: LocalizedError {
    case missing(String)
    case incompatible(String)
    case refused(String)
    case timeout

    public var errorDescription: String? {
        switch self {
        case .missing(let path): return "找不到可运行的 ORX。\(path)"
        case .incompatible(let path): return "此 ORX 版本不支持桌面路由配置。\(path)"
        case .refused(let message): return message
        case .timeout: return "ORX 配置命令超时。"
        }
    }
}

public struct ORXRoutingClient: Sendable {
    public let executable: URL

    public init(executable: URL) {
        self.executable = executable
    }

    public static func resolveExecutable(
        override: String,
        home: URL = FileManager.default.homeDirectoryForCurrentUser,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) throws -> URL {
        let fm = FileManager.default
        let chosen = override.trimmingCharacters(in: .whitespacesAndNewlines)
        if !chosen.isEmpty {
            let path = (chosen as NSString).expandingTildeInPath
            guard path.hasPrefix("/"), fm.isExecutableFile(atPath: path) else {
                throw ORXRoutingError.missing(chosen)
            }
            return URL(fileURLWithPath: path)
        }
        let directories = [home.appendingPathComponent(".local/bin").path,
                           home.appendingPathComponent("bin").path,
                           "/opt/homebrew/bin", "/usr/local/bin"]
            + (environment["PATH"] ?? "").split(separator: ":")
                .filter { $0.hasPrefix("/") }.map(String.init)
        for directory in directories {
            let executable = URL(fileURLWithPath: directory).appendingPathComponent("orx")
            if fm.isExecutableFile(atPath: executable.path) {
                return executable
            }
        }
        throw ORXRoutingError.missing("可在此页指定 ORX 可执行文件的绝对路径。")
    }

    public func read(project: String?) async throws -> RoutingSnapshot {
        try await execute(project: project, request: nil, apply: false)
    }

    public func preview(project: String?, request: RoutingRequest) async throws -> RoutingSnapshot {
        try await execute(project: project, request: request, apply: false)
    }

    public func save(project: String?, request: RoutingRequest) async throws -> RoutingSnapshot {
        try await execute(project: project, request: request, apply: true)
    }

    private func execute(project: String?, request: RoutingRequest?,
                         apply: Bool) async throws -> RoutingSnapshot {
        try await Task.detached(priority: .userInitiated) {
            try query(project: project, request: request, apply: apply)
        }.value
    }

    private func query(project: String?, request: RoutingRequest?,
                       apply: Bool) throws -> RoutingSnapshot {
        let fm = FileManager.default
        let scratch = try fm.url(for: .itemReplacementDirectory, in: .userDomainMask,
                                 appropriateFor: fm.temporaryDirectory, create: true)
        defer { try? fm.removeItem(at: scratch) }
        let output = scratch.appendingPathComponent("response.json")
        try Data().write(to: output)
        let stdout = try FileHandle(forWritingTo: output)
        defer { try? stdout.close() }
        let process = Process()
        process.executableURL = executable
        process.currentDirectoryURL = URL(fileURLWithPath: "/")
        process.arguments = ["config", "routing", "--json", "--scope", project == nil ? "user" : "project"]
        if let project {
            process.arguments?.append(contentsOf: ["--project-root", project])
        }
        if let request {
            let file = scratch.appendingPathComponent("request.json")
            try JSONEncoder().encode(request).write(to: file, options: .atomic)
            process.arguments?.append(contentsOf: ["--request", file.path])
        }
        if apply { process.arguments?.append("--apply") }
        process.standardOutput = stdout
        process.standardError = FileHandle.nullDevice
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in finished.signal() }
        try process.run()
        guard finished.wait(timeout: .now() + 10) == .success else {
            process.terminate()
            throw ORXRoutingError.timeout
        }
        let data = try Data(contentsOf: output)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        if process.terminationStatus == 0,
           let snapshot = try? decoder.decode(RoutingSnapshot.self, from: data),
           snapshot.contract == 1 {
            return snapshot
        }
        struct Refusal: Decodable {
            let error: String
            let errors: [String]?
        }
        if let refusal = try? decoder.decode(Refusal.self, from: data) {
            throw ORXRoutingError.refused(([refusal.error] + (refusal.errors ?? []))
                .joined(separator: "\n"))
        }
        throw ORXRoutingError.incompatible(executable.path)
    }
}
