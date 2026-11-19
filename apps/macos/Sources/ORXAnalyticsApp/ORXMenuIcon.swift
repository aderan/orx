import AppKit

enum ORXMenuIcon {
    static func image(showsAttention: Bool) -> NSImage {
        showsAttention ? withAttention : normal
    }

    private static let normal = makeImage(badge: nil)
    private static let withAttention = makeImage(
        badge: NSImage(systemSymbolName: "exclamationmark.circle.fill", accessibilityDescription: nil))

    private static let mark: NSImage = {
        let side = ORXLayout.menuBarMarkSize
        return NSImage(size: NSSize(width: side, height: side), flipped: true) { _ in
            guard let context = NSGraphicsContext.current?.cgContext else { return false }
            context.saveGState()
            defer { context.restoreGState() }
            let scale = side / 18
            context.scaleBy(x: scale, y: scale)

            // The open O and check from ORXAppIcon.svg use an 18-point coordinate space.
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
    }()

    private static func makeImage(badge: NSImage?) -> NSImage {
        let size = ORXLayout.menuBarIconSize
        let markFrame = NSRect(x: 0, y: (size.height - mark.size.height) / 2,
                               width: mark.size.width, height: mark.size.height)
        let badgeSize = ORXLayout.menuBarAttentionBadgeSize
        let badgeFrame = NSRect(x: size.width - badgeSize, y: size.height - badgeSize,
                                width: badgeSize, height: badgeSize)
        let image = NSImage(size: size, flipped: false) { _ in
            mark.draw(in: markFrame)
            badge?.draw(in: badgeFrame)
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "ORX"
        return image
    }
}
