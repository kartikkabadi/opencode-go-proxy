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
        ),
        .testTarget(
            name: "OpenCodeGoMenuBarTests",
            dependencies: ["OpenCodeGoMenuBar"],
            path: "Tests/OpenCodeGoMenuBarTests"
        )
    ]
)
