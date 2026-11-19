import SwiftUI
import ORXAnalyticsCore

/// Routing configuration has its own availability and never gates engine queries.
struct RoutingView: View {
    @EnvironmentObject private var app: AppModel
    @AppStorage("orx.executable.path") private var savedExecutable = ""
    @State private var executable = ""
    @State private var snapshot: RoutingSnapshot?
    @State private var installedPath: String?
    @State private var error: String?
    @State private var loading = false

    private var projects: [String] {
        let reported = app.cachedOverview?.envelope.projectHealth.map(\.project) ?? []
        return Set(reported + [app.routingProject].compactMap { $0 }).sorted()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Picker("生效范围", selection: Binding(get: { app.routingProject }, set: app.openRouting)) {
                    Text("用户默认").tag(String?.none)
                    ForEach(projects, id: \.self) { project in
                        Text(project).tag(Optional(project))
                    }
                }
                Spacer()
                Button("重新载入") { Task { await load(project: app.routingProject) } }
                    .disabled(loading)
            }
            HStack {
                TextField("ORX 可执行文件路径", text: $executable,
                          prompt: Text("自动查找"))
                    .onSubmit { useExecutable() }
                Button("使用此路径") { useExecutable() }
                    .disabled(executable == savedExecutable)
            }
            if let installedPath, let snapshot {
                Text("ORX \(snapshot.orxVersion)  ·  \(installedPath)")
                    .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                Text("目标配置文件  \(snapshot.targetPath)")
                    .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            if let error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(.orange)
                    .textSelection(.enabled)
            }
            Divider()
            if let snapshot {
                configuration(snapshot.modelSelection)
            } else if loading {
                ProgressView("正在读取 ORX 配置").frame(maxHeight: .infinity)
            } else {
                Text("配置可用后将在此显示。").foregroundStyle(.secondary)
                Spacer()
            }
        }
        .padding(16)
        .onAppear { executable = savedExecutable }
        .task(id: app.routingProject) { await load(project: app.routingProject) }
    }

    private func useExecutable() {
        savedExecutable = executable
        Task { await load(project: app.routingProject) }
    }

    @MainActor
    private func load(project: String?) async {
        loading = true
        snapshot = nil
        error = nil
        defer { if project == app.routingProject { loading = false } }
        do {
            let url = try ORXRoutingClient.resolveExecutable(override: savedExecutable)
            let result = try await ORXRoutingClient(executable: url).read(project: project)
            guard !Task.isCancelled, project == app.routingProject else { return }
            installedPath = url.path
            snapshot = result
        } catch {
            guard !Task.isCancelled, project == app.routingProject else { return }
            self.error = error.localizedDescription
        }
    }

    private func configuration(_ selection: RoutingSelection) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                group("Controller", origin: selection.controller.origin,
                      candidates: [selection.controller.candidate])
                VStack(alignment: .leading, spacing: 10) {
                    Text("Planner").font(.headline)
                    Text("规划深度  \(selection.planner.depth)  ·  \(originLabel(selection.planner.depthOrigin))")
                        .font(.callout).foregroundStyle(.secondary)
                    ForEach(["light", "standard", "deep"], id: \.self) { depth in
                        if let group = selection.planner.depths[depth] {
                            self.group(depth, origin: group.origin, candidates: group.candidates)
                        }
                    }
                }
                Divider()
                group("Worker", origin: selection.worker.origin, candidates: selection.worker.candidates)
                Divider()
                group("Verifier", origin: selection.verifier.origin, candidates: selection.verifier.candidates)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func group(_ title: String, origin: String,
                       candidates: [RoutingCandidate]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title).font(.headline)
                Text(originLabel(origin)).font(.caption).foregroundStyle(.secondary)
            }
            if candidates.isEmpty {
                Text("未配置候选").foregroundStyle(.secondary)
            }
            ForEach(Array(candidates.enumerated()), id: \.offset) { index, candidate in
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(index + 1). \(candidate.profile)").font(.body.weight(.medium))
                    if let error = candidate.error {
                        Text(error).foregroundStyle(.orange)
                    } else {
                        Text([candidate.harness, candidate.model, candidate.tier ?? candidate.legacyClass,
                              candidate.effort.map { "推理 \($0)" }, candidate.nativeEffort]
                            .compactMap { $0 }.joined(separator: "  ·  "))
                            .foregroundStyle(.secondary)
                    }
                }
                .font(.callout)
                .textSelection(.enabled)
            }
        }
    }

    private func originLabel(_ origin: String) -> String {
        switch origin {
        case "project": return "项目覆盖"
        case "user": return "用户默认"
        case "default": return "内置默认"
        case "env": return "环境变量"
        default: return origin
        }
    }
}
