import AppKit

enum ORXMenuIcon {
    static func image(showsAttention: Bool) -> NSImage {
        showsAttention ? withAttention : normal
    }

    private static let normal: NSImage = {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: true) { _ in
            // The open O and check from ORXAppIcon.svg, fitted to the status bar.
            let outline = NSBezierPath()
            outline.move(to: NSPoint(x: 12, y: 4.3))
            outline.line(to: NSPoint(x: 10.5, y: 2.8))
            outline.line(to: NSPoint(x: 6.1, y: 2.8))
            outline.line(to: NSPoint(x: 2.2, y: 6.7))
            outline.line(to: NSPoint(x: 2.2, y: 11.1))
            outline.line(to: NSPoint(x: 6.1, y: 15))
            outline.line(to: NSPoint(x: 10.6, y: 15))
            outline.line(to: NSPoint(x: 14.5, y: 11.1))
            outline.line(to: NSPoint(x: 14.5, y: 9))
            outline.move(to: NSPoint(x: 5.1, y: 8.6))
            outline.line(to: NSPoint(x: 7.8, y: 11.3))
            outline.line(to: NSPoint(x: 15.5, y: 3.6))
            outline.lineWidth = 1.7
            outline.lineCapStyle = .round
            outline.lineJoinStyle = .round
            NSColor.black.setStroke()
            outline.stroke()
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "ORX"
        return image
    }()

    private static let withAttention: NSImage = {
        let configuration = NSImage.SymbolConfiguration(pointSize: 11, weight: .medium)
        guard let warning = NSImage(systemSymbolName: "exclamationmark.triangle.fill",
                                    accessibilityDescription: nil)?
            .withSymbolConfiguration(configuration) else { return normal }

        let brandFrame = NSRect(origin: .zero, size: normal.size)
        let warningFrame = NSRect(x: brandFrame.maxX + 4,
                                  y: brandFrame.midY - warning.size.height / 2,
                                  width: warning.size.width, height: warning.size.height)
        let size = NSSize(width: ceil(warningFrame.maxX), height: brandFrame.height)
        let image = NSImage(size: size, flipped: false) { _ in
            normal.draw(in: brandFrame)
            warning.draw(in: warningFrame)
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = normal.accessibilityDescription
        return image
    }()
}
