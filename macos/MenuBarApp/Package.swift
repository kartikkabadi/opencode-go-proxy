// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "OpenCodeGoMenuBar",
    platforms: [
        .macOS(.v13)
    ],
    targets: [
        .executableTarget(
            name: "OpenCodeGoMenuBar",
            path: "Sources/OpenCodeGoMenuBar"
        )
    ]
)
