#!/usr/bin/env swift

import AVFoundation
import CoreGraphics
import CoreText
import Foundation
import ImageIO

private struct Scene {
    let filename: String
    let caption: String
}

private let scenes = [
    Scene(
        filename: "01-overview.png",
        caption: "One control plane across every repository"
    ),
    Scene(
        filename: "02-launch-config.png",
        caption: "Launch with explicit model and review policy"
    ),
    Scene(
        filename: "03-plan-gate.png",
        caption: "Human judgment stays at the decision boundary"
    ),
    Scene(
        filename: "04-run-history.png",
        caption: "Every run becomes an audit trail"
    ),
    Scene(
        filename: "05-evaluation.png",
        caption: "Evidence becomes repeatable evaluation"
    ),
    Scene(
        filename: "06-report-feed.png",
        caption: "Reports form a cross-repository improvement feed"
    ),
]

private let width = 1920
private let height = 1080
private let framesPerSecond: Int32 = 30
private let secondsPerScene = 3.25
private let crossfadeSeconds = 0.55

private enum RenderError: Error, CustomStringConvertible {
    case argument(String)
    case image(String)
    case video(String)

    var description: String {
        switch self {
        case .argument(let message), .image(let message), .video(let message):
            return message
        }
    }
}

private func arguments() throws -> (screenshots: URL, output: URL) {
    let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    var screenshots = cwd.appendingPathComponent("dist/visual-demo/screenshots")
    var output = cwd.appendingPathComponent("dist/visual-demo/orch-hub-demo.mp4")
    var index = 1
    let argv = CommandLine.arguments
    while index < argv.count {
        switch argv[index] {
        case "--screenshots":
            guard index + 1 < argv.count else {
                throw RenderError.argument("--screenshots requires a directory")
            }
            screenshots = URL(fileURLWithPath: argv[index + 1], relativeTo: cwd)
                .standardizedFileURL
            index += 2
        case "--output":
            guard index + 1 < argv.count else {
                throw RenderError.argument("--output requires an .mp4 path")
            }
            output = URL(fileURLWithPath: argv[index + 1], relativeTo: cwd)
                .standardizedFileURL
            index += 2
        default:
            throw RenderError.argument("unknown argument: \(argv[index])")
        }
    }
    guard output.pathExtension.lowercased() == "mp4" else {
        throw RenderError.argument("output must use the .mp4 extension")
    }
    return (screenshots, output)
}

private func loadImage(_ url: URL) throws -> CGImage {
    guard
        let source = CGImageSourceCreateWithURL(url as CFURL, nil),
        let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        throw RenderError.image("cannot read screenshot: \(url.path)")
    }
    return image
}

private func eased(_ value: CGFloat) -> CGFloat {
    let clamped = min(max(value, 0), 1)
    return clamped * clamped * (3 - 2 * clamped)
}

private func drawImage(
    _ image: CGImage,
    in context: CGContext,
    alpha: CGFloat
) {
    let canvasWidth = CGFloat(width)
    let canvasHeight = CGFloat(height)
    let scale = min(
        canvasWidth / CGFloat(image.width),
        canvasHeight / CGFloat(image.height)
    )
    let drawnWidth = CGFloat(image.width) * scale
    let drawnHeight = CGFloat(image.height) * scale
    let rect = CGRect(
        x: (canvasWidth - drawnWidth) / 2,
        y: (canvasHeight - drawnHeight) / 2,
        width: drawnWidth,
        height: drawnHeight
    )

    context.saveGState()
    context.setAlpha(alpha)
    context.interpolationQuality = .high
    context.draw(image, in: rect)
    context.restoreGState()
}

private func drawCaption(_ caption: String, in context: CGContext, alpha: CGFloat) {
    guard alpha > 0.001 else { return }
    let panel = CGRect(x: 118, y: 32, width: 1684, height: 92)
    context.saveGState()
    context.setAlpha(alpha)
    context.setFillColor(CGColor(red: 0.02, green: 0.055, blue: 0.075, alpha: 0.92))
    context.addPath(
        CGPath(
            roundedRect: panel,
            cornerWidth: 17,
            cornerHeight: 17,
            transform: nil
        )
    )
    context.fillPath()
    context.setStrokeColor(CGColor(red: 0.08, green: 0.84, blue: 0.92, alpha: 0.75))
    context.setLineWidth(2)
    context.addPath(
        CGPath(
            roundedRect: panel,
            cornerWidth: 17,
            cornerHeight: 17,
            transform: nil
        )
    )
    context.strokePath()

    let font = CTFontCreateWithName("SFMono-Semibold" as CFString, 34, nil)
    let attributes: [NSAttributedString.Key: Any] = [
        kCTFontAttributeName as NSAttributedString.Key: font,
        kCTForegroundColorAttributeName as NSAttributedString.Key:
            CGColor(red: 0.88, green: 0.97, blue: 1, alpha: 1),
    ]
    let line = CTLineCreateWithAttributedString(
        NSAttributedString(string: caption, attributes: attributes)
    )
    context.textPosition = CGPoint(x: 150, y: 60)
    CTLineDraw(line, context)
    context.restoreGState()
}

