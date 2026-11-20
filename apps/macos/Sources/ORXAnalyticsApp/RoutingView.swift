import SwiftUI
import ORXAnalyticsCore

/// Routing configuration has its own availability and never gates engine queries.
struct RoutingView: View {
    private enum Presentation: String, CaseIterable {
        case candidates = "候选配置"
        case topology = "派发拓扑"
        case matrix = "等级与能力"
    }

    private struct CandidateInspection: Identifiable {
        let candidate: RoutingCandidate
        var id: String { candidate.profile }
    }

    @State private var presentation: Presentation = .candidates
    @State private var filter = ""
    @State private var tier = ""
    @State private var inspection: CandidateInspection?

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
    @State private var previewedChanges: [String: RoutingValue]?
    private var validPreview: RoutingSnapshot? { previewedChanges == edits ? preview : nil }
    @State private var saving = false
    @State private var message: String?
    @State private var loadID = UUID()

    private var projects: [String] {
        let reported = app.cachedOverview?.envelope.projectHealth.map(\.project) ?? []
        return Set(reported + [app.routingProject].compactMap { $0 }).sorted()
    }

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: ORXSpace.md) {
                header
                scopeControls
                if let installedPath, let snapshot {
                    ORXValueRow("ORX \(snapshot.orxVersion)", installedPath, monospaced: true)
                        .textSelection(.enabled)
                    ORXValueRow("目标配置文件", (validPreview ?? snapshot).targetPath, monospaced: true)
                        .textSelection(.enabled)
                }
                if let error {
                    ORXNotice(text: error, tone: .warning, symbol: "exclamationmark.triangle")
                        .textSelection(.enabled)
                }
                if let message {
                    ORXNotice(text: message, tone: .success, symbol: "checkmark.circle")
                }
            }
            .padding(ORXSpace.lg)
            .background(ORXColor.surface)
            ORXRule()
            if let snapshot {
                let selection = (validPreview ?? snapshot).modelSelection
                if !editing { presentationControls }
                configuration(selection).disabled(saving)
                if editing {
                    ORXRule()
                    actions(snapshot).padding(ORXSpace.lg).background(ORXColor.surface)
                }
            } else if loading {
                ORXEmptyState("正在读取 ORX 配置", loading: true)
            } else {
                ORXEmptyState(error == nil ? "尚无路由配置" : "无法读取路由配置",
                              detail: "使用兼容的 ORX 可执行文件，或调整顶栏路径后重新载入。",
                              symbol: "point.3.connected.trianglepath.dotted")
            }
        }
        .orxSurface()
        .onAppear { executable = savedExecutable }
        .task(id: app.routingProject) { await load(project: app.routingProject) }
        .onChange(of: edits) { _ in clearPreview() }
        .sheet(item: $inspection) { selected in
            VStack(alignment: .leading, spacing: ORXSpace.lg) {
                ORXPageHeader(selected.candidate.profile, detail: "配置是后续派发的候选。")
                candidateFacts(selected.candidate)
                Spacer(minLength: 0)
                HStack {
                    Spacer()
                    Button("完成") { inspection = nil }
                        .buttonStyle(ORXButtonStyle(variant: .primary))
                        .keyboardShortcut(.defaultAction)
                }
            }
            .padding(ORXSpace.xl)
            .frame(minWidth: ORXLayout.routingInspectorWidth)
            .background(ORXColor.surface)
        }
    }

    private var header: some View {
        ORXPageHeader("模型与路由", detail: "检查生效配置和后续派发的候选顺序。") {
            if editing {
                ORXBadge(validPreview == nil ? "编辑中" : "预览中", tone: .warning)
            }
            Button {
                Task { await load(project: app.routingProject) }
            } label: {
                Label("重新载入", systemImage: "arrow.clockwise")
            }
            .buttonStyle(ORXButtonStyle())
            .disabled(loading || saving)
            if snapshot != nil && !editing {
                Button {
                    editing = true
                    message = nil
                } label: {
                    Label("编辑路由", systemImage: "square.and.pencil")
                }
                .buttonStyle(ORXButtonStyle(variant: .primary))
            }
        }
    }

    private var scopeControls: some View {
        HStack(alignment: .bottom, spacing: ORXSpace.md) {
            ORXField("生效范围") {
                Picker("生效范围", selection: Binding(get: { app.routingProject }, set: app.openRouting)) {
                    Text("用户默认").tag(String?.none)
                    ForEach(projects, id: \.self) { project in
                        Text(project).tag(Optional(project))
                    }
                }
                .labelsHidden()
            }
            .frame(maxWidth: ORXLayout.routingScopeWidth)
            .disabled(saving)
            ORXField("ORX 可执行文件路径") {
                ORXTextInput(title: "ORX 可执行文件路径", prompt: "自动查找", text: $executable, monospaced: true)
                    .onSubmit { if !saving { useExecutable() } }
            }
            Button("使用此路径", action: useExecutable)
                .buttonStyle(ORXButtonStyle())
                .disabled(executable == savedExecutable || saving)
        }
    }

    private var presentationControls: some View {
        HStack(spacing: ORXSpace.md) {
            Picker("路由视图", selection: $presentation) {
                ForEach(Presentation.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }
            .labelsHidden().pickerStyle(.segmented).fixedSize()
            Spacer(minLength: ORXSpace.sm)
            if presentation != .topology {
                Picker("等级", selection: $tier) {
                    Text("全部等级").tag("")
                    ForEach(["T0", "T1", "T2", "T3", "S"], id: \.self) { Text($0).tag($0) }
                }
                .fixedSize()
                ORXTextInput(title: "筛选候选", prompt: "配置名、模型或能力", text: $filter)
                    .frame(maxWidth: ORXLayout.routingScopeWidth)
            }
        }
        .padding(.horizontal, ORXSpace.lg)
        .padding(.vertical, ORXSpace.sm)
        .background(ORXColor.inset)
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
        clearPreview()
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
            VStack(alignment: .leading, spacing: ORXSpace.lg) {
                controllerContext(selection.controller)
                if editing || presentation == .candidates {
                    candidateGroups(selection)
                } else if presentation == .topology {
                    topology(selection)
                } else {
                    matrix(selection)
                }
            }
            .padding(ORXSpace.lg)
        }
    }

    private func candidateGroups(_ selection: RoutingSelection) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.lg) {
            Text("配置候选").font(ORXType.title).accessibilityAddTraits(.isHeader)
            let depth = selection.planner.depth
            HStack(spacing: ORXSpace.md) {
                Label("Planner", systemImage: "list.bullet.rectangle").font(ORXType.section)
                Text("默认规划深度 \(depth) · \(originLabel(selection.planner.depthOrigin))")
                    .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                Spacer()
                if editing {
                    Picker("默认规划深度", selection: textBinding("plan.depth", initial: depth)) {
                        Text("light").tag("light")
                        Text("standard").tag("standard")
                        Text("deep").tag("deep")
                    }
                    .fixedSize()
                }
            }
            ForEach(["light", "standard", "deep"], id: \.self) { depth in
                if let group = selection.planner.depths[depth] {
                    self.group(depth, origin: group.origin, candidates: group.candidates,
                               key: "plan.\(depth).profiles")
                }
            }
            group("Worker", origin: selection.worker.origin, candidates: selection.worker.candidates,
                  key: "worker.profiles")
            group("Verifier", origin: selection.verifier.origin, candidates: selection.verifier.candidates,
                  key: "verify.profiles")
        }
    }

    private func topology(_ selection: RoutingSelection) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.md) {
            Text("派发拓扑").font(ORXType.title).accessibilityAddTraits(.isHeader)
            Text("各角色显示配置顺序。派发时 ORX 按任务能力、宿主和资源状态筛选。")
                .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            HStack(alignment: .top, spacing: ORXSpace.md) {
                topologyStage("Controller", profiles: [selection.controller.sessionHarness ?? "宿主未声明"])
                topologyArrow
                let depth = selection.planner.depth
                topologyStage("Planner · \(depth)", profiles: selection.planner.depths[depth]?.candidates.map(\.profile) ?? [])
                topologyArrow
                topologyStage("Worker", profiles: selection.worker.candidates.map(\.profile))
                topologyArrow
                topologyStage("Verifier", profiles: selection.verifier.candidates.map(\.profile))
            }
        }
    }

    private var topologyArrow: some View {
        Image(systemName: "arrow.right")
            .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            .padding(.top, ORXSpace.md)
            .accessibilityHidden(true)
    }

    private func topologyStage(_ label: String, profiles: [String]) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.sm) {
            Text(label).font(ORXType.section)
            if profiles.isEmpty {
                Text("未配置候选").font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            }
            ForEach(Array(profiles.enumerated()), id: \.offset) { index, profile in
                Label(profile, systemImage: index == 0 ? "arrow.forward.circle" : "arrow.turn.down.right")
                    .font(ORXType.code).help(profile)
            }
        }
        .orxCard(padding: ORXSpace.md)
    }

    private func matrix(_ selection: RoutingSelection) -> some View {
        let planners = ["light", "standard", "deep"].flatMap { selection.planner.depths[$0]?.candidates ?? [] }
        let candidates = planners + selection.worker.candidates + selection.verifier.candidates
        let unique = candidates.reduce(into: [RoutingCandidate]()) { rows, candidate in
            if !rows.contains(where: { $0.profile == candidate.profile }) { rows.append(candidate) }
        }
        let visible = unique.filter(matchesFilter)
        return VStack(alignment: .leading, spacing: ORXSpace.md) {
            Text("等级与能力").font(ORXType.title).accessibilityAddTraits(.isHeader)
            if visible.isEmpty {
                ORXNotice(text: "没有匹配的配置候选", symbol: "magnifyingglass")
            } else {
                Grid(alignment: .leading, horizontalSpacing: ORXSpace.lg, verticalSpacing: ORXSpace.sm) {
                    GridRow {
                        Text("执行配置")
                        Text("模型")
                        Text("等级")
                        Text("请求推理")
                        Text("执行能力")
                    }
                    .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                    ORXRule().gridCellUnsizedAxes(.horizontal)
                    ForEach(visible, id: \.profile) { candidate in
                        GridRow {
                            Button(candidate.profile) { inspection = CandidateInspection(candidate: candidate) }
                                .buttonStyle(ORXButtonStyle(variant: .quiet))
                            Text(configuredModel(candidate))
                            Text(RoutingLabels.tier(candidate.tier) ?? "—")
                            Text(candidate.effort ?? "—")
                            Text(candidate.capabilities?.joined(separator: "、") ?? "—")
                        }
                        .font(ORXType.label)
                    }
                }
                .orxCard()
            }
        }
    }

    private func controllerContext(_ controller: RoutingSelection.Controller) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.xs) {
            HStack {
                Label("Controller 会话", systemImage: "terminal").font(ORXType.section)
                Spacer()
            }
            if let host = controller.sessionHarness {
                let origin = controller.source == "environment" ? "查询环境中的宿主" : "配置查询声明的宿主"
                ORXValueRow(origin, host)
                if let agents = controller.nativeAgents, !agents.isEmpty {
                    ORXValueRow("已声明的原生代理", agents.joined(separator: "、"))
                }
            } else {
                Text("本次配置查询未声明宿主。Run 详情记录当次派发的 Controller。")
                    .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            }
        }
        .orxCard(padding: ORXSpace.md)
    }

    private func group(_ title: String, origin: String, candidates: [RoutingCandidate], key: String) -> some View {
        let visible = editing ? candidates : candidates.filter(matchesFilter)
        return VStack(alignment: .leading, spacing: ORXSpace.sm) {
            HStack {
                Text(title).font(ORXType.section)
                ORXBadge(originLabel(origin))
                Spacer()
            }
            if candidates.isEmpty {
                Text("未配置候选").font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            } else if visible.isEmpty {
                Text("没有匹配的候选").font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            }
            if !editing || validPreview != nil {
                ForEach(Array(candidates.enumerated()), id: \.offset) { index, candidate in
                    if editing || matchesFilter(candidate) {
                        ORXRule()
                        candidateRow(candidate, position: index + 1)
                    }
                }
            }
            if editing { profileOrder(key, initial: candidates.map(\.profile)) }
        }
        .orxCard(padding: ORXSpace.md)
    }

    private func candidateRow(_ candidate: RoutingCandidate, position: Int) -> some View {
        Button {
            inspection = CandidateInspection(candidate: candidate)
        } label: {
            HStack(alignment: .top, spacing: ORXSpace.md) {
                Text("\(position)").font(ORXType.number).foregroundStyle(ORXColor.secondary)
                VStack(alignment: .leading, spacing: ORXSpace.xs) {
                    Text(candidate.profile).font(ORXType.section)
                    if let error = candidate.error {
                        Text(error).font(ORXType.caption).foregroundStyle(ORXColor.warningText)
                    } else {
                        Text([configuredModel(candidate), candidateExecution(candidate),
                              candidate.profileOrigin.map(originLabel)].compactMap { $0 }.joined(separator: " · "))
                            .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                        if let reason = candidate.hostBlockedReason {
                            Label(RoutingLabels.reason(reason), systemImage: "exclamationmark.triangle")
                                .font(ORXType.caption).foregroundStyle(ORXColor.warningText)
                        }
                    }
                }
                Spacer(minLength: ORXSpace.sm)
                if let level = RoutingLabels.tier(candidate.tier) {
                    ORXBadge(level)
                }
                if let effort = candidate.effort { ORXBadge(effort) }
                Image(systemName: "chevron.right").font(ORXType.badge)
                    .foregroundStyle(ORXColor.secondary).accessibilityHidden(true)
            }
            .padding(.vertical, ORXSpace.sm)
            .padding(.horizontal, ORXSpace.xs)
        }
        .buttonStyle(ORXRowButtonStyle())
        .help("查看 \(candidate.profile) 的配置与能力")
    }

    private func candidateFacts(_ candidate: RoutingCandidate) -> some View {
        VStack(alignment: .leading, spacing: ORXSpace.sm) {
            ORXValueRow("执行方式", candidateExecution(candidate) ?? "—")
            ORXValueRow("配置模型", configuredModel(candidate), monospaced: true)
            ORXValueRow("来源", candidate.profileOrigin.map(originLabel) ?? "—")
            ORXValueRow("等级", RoutingLabels.tier(candidate.tier) ?? "—")
            ORXValueRow("请求推理", candidate.effort ?? "—")
            if let effort = candidate.nativeEffort { ORXValueRow("原生推理覆盖", effort) }
            if let ref = candidate.agentRef { ORXValueRow("原生代理", ref) }
            if let skills = candidate.capabilities { ORXValueRow("执行能力", skills.joined(separator: "、")) }
            if let spec = candidate.modelSpec {
                if let speed = spec.speed { ORXValueRow("速度", speed) }
                if let size = spec.context { ORXValueRow("上下文", "\(MetricsFormat.countText(size)) tokens") }
                if let skills = spec.capabilities { ORXValueRow("模型能力要求", skills.joined(separator: "、")) }
            }
            if let reason = candidate.hostBlockedReason {
                ORXNotice(text: RoutingLabels.reason(reason), tone: .warning, symbol: "exclamationmark.triangle")
            }
            if let error = candidate.error {
                ORXNotice(text: error, tone: .warning, symbol: "exclamationmark.triangle")
            }
        }
        .textSelection(.enabled)
    }

    private func candidateExecution(_ candidate: RoutingCandidate) -> String? {
        RoutingLabels.execution(driver: candidate.driver, harness: candidate.harness,
                                declaredHarness: candidate.declaredHarness, hostMode: candidate.hostMode)
    }

    private func configuredModel(_ candidate: RoutingCandidate) -> String {
        if candidate.driver == "host" && candidate.model == "unconfigured" {
            return "沿用会话模型"
        }
        return candidate.model ?? "模型未配置"
    }

    private func matchesFilter(_ candidate: RoutingCandidate) -> Bool {
        guard tier.isEmpty || candidate.tier == tier else { return false }
        let term = filter.trimmingCharacters(in: .whitespacesAndNewlines)
        let keys = [candidate.profile, configuredModel(candidate)] + (candidate.capabilities ?? [])
        return term.isEmpty || keys.contains { $0.localizedCaseInsensitiveContains(term) }
    }

    private func actions(_ snapshot: RoutingSnapshot) -> some View {
        HStack(alignment: .center, spacing: ORXSpace.sm) {
            VStack(alignment: .leading, spacing: ORXSpace.xs) {
                Text(validPreview == nil ? "编辑后预览，确认生效配置再保存。" : "正在预览保存后的生效配置。")
                    .font(ORXType.section)
                Text("变更用于后续派发；已有尝试仍按当次事实显示。")
                    .font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            }
            Spacer()
            if saving { ProgressView().controlSize(.small).accessibilityLabel("正在处理配置") }
            Button("取消") {
                editing = false
                clearPreview()
                edits = [:]
                error = nil
            }
            .buttonStyle(ORXButtonStyle(variant: .quiet))
            Button("预览变更") { Task { await send(snapshot, apply: false) } }
                .buttonStyle(ORXButtonStyle())
                .disabled(edits.isEmpty)
            Button("保存") { Task { await send(snapshot, apply: true) } }
                .buttonStyle(ORXButtonStyle(variant: .primary))
                .disabled(validPreview == nil)
        }
        .disabled(saving)
    }

    @MainActor
    private func send(_ base: RoutingSnapshot, apply: Bool) async {
        guard !saving, !edits.isEmpty, !apply || validPreview != nil, let installedPath else { return }
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
                clearPreview()
                edits = [:]
                message = "已保存"
                snapshot = try await client.read(project: project)
            } else {
                let result = try await client.preview(project: project, request: request)
                guard request.changes == edits else { return }
                preview = result
                previewedChanges = request.changes
            }
        } catch {
            clearPreview()
            self.error = error.localizedDescription
        }
    }

    private func clearPreview() {
        preview = nil
        previewedChanges = nil
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
        VStack(alignment: .leading, spacing: ORXSpace.xs) {
            ForEach(Array(order.enumerated()), id: \.offset) { index, name in
                HStack {
                    Text("\(index + 1). \(name)").font(ORXType.code)
                    Spacer()
                    Button { move(index, to: index - 1, key: key, order: order) } label: {
                        Label("上移 \(name)", systemImage: "arrow.up").labelStyle(.iconOnly)
                    }
                    .disabled(index == 0)
                    .help("上移 \(name)")
                    Button { move(index, to: index + 1, key: key, order: order) } label: {
                        Label("下移 \(name)", systemImage: "arrow.down").labelStyle(.iconOnly)
                    }
                    .disabled(index + 1 == order.count)
                    .help("下移 \(name)")
                    Button {
                        var next = order
                        next.remove(at: index)
                        edits[key] = .profiles(next)
                    } label: {
                        Label("移除 \(name)", systemImage: "minus").labelStyle(.iconOnly)
                    }
                    .help("移除 \(name)")
                }
                .buttonStyle(ORXButtonStyle(variant: .quiet))
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

enum RoutingLabels {
    static func tier(_ value: String?) -> String? {
        switch value {
        case "T0": return "T0 旗舰"
        case "T1": return "T1 高阶"
        case "T2": return "T2 强力"
        case "T3": return "T3 高效"
        case "S": return "S 专有"
        case let value?: return value
        case nil: return nil
        }
    }

    static func execution(driver: String?, harness: String?, declaredHarness: String? = nil,
                          hostMode: String? = nil) -> String? {
        let tool = declaredHarness == "current"
            ? (harness == "current" ? "当前宿主" : harness.map { "当前宿主 → \($0)" })
            : harness
        let method: String?
        switch (driver, hostMode) {
        case ("host", "subagent"): method = "宿主子代理"
        case ("host", "self"): method = "宿主自行执行"
        case ("host", _): method = "宿主"
        case ("cli", _): method = "CLI"
        case ("external", _): method = "外部执行"
        default: method = driver
        }
        let parts = [method, tool].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " / ")
    }

    static func reason(_ value: String) -> String {
        let parts = value.split(separator: ":", maxSplits: 1).map(String.init)
        switch parts.first {
        case "host_not_declared": return "待声明 Controller 宿主"
        case "host_harness_mismatch": return "Controller 宿主不匹配 · \(parts.last ?? value)"
        case "host_agent_unavailable": return "未声明原生代理 · \(parts.last ?? value)"
        case "specialized_capability_required": return "任务需显式请求专有能力"
        case "missing_capability": return "缺少任务能力 · \(parts.last ?? value)"
        case "primary": return "首选"
        case "fallback": return "回退"
        case "pinned": return "指定候选"
        case "constrained_last_resort": return "受限条件下的最后候选"
        default: return value
        }
    }
}
