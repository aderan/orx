import Foundation
import ServiceManagement

/// Login-item management through `SMAppService.mainApp` (design §9:
/// 通过 SMAppService.mainApp 管理登录启动，默认关闭，显示系统实际状态).
///
/// The toggle NEVER tracks a desired state: after every action (and on
/// every view appearance) the SYSTEM's actual status is re-read and
/// displayed. Registering from a bare SwiftPM dev executable is refused
/// by the system — that refusal is shown honestly, it is not hidden.
@MainActor
final class LoginItemController: ObservableObject {

    /// Mirrors SMAppService.Status with user-facing text. The displayed
    /// value is always the system's own answer.
    enum SystemState: Equatable {
        case enabled
        case requiresApproval
        case notRegistered
        case unavailable

        var text: String {
            switch self {
            case .enabled:
                return "已登记（登录时启动）"
            case .requiresApproval:
                return "等待批准（在 系统设置 → 通用 → 登录项 中确认）"
            case .notRegistered:
                return "未登记（默认：不随登录启动）"
            case .unavailable:
                return "不可用（需要从安装的应用包运行）"
            }
        }

        var isEnabled: Bool { self == .enabled }
    }

    @Published private(set) var systemState: SystemState = .notRegistered
    /// The system's refusal text when a register/unregister call failed
    /// (e.g. running the bare dev executable instead of the .app).
    @Published private(set) var lastActionError: String?

    func refreshSystemState() {
        switch SMAppService.mainApp.status {
        case .enabled:
            systemState = .enabled
        case .requiresApproval:
            systemState = .requiresApproval
        case .notRegistered:
            systemState = .notRegistered
        case .notFound:
            systemState = .unavailable
        @unknown default:
            systemState = .unavailable
        }
    }

    /// Register/unregister with the system, then re-read the ACTUAL
    /// state — the UI never assumes the action succeeded.
    func setEnabled(_ on: Bool) {
        lastActionError = nil
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            lastActionError = "系统拒绝了该操作：\(error.localizedDescription)"
        }
        refreshSystemState()
    }
}
