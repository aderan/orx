import AppKit
import SwiftUI
import ORXAnalyticsCore

/// Semantic tokens shared by the main window, auxiliary windows and menu-bar panel.
enum ORXColor {
    static let canvas = adaptive(0xFAFAFA, 0x09090B)
    static let surface = adaptive(0xFFFFFF, 0x18181B)
    static let inset = adaptive(0xF4F4F5, 0x202023)
    static let hover = adaptive(0xECECEE, 0x29292D)
    static let selected = adaptive(0xE4E4E7, 0x35353A)
    static let border = adaptive(0xDADAE0, 0x3F3F46)
    static let text = adaptive(0x18181B, 0xF4F4F5)
    static let secondary = adaptive(0x52525B, 0xA1A1AA)
    static let accent = adaptive(0x18181B, 0xF4F4F5)
    static let onAccent = adaptive(0xFFFFFF, 0x18181B)
    static let warningText = adaptive(0x854D0E, 0xFCD34D)
    static let warningFill = adaptive(0xFEF3C7, 0x3E3018)
    static let dangerText = adaptive(0x991B1B, 0xFCA5A5)
    static let dangerFill = adaptive(0xFEF2F2, 0x381D20)
    static let successText = adaptive(0x166534, 0x86EFAC)
    static let successFill = adaptive(0xECFDF5, 0x163027)

    private static func adaptive(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let hex = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: Double((hex >> 16) & 255) / 255,
                           green: Double((hex >> 8) & 255) / 255,
                           blue: Double(hex & 255) / 255, alpha: 1)
        })
    }
}

enum ORXType {
    static let page = Font.system(size: 22, weight: .semibold)
    static let title = Font.system(size: 15, weight: .semibold)
    static let section = Font.system(size: 13, weight: .semibold)
    static let body = Font.system(size: 13)
    static let label = Font.system(size: 12, weight: .medium)
    static let caption = Font.system(size: 11)
    static let badge = Font.system(size: 10, weight: .semibold)
    static let code = Font.system(size: 11, design: .monospaced)
    static let number = Font.system(size: 13, weight: .medium).monospacedDigit()
    static let metric = Font.system(size: 16, weight: .semibold).monospacedDigit()
}

enum ORXSpace {
    static let xxs: CGFloat = 2
    static let xs: CGFloat = 4
    static let sm: CGFloat = 8
    static let md: CGFloat = 12
    static let lg: CGFloat = 16
    static let xl: CGFloat = 24
    static let xxl: CGFloat = 32
}

enum ORXRadius {
    static let badge: CGFloat = 4
    static let control: CGFloat = 6
    static let row: CGFloat = 8
    static let card: CGFloat = 12
}

enum ORXLayout {
    static let menuBarMarkSize: CGFloat = 14
    static let panelWidth: CGFloat = 368
    static let panelScrollHeight: CGFloat = 480
    static let windowMinWidth: CGFloat = 820
    static let windowMinHeight: CGFloat = 615
    static let windowIdealWidth: CGFloat = 1060
    static let windowIdealHeight: CGFloat = 740
    static let splitBreakpoint: CGFloat = 920
    static let settingsNavigationWidth: CGFloat = 152
    static let settingsWidth: CGFloat = 680
    static let settingsHeight: CGFloat = 530
    static let diagnosticsWidth: CGFloat = 660
    static let diagnosticsHeight: CGFloat = 560
    static let controlHeight: CGFloat = 27
    static let historyIdentifierField: CGFloat = 90
    static let historyOrderField: CGFloat = 94
    static let routingScopeWidth: CGFloat = 255
    static let routingInspectorWidth: CGFloat = 420
    static let taskColumn: CGFloat = 320
    static let historyProjectColumn: CGFloat = 92
    static let historyStatusColumn: CGFloat = 94
    static let historyProgressColumn: CGFloat = 70
    static let historyUsageColumn: CGFloat = 134
    static let historyTimeColumn: CGFloat = 146
    static let stroke: CGFloat = 1
}

extension AppAppearance {
    var nativeAppearance: NSAppearance? {
        switch self {
        case .system: return nil
        case .light: return NSAppearance(named: .aqua)
        case .dark: return NSAppearance(named: .darkAqua)
        }
    }

    var title: String {
        switch self {
        case .system: return "跟随系统"
        case .light: return "浅色"
        case .dark: return "深色"
        }
    }
}

enum ORXTone {
    case neutral, warning, danger, success

    var ink: Color {
        switch self {
        case .neutral: return ORXColor.text
        case .warning: return ORXColor.warningText
        case .danger: return ORXColor.dangerText
        case .success: return ORXColor.successText
        }
    }

    var fill: Color {
        switch self {
        case .neutral: return ORXColor.inset
        case .warning: return ORXColor.warningFill
        case .danger: return ORXColor.dangerFill
        case .success: return ORXColor.successFill
        }
    }

    static func execution(_ raw: String?) -> Self {
        switch raw {
        case "failed", "error": return .danger
        case "blocked", "waiting", "waiting_external": return .warning
        case "done", "completed", "success", "passed": return .success
        default: return .neutral
        }
    }
}
