"""Render the production task evidence section offscreen without changing user UI.

Usage: python render_evidence.py <checkout> <tasks.get.json> <empty-work-dir>
The copy changes only the entry point and root body to expose the unchanged
production taskBreakdown method. This verifies section layout, not Finder,
login items, real window interaction or an installed app's lifecycle.
"""
from pathlib import Path
import shutil
import subprocess
import sys

source, payload, work = (Path(p).resolve() for p in sys.argv[1:])
work.mkdir(parents=True, exist_ok=False)
package = work / 'macos'
shutil.copytree(source/'apps/macos', package,
                ignore=shutil.ignore_patterns('.build', 'dist', '.swiftpm'))
entry = package/'Sources/ORXAnalyticsApp/ORXAnalyticsApp.swift'
entry.write_text(entry.read_text().replace('@main\nstruct ORXAnalyticsApp', 'struct ORXAnalyticsApp'))
view = package/'Sources/ORXAnalyticsApp/RunDetailView.swift'
text = view.read_text()
start = text.index('    var body: some View {', text.index('struct RunDetailView: View'))
# The existing root body ends before the first private view/helper declaration.
end_candidates = [text.find(marker, start+1) for marker in ['\n    @ViewBuilder', '\n    private func ', '\n    private var ', '\n    // MARK:']]
end = min(i for i in end_candidates if i != -1)
text = text[:start]+'''    var body: some View {
        taskBreakdown(EvidenceCapture.selection, EvidenceCapture.detail)
            .padding(ORXSpace.md)
            .orxSurface()
    }
''' + text[end:]
view.write_text(text)
helper = package/'Sources/ORXAnalyticsApp/EvidenceCapture.swift'
helper.write_text(r'''
import AppKit
import SwiftUI
import ORXAnalyticsCore

@main @MainActor struct EvidenceCapture {
    static var detail: TaskDetailEnvelope!
    static var selection: SelectedTaskRef!
    static func main() throws {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)
        let input = URL(fileURLWithPath: CommandLine.arguments[1])
        let output = URL(fileURLWithPath: CommandLine.arguments[2])
        detail = try JSONDecoder().decode(TaskDetailEnvelope.self, from: Data(contentsOf: input))
        selection = SelectedTaskRef(revisionID: 1, taskID: "T001", objective: "Evidence layout acceptance")
        let client = EngineSocketClient(socketPath: "/tmp/orx-render-unused.sock")
        let model = AppModel(client: client, cache: OverviewCacheStore(directory: output.appendingPathComponent("cache")))
        for dark in [false, true] {
            NSApp.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
            for width in [380.0, 760.0] {
                let root = RunDetailView(identity: RunIdentity(project: "/tmp/isolated-render", runID: "R001"), client: client, page: .execution)
                    .environmentObject(model)
                    .environment(\.colorScheme, dark ? .dark : .light)
                    .frame(width: width, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                let host = NSHostingView(rootView: root)
                host.appearance = NSApp.appearance
                host.frame = NSRect(x: 0, y: 0, width: width, height: 2400)
                host.layoutSubtreeIfNeeded()
                let height = max(100, host.fittingSize.height)
                host.frame.size = NSSize(width: width, height: height)
                host.layoutSubtreeIfNeeded()
                guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else {
                    throw NSError(domain: "ORXRender", code: 1)
                }
                host.cacheDisplay(in: host.bounds, to: bitmap)
                let name = "evidence-\(dark ? "dark" : "light")-\(Int(width)).png"
                try bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent(name))
                print(name, width, height)
            }
        }
    }
}
''')
subprocess.run(['swift', 'build', '--package-path', str(package)], check=True)
result = subprocess.run(['swift', 'build', '--package-path', str(package), '--show-bin-path'], check=True, capture_output=True, text=True)
subprocess.run([str(Path(result.stdout.strip())/'ORXAnalyticsApp'), str(payload), str(work)], check=True)
