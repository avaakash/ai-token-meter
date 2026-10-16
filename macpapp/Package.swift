// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "TokenMeter",
    platforms: [.macOS(.v13)],
    targets: [
        .target(
            name: "UsageCore",
            linkerSettings: [.linkedLibrary("sqlite3")]
        ),
        .executableTarget(
            name: "TokenMeter",
            dependencies: ["UsageCore"]
        ),
        .executableTarget(
            name: "usage-cli",
            dependencies: ["UsageCore"]
        ),
        // XCTest/swift-testing aren't usable with Command Line Tools alone, so
        // the checks are a plain executable that exits non-zero on failure.
        .executableTarget(
            name: "usage-core-checks",
            dependencies: ["UsageCore"]
        ),
    ]
)