private func render(
    images: [CGImage],
    output: URL
) throws {
    let manager = FileManager.default
    try manager.createDirectory(
        at: output.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    let temporary = output.deletingLastPathComponent().appendingPathComponent(
        ".visual-demo-\(UUID().uuidString).mp4"
    )
    defer { try? manager.removeItem(at: temporary) }

    let writer = try AVAssetWriter(outputURL: temporary, fileType: .mp4)
    let settings: [String: Any] = [
        AVVideoCodecKey: AVVideoCodecType.h264,
        AVVideoWidthKey: width,
        AVVideoHeightKey: height,
        AVVideoCompressionPropertiesKey: [
            AVVideoAverageBitRateKey: 16_000_000,
            AVVideoProfileLevelKey: AVVideoProfileLevelH264HighAutoLevel,
            AVVideoExpectedSourceFrameRateKey: framesPerSecond,
            AVVideoMaxKeyFrameIntervalKey: framesPerSecond * 3,
        ],
    ]
    let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
    input.expectsMediaDataInRealTime = false
    let attributes: [String: Any] = [
        kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        kCVPixelBufferWidthKey as String: width,
        kCVPixelBufferHeightKey as String: height,
        kCVPixelBufferCGImageCompatibilityKey as String: true,
        kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
    ]
    let adaptor = AVAssetWriterInputPixelBufferAdaptor(
        assetWriterInput: input,
        sourcePixelBufferAttributes: attributes
    )
    guard writer.canAdd(input) else {
        throw RenderError.video("AVAssetWriter rejected the video input")
    }
    writer.add(input)
    guard writer.startWriting() else {
        throw RenderError.video(writer.error?.localizedDescription ?? "cannot start writer")
    }
    writer.startSession(atSourceTime: .zero)

    let framesPerScene = Int(secondsPerScene * Double(framesPerSecond))
    let fadeFrames = Int(crossfadeSeconds * Double(framesPerSecond))
    let totalFrames = framesPerScene * images.count
    let colorSpace = CGColorSpaceCreateDeviceRGB()

    for frameIndex in 0..<totalFrames {
        while !input.isReadyForMoreMediaData {
            Thread.sleep(forTimeInterval: 0.002)
        }
        guard let pool = adaptor.pixelBufferPool else {
            throw RenderError.video("pixel-buffer pool is unavailable")
        }
        var optionalBuffer: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &optionalBuffer) == kCVReturnSuccess,
              let buffer = optionalBuffer
        else {
            throw RenderError.video("cannot allocate a pixel buffer")
        }
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        guard
            let baseAddress = CVPixelBufferGetBaseAddress(buffer),
            let context = CGContext(
                data: baseAddress,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
                space: colorSpace,
                bitmapInfo: CGBitmapInfo.byteOrder32Little.rawValue
                    | CGImageAlphaInfo.premultipliedFirst.rawValue
            )
        else {
            throw RenderError.video("cannot create the frame context")
        }

        context.setFillColor(CGColor(red: 0.01, green: 0.025, blue: 0.035, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let sceneIndex = min(frameIndex / framesPerScene, images.count - 1)
        let localFrame = frameIndex % framesPerScene
        let fadeStart = framesPerScene - fadeFrames
        let hasNext = sceneIndex + 1 < images.count
        if hasNext && localFrame >= fadeStart {
            let fade = eased(
                CGFloat(localFrame - fadeStart) / CGFloat(max(fadeFrames - 1, 1))
            )
            drawImage(
                images[sceneIndex],
                in: context,
                alpha: 1
            )
            drawImage(
                images[sceneIndex + 1],
                in: context,
                alpha: fade
            )
            drawCaption(scenes[sceneIndex].caption, in: context, alpha: 1 - fade)
            drawCaption(scenes[sceneIndex + 1].caption, in: context, alpha: fade)
        } else {
            drawImage(
                images[sceneIndex],
                in: context,
                alpha: 1
            )
            drawCaption(scenes[sceneIndex].caption, in: context, alpha: 1)
        }

        let presentationTime = CMTime(
            value: CMTimeValue(frameIndex),
            timescale: framesPerSecond
        )
        guard adaptor.append(buffer, withPresentationTime: presentationTime) else {
            throw RenderError.video(writer.error?.localizedDescription ?? "cannot append frame")
        }
    }

    input.markAsFinished()
    let finished = DispatchSemaphore(value: 0)
    writer.finishWriting { finished.signal() }
    finished.wait()
    guard writer.status == .completed else {
        throw RenderError.video(writer.error?.localizedDescription ?? "video render failed")
    }
    if manager.fileExists(atPath: output.path) {
        try manager.removeItem(at: output)
    }
    try manager.moveItem(at: temporary, to: output)
}

do {
    let paths = try arguments()
    let images = try scenes.map { scene in
        try loadImage(paths.screenshots.appendingPathComponent(scene.filename))
    }
    try render(images: images, output: paths.output)
    print(paths.output.path)
} catch {
    FileHandle.standardError.write(Data("visual-demo video: \(error)\n".utf8))
    exit(1)
}
