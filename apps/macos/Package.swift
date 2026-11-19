// swift-tools-version:5.9
// ORX Analytics menu-bar app (design docs/menubar-app-design.md §7/§9).
// Xcode-free build chain: `swift build` / `swift test` / scripts/*.sh.
import Foundation
import PackageDescription

// The executable is a menu-bar-only app (LSUIElement). SwiftPM builds a
// bare executable, so the Info.plist is embedded into the __TEXT,__info_plist
// section; the Stage-5 .app bundle reuses the same plist file.
let packageRoot = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
let embeddedInfoPlist = packageRoot
    .appendingPathComponent("Resources/Info.plist").path

let package = Package(
    name: "ORXAnalytics",
    platforms: [.macOS(.v13)],
    targets: [
        .target(
            name: "ORXAnalyticsCore",
            path: "Sources/ORXAnalyticsCore"
        ),
        .executableTarget(
            name: "ORXAnalyticsApp",
            dependencies: ["ORXAnalyticsCore"],
            path: "Sources/ORXAnalyticsApp",
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", embeddedInfoPlist,
                ])
            ]
        ),
        .testTarget(
            name: "ORXAnalyticsCoreTests",
            dependencies: ["ORXAnalyticsCore"],
            path: "Tests/ORXAnalyticsCoreTests"
        ),
    ]
)
