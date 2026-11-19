import AppKit
import SwiftUI

/// Window routing for the single run-detail window (G012 用户反馈 2:
/// 点击项目后窗口到最前方).
///
/// Clicking a panel card must ACTIVATE the app and bring the detail
/// window to the FRONT with keyboard focus — on first open, when the
/// window is already open but obscured by other apps, and across rapid
/// clicks on different projects. SwiftUI's `openWindow` alone reuses the
/// single `Window` scene (so it never creates a duplicate window) but
/// does not reliably activate a backgrounded menu-bar app (LSUIElement)
/// nor reorder an already-open obscured window. The routing therefore
/// wraps it in three steps:
///
/// 1. `NSApp.activate(ignoringOtherApps:)` — the app becomes frontmost
///    so the window can actually take keyboard focus;
/// 2. `openWindow(id:)` — opens the window, or reuses the existing
///    single instance (still exactly one run-detail window);
/// 3. a next-runloop-tick front pass — finds the run-detail `NSWindow`
///    and `makeKeyAndOrderFront`s it. This covers the
///    already-open-but-obscured case even when the click re-selects the
///    SAME run (no state change for SwiftUI to react to).
///
/// `RunDetailHostView` additionally fronts its own window on appear and
/// whenever the app's run selection changes, so view-side routing
/// (连续点击不同项目) fronts the window regardless of how the
/// selection was set (panel card, notification deep link).
enum DetailWindowRouting {
    /// Scene id of the run-detail window ("历史查询 / 执行详情").
    static let runDetailWindowID = "run-detail"

    /// Title fragment of the run-detail window — the fallback match for
    /// environments where SwiftUI does not expose the plain scene id as
    /// the NSWindow identifier.
    private static let runDetailTitleFragment = "执行详情"

    /// Open (or reuse) the run-detail window at the front with keyboard
    /// focus. Main-actor UI action; the selection must already be set
    /// (`AppModel.openRunDetail`) so the window opens showing the
    /// clicked run.
    @MainActor
    static func openRunDetailFrontmost(_ openWindow: OpenWindowAction) {
        NSApp.activate(ignoringOtherApps: true)
        openWindow(id: runDetailWindowID)
        // First open: SwiftUI materializes the NSWindow during the call
        // above; the front pass runs one tick later either way.
        DispatchQueue.main.async { frontRunDetailWindow() }
    }

    /// Bring the EXISTING run-detail window to the front and make it
    /// key, activating the app. No-op when no run-detail window exists.
    @MainActor
    static func frontRunDetailWindow() {
        NSApp.activate(ignoringOtherApps: true)
        for window in NSApp.windows where isRunDetailWindow(window) {
            window.makeKeyAndOrderFront(nil)
        }
    }

    /// Front one specific window (the detail host's own window when the
    /// host captured it); falls back to the window hunt when nil.
    @MainActor
    static func focus(_ window: NSWindow?) {
        NSApp.activate(ignoringOtherApps: true)
        if let window {
            window.makeKeyAndOrderFront(nil)
        } else {
            frontRunDetailWindow()
        }
    }

    @MainActor
    private static func isRunDetailWindow(_ window: NSWindow) -> Bool {
        window.identifier?.rawValue.contains(runDetailWindowID) == true
            || window.title.contains(runDetailTitleFragment)
    }
}
