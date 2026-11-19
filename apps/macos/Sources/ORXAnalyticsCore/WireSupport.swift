import Foundation

/// Wire-tolerance helpers for decoding the engine's JSON. The Python
/// side serializes SQLite rows directly: ids arrive as INTEGER (JSON
/// numbers), boolean columns may arrive as 0/1, and a snapshot row id is
/// an int even when a display key looks like a string. These helpers
/// accept the shapes the engine actually emits — they never invent a
/// value that was not on the wire (missing stays nil).
enum WireSupport {

    /// Int from Int / whole Double / numeric String / null / absent.
    /// Used for ids the engine sends as INTEGER but that must survive
    /// fixtures that wrote them as strings.
    static func flexInt<K: CodingKey>(_ c: KeyedDecodingContainer<K>,
                                      _ key: K) -> Int? {
        if let i = (try? c.decodeIfPresent(Int.self, forKey: key)) ?? nil {
            return i
        }
        if let d = (try? c.decodeIfPresent(Double.self, forKey: key)) ?? nil {
            return Int(exactly: d)
        }
        if let s = (try? c.decodeIfPresent(String.self, forKey: key)) ?? nil {
            return Int(s)
        }
        return nil
    }

    /// Bool from Bool / 0 / 1 / null (SQLite INTEGER columns serialize
    /// as JSON numbers).
    static func flexBool<K: CodingKey>(_ c: KeyedDecodingContainer<K>,
                                       _ key: K) -> Bool? {
        if let b = (try? c.decodeIfPresent(Bool.self, forKey: key)) ?? nil {
            return b
        }
        if let i = (try? c.decodeIfPresent(Int.self, forKey: key)) ?? nil {
            switch i {
            case 0: return false
            case 1: return true
            default: return nil
            }
        }
        return nil
    }
}
