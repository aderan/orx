import Foundation

/// Errors from the raw newline-delimited Unix socket transport.
public enum TransportError: Error, Equatable, CustomStringConvertible {
    /// The socket path does not exist / nobody is listening.
    case unreachable(path: String, detail: String)
    /// Read or write exceeded the configured timeout.
    case timeout(String)
    /// The peer closed the connection mid-exchange.
    case closedByPeer
    /// A lower-level POSIX failure.
    case io(String)

    public var description: String {
        switch self {
        case let .unreachable(path, detail):
            return "engine socket \(path) unreachable: \(detail)"
        case .timeout(let what):
            return "timed out waiting for \(what)"
        case .closedByPeer:
            return "engine closed the connection"
        case .io(let detail):
            return "socket error: \(detail)"
        }
    }
}

/// A buffered newline-delimited reader/writer over an AF_UNIX stream
/// socket — the engine's transport contract (design §7: user-private
/// Unix socket, newline-delimited JSON, no HTTP port).
///
/// The transport is deliberately synchronous with send/receive timeouts;
/// callers run it off the main thread (see `EngineClient` async wrappers).
/// One transport serves one request at a time.
public final class UnixSocketLineTransport {
    private let path: String
    private var fd: Int32 = -1
    private var buffer = Data()
    private let lock = NSLock()

    public private(set) var isOpen = false

    /// - Parameters:
    ///   - path: filesystem path of the Unix domain socket.
    ///   - timeout: per-operation send/receive timeout in seconds.
    public init(path: String, timeout: TimeInterval = 5.0) {
        self.path = path
        self.timeout = timeout
    }

    deinit { close() }

    private let timeout: TimeInterval

    /// Connect to the socket. Throws `.unreachable` when nothing is
    /// listening — the app treats that as "engine not running", never
    /// as data corruption.
    public func connect() throws {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else {
            throw TransportError.io("socket() failed: \(String(cString: strerror(errno)))")
        }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8)
        guard pathBytes.count < MemoryLayout.size(ofValue: addr.sun_path) else {
            close(fd: fd)
            throw TransportError.io("socket path too long")
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { dest in
            dest.copyBytes(from: pathBytes)
        }
        let len = socklen_t(MemoryLayout<sockaddr_un>.size(ofValue: addr))
        let rc = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                Darwin.connect(fd, sa, len)
            }
        }
        guard rc == 0 else {
            let detail = String(cString: strerror(errno))
            close(fd: fd)
            throw TransportError.unreachable(path: path, detail: detail)
        }
        try setTimeoutOptions(fd: fd)
        self.fd = fd
        isOpen = true
    }

    public func close() {
        lock.lock()
        defer { lock.unlock() }
        close(fd: fd)
        fd = -1
        isOpen = false
    }

    private func close(fd: Int32) {
        guard fd >= 0 else { return }
        Darwin.close(fd)
    }

    private func setTimeoutOptions(fd: Int32) throws {
        let tv = timeval(tv_sec: time_t(timeout),
                         tv_usec: suseconds_t((timeout.truncatingRemainder(dividingBy: 1)) * 1_000_000))
        var tvCopy = tv
        let rc = withUnsafePointer(to: &tvCopy) { ptr in
            ptr.withMemoryRebound(to: timeval.self, capacity: 1) {
                setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, $0, socklen_t(MemoryLayout<timeval>.size))
            }
        }
        _ = rc
        var sndCopy = tv
        let rc2 = withUnsafePointer(to: &sndCopy) { ptr in
            ptr.withMemoryRebound(to: timeval.self, capacity: 1) {
                setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, $0, socklen_t(MemoryLayout<timeval>.size))
            }
        }
        _ = rc2
        if rc != 0 || rc2 != 0 {
            throw TransportError.io("setsockopt(SO_RCVTIMEO/SNDTIMEO) failed")
        }
    }

    /// Send one line (newline appended here).
    public func sendLine(_ data: Data) throws {
        var payload = data
        payload.append(0x0A)
        try payload.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            var sent = 0
            while sent < raw.count {
                let n = write(fd, raw.baseAddress!.advanced(by: sent), raw.count - sent)
                if n > 0 { sent += n; continue }
                if n == 0 { throw TransportError.closedByPeer }
                if errno == EINTR { continue }
                if errno == EAGAIN || errno == EWOULDBLOCK {
                    throw TransportError.timeout("sending request")
                }
                throw TransportError.io("write failed: \(String(cString: strerror(errno)))")
            }
        }
    }

    /// Read one newline-terminated line (newline stripped). Blocks until a
    /// full line arrives, the timeout hits, or the peer closes.
    public func receiveLine() throws -> Data {
        while true {
            if let nl = buffer.firstIndex(of: 0x0A) {
                let line = buffer[buffer.startIndex..<nl]
                let value = Data(line)
                buffer.removeSubrange(buffer.startIndex...nl)
                return value
            }
            var chunk = [UInt8](repeating: 0, count: 65_536)
            let n = chunk.withUnsafeMutableBufferPointer { buf in
                read(fd, buf.baseAddress, buf.count)
            }
            if n > 0 {
                buffer.append(contentsOf: chunk[0..<n])
                continue
            }
            if n == 0 {
                // Peer closed: half a line (or nothing) remains.
                if buffer.isEmpty { throw TransportError.closedByPeer }
                let rest = buffer
                buffer.removeAll()
                return rest
            }
            if errno == EINTR { continue }
            if errno == EAGAIN || errno == EWOULDBLOCK {
                throw TransportError.timeout("reading response")
            }
            throw TransportError.io("read failed: \(String(cString: strerror(errno)))")
        }
    }
}
