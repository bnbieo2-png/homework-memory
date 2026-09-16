import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count == 2 else {
    fputs("usage: detect_image_orientation.swift IMAGE\n", stderr)
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
guard let image = NSImage(contentsOf: imageURL),
      let data = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: data),
      let cgImage = bitmap.cgImage else {
    fputs("cannot load image\n", stderr)
    exit(3)
}

let orientations: [(String, CGImagePropertyOrientation, Int)] = [
    ("up", .up, 0),
    ("right", .right, 90),
    ("down", .down, 180),
    ("left", .left, 270),
]

var candidates: [[String: Any]] = []
for (name, orientation, rotation) in orientations {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    let handler = VNImageRequestHandler(cgImage: cgImage, orientation: orientation)
    do {
        try handler.perform([request])
        let observations = request.results ?? []
        let textCandidates = observations.compactMap { $0.topCandidates(1).first }
        let characters = textCandidates.reduce(0) { $0 + $1.string.count }
        let confidence = textCandidates.reduce(0.0) { $0 + Double($1.confidence) }
        let horizontalLines = observations.filter {
            $0.boundingBox.width > $0.boundingBox.height * 2
        }.count
        let lineShapeScore = observations.reduce(0.0) {
            $0 + min(20.0, Double($1.boundingBox.width / max($1.boundingBox.height, 0.001)))
        }
        candidates.append([
            "orientation": name,
            "rotation": rotation,
            "characters": characters,
            "horizontalLines": horizontalLines,
            "score": lineShapeScore + confidence,
        ])
    } catch {
        continue
    }
}

let sorted = candidates.sorted {
    ($0["score"] as? Double ?? 0) > ($1["score"] as? Double ?? 0)
}
let best = sorted.first ?? [
    "orientation": "unknown", "rotation": 0, "characters": 0,
    "horizontalLines": 0, "score": 0.0,
]
let runnerUpScore = sorted.dropFirst().first?["score"] as? Double ?? 0
let bestScore = best["score"] as? Double ?? 0
let bestRotation = best["rotation"] as? Int ?? 0
let minimumCharacters = bestRotation == 180 ? 30 : 12
let minimumHorizontalLines = bestRotation == 180 ? 2 : 1
let reliable = (best["characters"] as? Int ?? 0) >= minimumCharacters
    && (best["horizontalLines"] as? Int ?? 0) >= minimumHorizontalLines
    && bestScore - runnerUpScore >= 0.8

let result: [String: Any] = [
    "orientation": best["orientation"] ?? "unknown",
    "rotation": reliable ? bestRotation : 0,
    "reliable": reliable,
    "scoreMargin": bestScore - runnerUpScore,
]
let payload = try JSONSerialization.data(withJSONObject: result)
print(String(data: payload, encoding: .utf8)!)
