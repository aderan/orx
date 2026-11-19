import AppKit

enum ORXMenuIcon {
    static func image(showsAttention: Bool) -> NSImage {
        showsAttention ? withAttention : normal
    }

    private static let normal = makeImage(badge: nil)
    private static let withAttention = makeImage(
        badge: NSImage(systemSymbolName: "exclamationmark.circle.fill", accessibilityDescription: nil))

    private static let wordmark: NSBezierPath = {
        let path = NSBezierPath(roundedRect: NSRect(x: 5, y: 6.25, width: 4.5, height: 6.5),
                                xRadius: 1.75, yRadius: 1.75)
        // R
        path.move(to: NSPoint(x: 11.8, y: 6.25))
        path.line(to: NSPoint(x: 11.8, y: 12.75))
        path.line(to: NSPoint(x: 14.6, y: 12.75))
        path.curve(to: NSPoint(x: 14.6, y: 9.55),
                   controlPoint1: NSPoint(x: 16.75, y: 12.75),
                   controlPoint2: NSPoint(x: 16.75, y: 9.55))
        path.line(to: NSPoint(x: 11.8, y: 9.55))
        path.move(to: NSPoint(x: 13.95, y: 9.55))
        path.line(to: NSPoint(x: 16.15, y: 6.25))
        // X
        path.move(to: NSPoint(x: 18, y: 12.75))
        path.line(to: NSPoint(x: 21.65, y: 6.25))
        path.move(to: NSPoint(x: 18, y: 6.25))
        path.line(to: NSPoint(x: 21.65, y: 12.75))
        path.lineWidth = 1.25
        path.lineCapStyle = .round
        path.lineJoinStyle = .round
        return path
    }()

    private static func makeImage(badge: NSImage?) -> NSImage {
        let size = ORXLayout.menuBarIconSize
        let bounds = NSRect(origin: .zero, size: size)
        let badgeSize = ORXLayout.menuBarAttentionBadgeSize
        let badgeFrame = NSRect(x: size.width - badgeSize, y: size.height - badgeSize,
                                width: badgeSize, height: badgeSize)
        let outline = NSBezierPath(roundedRect: NSRect(x: 1, y: 2.25, width: 25, height: 14.5),
                                   xRadius: 4, yRadius: 4)
        outline.lineWidth = 1.2
        let image = NSImage(size: size, flipped: false) { _ in
            NSColor.black.setStroke()
            NSGraphicsContext.saveGraphicsState()
            if badge != nil {
                // Leave a narrow gap where the attention badge overlaps the outline.
                let clip = NSBezierPath(rect: bounds)
                clip.append(NSBezierPath(ovalIn: badgeFrame.insetBy(dx: -1, dy: -1)))
                clip.windingRule = .evenOdd
                clip.addClip()
            }
            outline.stroke()
            NSGraphicsContext.restoreGraphicsState()
            wordmark.stroke()
            badge?.draw(in: badgeFrame)
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "ORX"
        return image
    }
}
