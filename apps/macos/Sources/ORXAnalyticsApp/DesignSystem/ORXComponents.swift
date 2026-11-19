import SwiftUI

struct ORXRule: View {
    var body: some View {
        Rectangle().fill(ORXColor.border).frame(height: ORXLayout.stroke)
            .accessibilityHidden(true)
    }
}

struct ORXBadge: View {
    let title: String
    var tone: ORXTone = .neutral
    var symbol: String?

    init(_ title: String, tone: ORXTone = .neutral, symbol: String? = nil) {
        self.title = title
        self.tone = tone
        self.symbol = symbol
    }

    var body: some View {
        HStack(spacing: ORXSpace.xs) {
            if let symbol { Image(systemName: symbol).accessibilityHidden(true) }
            Text(title)
        }
        .font(ORXType.badge)
        .foregroundStyle(tone.ink)
        .padding(.horizontal, ORXSpace.sm)
        .padding(.vertical, ORXSpace.xs)
        .background(tone.fill, in: RoundedRectangle(cornerRadius: ORXRadius.badge))
    }
}

struct ORXPageHeader<Actions: View>: View {
    let title: String
    let detail: String?
    let actions: Actions

    init(_ title: String, detail: String? = nil, @ViewBuilder actions: () -> Actions) {
        self.title = title
        self.detail = detail
        self.actions = actions()
    }

    var body: some View {
        HStack(alignment: .center, spacing: ORXSpace.lg) {
            VStack(alignment: .leading, spacing: ORXSpace.xs) {
                Text(title).font(ORXType.page).accessibilityAddTraits(.isHeader)
                if let detail {
                    Text(detail).font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                }
            }
            Spacer(minLength: ORXSpace.md)
            actions
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

extension ORXPageHeader where Actions == EmptyView {
    init(_ title: String, detail: String? = nil) {
        self.init(title, detail: detail) { EmptyView() }
    }
}

struct ORXNotice: View {
    let text: String
    var tone: ORXTone = .neutral
    var symbol: String = "info.circle"

    var body: some View {
        Label(text, systemImage: symbol)
            .font(ORXType.label)
            .foregroundStyle(tone.ink)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(ORXSpace.md)
            .background(tone.fill, in: RoundedRectangle(cornerRadius: ORXRadius.row))
    }
}

struct ORXEmptyState<Actions: View>: View {
    let title: String
    let detail: String?
    let symbol: String
    let loading: Bool
    let actions: Actions

    init(_ title: String, detail: String? = nil, symbol: String = "tray",
         loading: Bool = false, @ViewBuilder actions: () -> Actions) {
        self.title = title
        self.detail = detail
        self.symbol = symbol
        self.loading = loading
        self.actions = actions()
    }

    var body: some View {
        VStack(spacing: ORXSpace.sm) {
            if loading {
                ProgressView().controlSize(.small)
            } else {
                Image(systemName: symbol).font(ORXType.page)
                    .foregroundStyle(ORXColor.secondary)
                    .accessibilityHidden(true)
            }
            Text(title).font(ORXType.title).accessibilityAddTraits(.isHeader)
            if let detail {
                Text(detail).font(ORXType.body).foregroundStyle(ORXColor.secondary)
                    .textSelection(.enabled)
            }
            actions
        }
        .multilineTextAlignment(.center)
        .padding(ORXSpace.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

extension ORXEmptyState where Actions == EmptyView {
    init(_ title: String, detail: String? = nil, symbol: String = "tray", loading: Bool = false) {
        self.init(title, detail: detail, symbol: symbol, loading: loading) { EmptyView() }
    }
}

struct ORXField<Control: View>: View {
    let title: String
    let control: Control

    init(_ title: String, @ViewBuilder control: () -> Control) {
        self.title = title
        self.control = control()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: ORXSpace.xs) {
            Text(title).font(ORXType.caption).foregroundStyle(ORXColor.secondary)
                .accessibilityHidden(true)
            control.accessibilityLabel(title)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct ORXValueRow: View {
    let name: String
    let value: String
    var monospaced: Bool = false

    init(_ name: String, _ value: String, monospaced: Bool = false) {
        self.name = name
        self.value = value
        self.monospaced = monospaced
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: ORXSpace.md) {
            Text(name).font(ORXType.caption).foregroundStyle(ORXColor.secondary)
            Spacer(minLength: ORXSpace.sm)
            Text(value).font(monospaced ? ORXType.code : ORXType.body).multilineTextAlignment(.trailing)
        }
        .accessibilityElement(children: .combine)
    }
}

extension View {
    /// Bordered content surfaces sit directly on the window canvas.
    func orxCard(padding: CGFloat = ORXSpace.lg, fill: Color = ORXColor.surface) -> some View {
        self.padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(fill, in: RoundedRectangle(cornerRadius: ORXRadius.card))
            .overlay {
                RoundedRectangle(cornerRadius: ORXRadius.card).strokeBorder(ORXColor.border)
                    .allowsHitTesting(false)
                    .accessibilityHidden(true)
            }
    }

    func orxSurface(_ fill: Color = ORXColor.canvas) -> some View {
        self.font(ORXType.body)
            .foregroundStyle(ORXColor.text)
            .tint(ORXColor.accent)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(fill)
    }
}

extension View {
    func orxCacheConfirmation(isPresented: Binding<Bool>, onConfirm: @escaping () -> Void) -> some View {
        alert("清理显示缓存？", isPresented: isPresented) {
            Button("取消", role: .cancel) {}
            Button("清理显示缓存", role: .destructive, action: onConfirm)
        } message: {
            Text("项目数据和 Analytics 采集记录会保留。")
        }
    }
}

/// A native editable field with ORX foreground, prompt, border and focus roles.
struct ORXTextInput: View {
    let title: String
    let prompt: String
    @Binding var text: String
    var monospaced = false
    var autoFocus = false
    @FocusState private var focused: Bool

    var body: some View {
        ZStack(alignment: .leading) {
            if text.isEmpty {
                Text(prompt).foregroundStyle(ORXColor.secondary)
                    .lineLimit(1).allowsHitTesting(false).accessibilityHidden(true)
            }
            TextField("", text: $text)
                .textFieldStyle(.plain)
                .foregroundStyle(ORXColor.text)
                .accessibilityLabel(title)
                .focused($focused)
        }
        .font(monospaced ? ORXType.code : ORXType.label)
        .padding(.horizontal, ORXSpace.sm)
        .frame(minHeight: ORXLayout.controlHeight)
        .background(ORXColor.surface, in: RoundedRectangle(cornerRadius: ORXRadius.control))
        .overlay {
            RoundedRectangle(cornerRadius: ORXRadius.control)
                .strokeBorder(focused ? ORXColor.accent : ORXColor.border)
                .allowsHitTesting(false)
        }
        .task {
            if autoFocus {
                await Task.yield()
                focused = true
            }
        }
    }
}
