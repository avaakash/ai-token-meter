import AppKit

/// One item in the menu bar: a source glyph, which window ("5h", "W",
/// "Day"), the value, and optionally the time until it resets.
struct StatusEntry {
    var source: String
    var label: String
    var value: String
    var detail: String?
}

/// Draws the status item as a single image, so the Claude and Codex glyphs
/// can sit beside the text. In the normal state it's a template image, which
/// macOS tints for light and dark menu bars and the highlighted state; at
/// 75% and 90% it's drawn in the alert colour instead.
enum StatusRenderer {
    private static let glyphSize: CGFloat = 13
    private static let entryGap: CGFloat = 10

    static func image(_ entries: [StatusEntry], alert: NSColor?) -> NSImage {
        let font = NSFont.menuBarFont(ofSize: 0)
        let small = NSFont.systemFont(ofSize: max(9, font.pointSize - 2), weight: .medium)
        let height = NSStatusBar.system.thickness
        let ink = alert ?? .black

        func text(_ string: String, _ font: NSFont, alpha: CGFloat) -> NSAttributedString {
            NSAttributedString(string: string, attributes: [.font: font, .foregroundColor: ink.withAlphaComponent(alpha)])
        }
        let layouts = entries.map { e -> [NSAttributedString] in
            [text(e.label, small, alpha: 0.7), text(e.value, font, alpha: 1)] + (e.detail.map { [text($0, small, alpha: 0.7)] } ?? [])
        }
        let widths = layouts.map { parts in
            glyphSize + 3 + parts.enumerated().reduce(0) { $0 + ceil($1.element.size().width) + ($1.offset == 0 ? 0 : 3) }
        }
        let width = ceil(widths.reduce(0, +) + entryGap * CGFloat(max(0, entries.count - 1)) + 2)

        let image = NSImage(size: NSSize(width: width, height: height), flipped: true) { _ in
            let baseline = (height + font.capHeight) / 2
            var x: CGFloat = 1
            for (entry, parts) in zip(entries, layouts) {
                drawGlyph(entry.source, in: NSRect(x: x, y: (height - glyphSize) / 2, width: glyphSize, height: glyphSize), color: ink)
                x += glyphSize + 3
                for (i, part) in parts.enumerated() {
                    if i > 0 { x += 3 }
                    let partFont = part.attribute(.font, at: 0, effectiveRange: nil) as? NSFont ?? font
                    part.draw(at: NSPoint(x: x, y: baseline - partFont.ascender))
                    x += ceil(part.size().width)
                }
                x += entryGap
            }
            return true
        }
        image.isTemplate = alert == nil
        return image
    }

    /// The dashboard's glyphs, from their 20-unit SVG paths.
    private static func drawGlyph(_ source: String, in rect: NSRect, color: NSColor) {
        let s = rect.width / 20
        func p(_ x: CGFloat, _ y: CGFloat) -> NSPoint { NSPoint(x: rect.minX + x * s, y: rect.minY + y * s) }
        let path = NSBezierPath()
        if source == "codex" {
            path.append(NSBezierPath(roundedRect: NSRect(origin: p(2.8, 3.8), size: NSSize(width: 14.4 * s, height: 12.4 * s)),
                                     xRadius: 2.6 * s, yRadius: 2.6 * s))
            path.move(to: p(6.2, 8)); path.line(to: p(8.6, 10)); path.line(to: p(6.2, 12))
            path.move(to: p(10.6, 12.2)); path.line(to: p(13.8, 12.2))
        } else {
            for (a, b) in [((10, 2.6), (10, 17.4)), ((2.6, 10), (17.4, 10)), ((4.8, 4.8), (15.2, 15.2)), ((15.2, 4.8), (4.8, 15.2))] {
                path.move(to: p(a.0, a.1)); path.line(to: p(b.0, b.1))
            }
        }
        path.lineWidth = 1.7 * s
        path.lineCapStyle = .round
        path.lineJoinStyle = .round
        color.setStroke()
        path.stroke()
    }

    /// "45m", "3h 20m", "2d 5h".
    static func compactDuration(until date: Date, now: Date = Date()) -> String {
        let minutes = max(0, Int((date.timeIntervalSince(now) / 60).rounded()))
        if minutes < 60 { return "\(minutes)m" }
        let hours = minutes / 60
        if hours < 24 { return minutes % 60 == 0 ? "\(hours)h" : "\(hours)h \(minutes % 60)m" }
        return hours % 24 == 0 ? "\(hours / 24)d" : "\(hours / 24)d \(hours % 24)h"
    }
}
