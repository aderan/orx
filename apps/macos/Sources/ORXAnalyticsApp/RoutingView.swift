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
    @State private var editing = false
    @State private var edits: [String: RoutingValue] = [:]
    @State private var preview: RoutingSnapshot?
    @State private var saving = false
    @State private var message: String?
    @State private var loadID = UUID()

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
                .disabled(saving)
                Spacer()
                Button("重新载入") { Task { await load(project: app.routingProject) } }
                    .disabled(loading || saving)
            }
            HStack {
                TextField("ORX 可执行文件路径", text: $executable,
                          prompt: Text("自动查找"))
                    .onSubmit { useExecutable() }
                Button("使用此路径") { useExecutable() }
                    .disabled(executable == savedExecutable || saving)
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
            if let message {
                Text(message).font(.callout).foregroundStyle(.secondary)
            }
            Divider()
            if let snapshot {
                configuration((preview ?? snapshot).modelSelection)
                    .disabled(saving)
                Divider()
                actions(snapshot)
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
        .onChange(of: edits) { _ in preview = nil }
    }

    private func useExecutable() {
        savedExecutable = executable
        Task { await load(project: app.routingProject) }
    }

    @MainActor
    private func load(project: String?) async {
        let id = UUID()
        loadID = id
        loading = true
        snapshot = nil
        error = nil
        editing = false
        edits = [:]
        preview = nil
        message = nil
        defer { if id == loadID { loading = false } }
        do {
            let url = try ORXRoutingClient.resolveExecutable(override: savedExecutable)
            let result = try await ORXRoutingClient(executable: url).read(project: project)
            guard !Task.isCancelled, id == loadID, project == app.routingProject else { return }
            installedPath = url.path
            snapshot = result
        } catch {
            guard !Task.isCancelled, id == loadID, project == app.routingProject else { return }
            self.error = error.localizedDescription
        }
    }

    private func configuration(_ selection: RoutingSelection) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                group("Controller", origin: selection.controller.origin,
                      candidates: [selection.controller.candidate])
                if editing {
                    let key = "controller.profile"
                    Picker("Controller profile", selection: textBinding(key, initial: selection.controller.candidate.profile)) {
                        Text(selection.controller.candidate.profile).tag(selection.controller.candidate.profile)
                        ForEach((snapshot?.availableProfiles ?? []).filter {
                            $0 != selection.controller.candidate.profile
                        }, id: \.self) { Text($0).tag($0) }
                    }
                }
                VStack(alignment: .leading, spacing: 10) {
                    Text("Planner").font(.headline)
                    Text("规划深度  \(selection.planner.depth)  ·  \(originLabel(selection.planner.depthOrigin))")
                        .font(.callout).foregroundStyle(.secondary)
                    if editing {
                        Picker("规划深度", selection: textBinding("plan.depth", initial: selection.planner.depth)) {
                            Text("自动").tag("auto")
                            Text("light").tag("light")
                            Text("standard").tag("standard")
                            Text("deep").tag("deep")
                        }
                    }
                    ForEach(["light", "standard", "deep"], id: \.self) { depth in
                        if let group = selection.planner.depths[depth] {
                            self.group(depth, origin: group.origin, candidates: group.candidates,
                                       key: "plan.\(depth).profiles")
                        }
                    }
                }
                Divider()
                group("Worker", origin: selection.worker.origin, candidates: selection.worker.candidates,
                      key: "worker.profiles")
                Divider()
                group("Verifier", origin: selection.verifier.origin, candidates: selection.verifier.candidates,
                      key: "verify.profiles")
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func group(_ title: String, origin: String,
                       candidates: [RoutingCandidate], key: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title).font(.headline)
                Text(originLabel(origin)).font(.caption).foregroundStyle(.secondary)
            }
            if candidates.isEmpty {
                Text("未配置候选").foregroundStyle(.secondary)
            }
            if !editing || preview != nil || key == nil {
                ForEach(Array(candidates.enumerated()), id: \.offset) { index, candidate in
                    candidateRow(candidate, position: index + 1)
                }
            }
            if editing, let key {
                profileOrder(key, initial: candidates.map(\.profile))
                    .disabled(saving)
            }
        }
    }

    private func candidateRow(_ candidate: RoutingCandidate, position: Int) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(position). \(candidate.profile)").font(.body.weight(.medium))
            if let error = candidate.error {
                Text(error).foregroundStyle(.orange)
            } else {
                Text([candidate.harness, candidate.model, candidate.tier ?? candidate.legacyClass,
                      candidate.effort.map { "推理 \($0)" }, candidate.nativeEffort,
                      candidate.profileOrigin.map(originLabel)]
                    .compactMap { $0 }.joined(separator: "  ·  "))
                    .foregroundStyle(.secondary)
            }
        }
        .font(.callout)
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func actions(_ snapshot: RoutingSnapshot) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if editing {
                Text(preview == nil
                     ? "先预览目标配置文件及生效值，再保存。"
                     : "正在预览保存后的生效配置。")
                    .font(.callout)
                Text("变更由后续派发读取，已有尝试的模型和路由记录仍按当次事实显示。")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("取消") {
                        editing = false
                        preview = nil
                        edits = [:]
                        error = nil
                    }
                    Spacer()
                    if saving { ProgressView().controlSize(.small) }
                    Button("预览变更") { Task { await send(snapshot, apply: false) } }
                        .disabled(edits.isEmpty)
                    Button("保存") { Task { await send(snapshot, apply: true) } }
                        .buttonStyle(.borderedProminent)
                        .disabled(preview == nil)
                }
                .disabled(saving)
            } else {
                Button("编辑路由") { editing = true }
            }
        }
    }

    @MainActor
    private func send(_ base: RoutingSnapshot, apply: Bool) async {
        guard !saving, let installedPath else { return }
        let project = app.routingProject
        let client = ORXRoutingClient(executable: URL(fileURLWithPath: installedPath))
        let request = RoutingRequest(revision: base.revision, changes: edits)
        saving = true
        error = nil
        message = nil
        defer { saving = false }
        do {
            if apply {
                snapshot = try await client.save(project: project, request: request)
                editing = false
                preview = nil
                edits = [:]
                message = "已保存"
                snapshot = try await client.read(project: project)
            } else {
                preview = try await client.preview(project: project, request: request)
            }
        } catch {
            preview = nil
            self.error = error.localizedDescription
        }
    }

    private func textBinding(_ key: String, initial: String) -> Binding<String> {
        Binding(get: {
            if case let .text(value)? = edits[key] { return value }
            return initial
        }, set: { edits[key] = .text($0) })
    }

    private func orderedProfiles(_ key: String, initial: [String]) -> [String] {
        if case let .profiles(names)? = edits[key] { return names }
        return initial
    }

    @ViewBuilder
    private func profileOrder(_ key: String, initial: [String]) -> some View {
        let order = orderedProfiles(key, initial: initial)
        let available = (snapshot?.availableProfiles ?? []).filter { !order.contains($0) }
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(order.enumerated()), id: \.offset) { index, name in
                HStack {
                    Text("\(index + 1). \(name)").frame(minWidth: 140, alignment: .leading)
                    Button { move(index, to: index - 1, key: key, order: order) } label: {
                        Image(systemName: "arrow.up")
                    }
                    .disabled(index == 0)
                    .help("上移 \(name)")
                    Button { move(index, to: index + 1, key: key, order: order) } label: {
                        Image(systemName: "arrow.down")
                    }
                    .disabled(index + 1 == order.count)
                    .help("下移 \(name)")
                    Button {
                        var next = order
                        next.remove(at: index)
                        edits[key] = .profiles(next)
                    } label: {
                        Image(systemName: "minus")
                    }
                    .help("移除 \(name)")
                }
            }
            Menu("添加候选") {
                ForEach(available, id: \.self) { profile in
                    Button(profile) { edits[key] = .profiles(order + [profile]) }
                }
            }
            .disabled(available.isEmpty)
        }
    }

    private func move(_ index: Int, to destination: Int, key: String, order: [String]) {
        guard order.indices.contains(destination) else { return }
        var next = order
        next.swapAt(index, destination)
        edits[key] = .profiles(next)
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
