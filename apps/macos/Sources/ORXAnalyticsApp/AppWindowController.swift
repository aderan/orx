import AppKit
import SwiftUI
import ORXAnalyticsCore

/// One reusable window for each view, independent of the status item.
@MainActor
final class AppWindowController {
    private let model: AppModel
    private lazy var main = makeWindow("ORX", id: "run-detail", content: RunDetailHostView(
        openSettings: { [weak self] in self?.showSettings() }
    ))
    private lazy var diagnostics = makeWindow("诊断", id: "diagnostics", content: DiagnosticsView())
    private lazy var settings = makeWindow("ORX 设置", id: "settings", content: SettingsView())

    init(model: AppModel) {
        self.model = model
    }

    func showMain() { show(main) }

    func showDiagnostics() { show(diagnostics) }

    func showSettings() { show(settings) }

    private func makeWindow<Content: View>(_ title: String, id: String,
                                           content: Content) -> NSWindowController {
        let window = NSWindow(contentViewController: NSHostingController(
            rootView: content.environmentObject(model)))
        window.styleMask.insert([.closable, .miniaturizable, .resizable])
        window.title = title
        window.identifier = NSUserInterfaceItemIdentifier(id)
        window.isReleasedWhenClosed = false
        window.center()
        return NSWindowController(window: window)
    }

    private func show(_ controller: NSWindowController) {
        NSApp.activate(ignoringOtherApps: true)
        controller.showWindow(nil)
        controller.window?.makeKeyAndOrderFront(nil)
    }
}
