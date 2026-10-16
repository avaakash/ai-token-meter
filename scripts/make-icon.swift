// Renders Token Meter's app icon at every size macOS asks for and packs
// them into macpapp/Resources/AppIcon.icns.
//
//   swift scripts/make-icon.swift            (from the repo root)
//
// The drawing is vector, done on Apple's 1024-point icon grid: an 824-point
// rounded square inset 100 points, with the standard soft drop shadow.

import AppKit

let canvas: CGFloat = 1024

func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat(hex >> 16 & 0xff) / 255, green: CGFloat(hex >> 8 & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: alpha)
}

func draw(in ctx: CGContext) {
    let body = CGRect(x: 100, y: 100, width: 824, height: 824)
    let shape = CGPath(roundedRect: body, cornerWidth: 186, cornerHeight: 186, transform: nil)

    // Base with drop shadow.
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: color(0x000000, 0.32))
    ctx.addPath(shape)
    ctx.setFillColor(color(0x1d1f23))
    ctx.fillPath()
    ctx.restoreGState()

    // Graphite gradient, lighter at the top.
    ctx.saveGState()
    ctx.addPath(shape)
    ctx.clip()
    let base = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                          colors: [color(0x3a3d44), color(0x1b1c20)] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(base, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])
    // A faint inner rim catches the light along the top edge.
    ctx.addPath(CGPath(roundedRect: body.insetBy(dx: 2, dy: 2), cornerWidth: 184, cornerHeight: 184, transform: nil))
    ctx.setStrokeColor(color(0xffffff, 0.10))
    ctx.setLineWidth(4)
    ctx.strokePath()
    ctx.restoreGState()

    // Segmented gauge: 240 degrees, eight segments, like the app's meters.
    let center = CGPoint(x: 512, y: 452)
    let radius: CGFloat = 268, thickness: CGFloat = 74
    let start: CGFloat = 210, sweep: CGFloat = 240, segments = 8, gap: CGFloat = 5
    let per = (sweep - gap * CGFloat(segments - 1)) / CGFloat(segments)
    let blue = CGGradient(colorsSpace: CGColorSpace(name: CGColorSpace.sRGB),
                          colors: [color(0x5aa2ff), color(0x2a78d6)] as CFArray, locations: [0, 1])!
    for i in 0..<segments {
        let a0 = (start - CGFloat(i) * (per + gap)) * .pi / 180
        let a1 = (start - CGFloat(i) * (per + gap) - per) * .pi / 180
        let arc = CGMutablePath()
        arc.addArc(center: center, radius: radius, startAngle: a0, endAngle: a1, clockwise: true)
        let stroked = arc.copy(strokingWithWidth: thickness, lineCap: .butt, lineJoin: .miter, miterLimit: 10)
        ctx.saveGState()
        ctx.addPath(stroked)
        if i < 5 {
            ctx.clip()
            ctx.drawLinearGradient(blue, start: CGPoint(x: 512, y: center.y + radius + thickness),
                                   end: CGPoint(x: 512, y: center.y - radius), options: [])
        } else if i == 5 {
            ctx.setFillColor(color(0xfab219))
            ctx.fillPath()
        } else {
            ctx.setFillColor(color(0xffffff, 0.13))
            ctx.fillPath()
        }
        ctx.restoreGState()
    }

    // Needle at about 68%, with a hub.
    let angle = (start - 0.68 * sweep) * .pi / 180
    let tip = CGPoint(x: center.x + cos(angle) * (radius - 18), y: center.y + sin(angle) * (radius - 18))
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -4), blur: 10, color: color(0x000000, 0.45))
    ctx.setStrokeColor(color(0xffffff))
    ctx.setLineWidth(26)
    ctx.setLineCap(.round)
    ctx.move(to: center)
    ctx.addLine(to: tip)
    ctx.strokePath()
    ctx.setFillColor(color(0xffffff))
    ctx.fillEllipse(in: CGRect(x: center.x - 50, y: center.y - 50, width: 100, height: 100))
    ctx.restoreGState()
    ctx.setFillColor(color(0x24262b))
    ctx.fillEllipse(in: CGRect(x: center.x - 20, y: center.y - 20, width: 40, height: 40))

    // A capacity bar below the gauge: the app's other recurring form.
    let bar = CGRect(x: 302, y: 236, width: 420, height: 36)
    let parts: [(CGFloat, CGColor)] = [(0.46, color(0x3987e5)), (0.2, color(0xeb6834)), (0.16, color(0x1baf7a)), (0.1, color(0xeda100))]
    ctx.saveGState()
    ctx.addPath(CGPath(roundedRect: bar, cornerWidth: 18, cornerHeight: 18, transform: nil))
    ctx.clip()
    ctx.setFillColor(color(0xffffff, 0.13))
    ctx.fill(bar)
    var x = bar.minX
    for (share, c) in parts {
        let w = bar.width * share
        ctx.setFillColor(c)
        ctx.fill(CGRect(x: x, y: bar.minY, width: w - 6, height: bar.height))
        x += w
    }
    ctx.restoreGState()
}

func png(size: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    let ctx = NSGraphicsContext(bitmapImageRep: rep)!.cgContext
    ctx.scaleBy(x: CGFloat(size) / canvas, y: CGFloat(size) / canvas)
    draw(in: ctx)
    return rep.representation(using: .png, properties: [:])!
}

let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
let iconset = FileManager.default.temporaryDirectory.appendingPathComponent("AppIcon-\(UUID().uuidString).iconset")
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
for points in [16, 32, 128, 256, 512] {
    try png(size: points).write(to: iconset.appendingPathComponent("icon_\(points)x\(points).png"))
    try png(size: points * 2).write(to: iconset.appendingPathComponent("icon_\(points)x\(points)@2x.png"))
}
let out = root.appendingPathComponent("macpapp/Resources/AppIcon.icns")
let iconutil = Process()
iconutil.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
iconutil.arguments = ["-c", "icns", iconset.path, "-o", out.path]
try iconutil.run()
iconutil.waitUntilExit()
try? png(size: 1024).write(to: root.appendingPathComponent("docs/assets/icon.png"))
try? FileManager.default.removeItem(at: iconset)
print(iconutil.terminationStatus == 0 ? "wrote \(out.path)" : "iconutil failed")
exit(iconutil.terminationStatus)
