// Draws GRC's app icon (a small connected network) as a 1024px PNG.
import AppKit

let size = 1024.0
let image = NSImage(size: NSSize(width: size, height: size))
image.lockFocus()
let ctx = NSGraphicsContext.current!.cgContext
let inset = 100.0, rect = CGRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
let bg = CGPath(roundedRect: rect, cornerWidth: 185, cornerHeight: 185, transform: nil)
ctx.addPath(bg); ctx.clip()
let colors = [NSColor(red: 0.17, green: 0.42, blue: 0.36, alpha: 1).cgColor, NSColor(red: 0.09, green: 0.24, blue: 0.21, alpha: 1).cgColor] as CFArray
ctx.drawLinearGradient(CGGradient(colorsSpace: nil, colors: colors, locations: [0, 1])!, start: CGPoint(x: 0, y: size), end: CGPoint(x: 0, y: 0), options: [])
let pts: [(Double, Double, Double)] = [(512, 540, 70), (330, 700, 46), (700, 720, 52), (300, 380, 40), (690, 360, 58), (500, 270, 34), (820, 520, 30)]
let links = [(0, 1), (0, 2), (0, 3), (0, 4), (4, 5), (3, 5), (2, 6), (4, 6), (1, 3)]
ctx.setStrokeColor(NSColor(white: 1, alpha: 0.55).cgColor); ctx.setLineWidth(16); ctx.setLineCap(.round)
for (a, b) in links { ctx.move(to: CGPoint(x: pts[a].0, y: pts[a].1)); ctx.addLine(to: CGPoint(x: pts[b].0, y: pts[b].1)) }
ctx.strokePath()
for (i, p) in pts.enumerated() {
    ctx.setFillColor(i == 0 ? NSColor(red: 0.55, green: 0.9, blue: 0.76, alpha: 1).cgColor : NSColor.white.cgColor)
    ctx.fillEllipse(in: CGRect(x: p.0 - p.2, y: p.1 - p.2, width: p.2 * 2, height: p.2 * 2))
}
image.unlockFocus()
let rep = NSBitmapImageRep(data: image.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
