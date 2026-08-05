// DJ Claude notification helper — the launchd-safe event source.
// Music.app posts com.apple.Music.playerInfo on every track/state change;
// observing it needs no Apple events and no TCC grant, which matters here
// because launchd-context Apple events to Music hang on this box.
// Prints one JSON line per notification. Compile:
//   swiftc -O djnotify.swift -o djnotify
import Foundation

let center = DistributedNotificationCenter.default()
center.addObserver(
    forName: NSNotification.Name("com.apple.Music.playerInfo"),
    object: nil, queue: nil
) { note in
    var out: [String: Any] = ["at": Date().timeIntervalSince1970]
    if let info = note.userInfo {
        for (key, value) in info {
            let k = String(describing: key)
            if value is NSNumber || value is String {
                out[k] = value
            } else {
                out[k] = String(describing: value)
            }
        }
    }
    if let data = try? JSONSerialization.data(withJSONObject: out),
       let line = String(data: data, encoding: .utf8) {
        print(line)
        fflush(stdout)
    }
}
RunLoop.main.run()
