import SwiftUI

struct ORXButtonStyle: ButtonStyle {
    enum Variant { case standard, primary, quiet, destructive }

    var variant: Variant = .standard

    func makeBody(configuration: Configuration) -> some View {
        Control(configuration: configuration, variant: variant)
    }

    private struct Control: View {
        let configuration: Configuration
        let variant: Variant
        @Environment(\.isEnabled) private var isEnabled
        @State private var hovered = false

        var body: some View {
            configuration.label
                .font(ORXType.label)
                .foregroundStyle(foreground)
                .padding(.horizontal, ORXSpace.md)
                .frame(minHeight: ORXLayout.controlHeight)
                .background(background, in: RoundedRectangle(cornerRadius: ORXRadius.control))
                .overlay {
                    if variant == .standard {
                        RoundedRectangle(cornerRadius: ORXRadius.control)
                            .strokeBorder(ORXColor.border)
                            .allowsHitTesting(false)
                    }
                }
                .contentShape(Rectangle())
                .opacity(isEnabled ? 1 : 0.5)
                .onHover { hovered = $0 }
        }

        private var foreground: Color {
            switch variant {
            case .primary: return ORXColor.onAccent
            case .destructive: return ORXColor.dangerText
            case .standard, .quiet: return ORXColor.text
            }
        }

        private var background: Color {
            if configuration.isPressed || hovered {
                switch variant {
                case .primary: return ORXColor.accent.opacity(0.85)
                case .destructive: return ORXColor.dangerFill
                case .standard, .quiet: return ORXColor.hover
                }
            }
            switch variant {
            case .standard: return ORXColor.surface
            case .primary: return ORXColor.accent
            case .quiet, .destructive: return .clear
            }
        }
    }
}

struct ORXRowButtonStyle: ButtonStyle {
    var selected: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        Row(configuration: configuration, selected: selected)
    }

    private struct Row: View {
        let configuration: Configuration
        let selected: Bool
        @State private var hovered = false

        var body: some View {
            configuration.label
                .foregroundStyle(ORXColor.text)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(background, in: RoundedRectangle(cornerRadius: ORXRadius.row))
                .contentShape(Rectangle())
                .onHover { hovered = $0 }
        }

        private var background: Color {
            if selected || configuration.isPressed { return ORXColor.selected }
            return hovered ? ORXColor.hover : .clear
        }
    }
}
