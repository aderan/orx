import Foundation
import SQLite3

enum SQLiteBackup {
    private struct Failure: LocalizedError {
        var errorDescription: String?
        init(_ detail: String) { errorDescription = detail }
    }

    static func create(from source: URL, to destination: URL) throws {
        var input: OpaquePointer?
        defer { sqlite3_close(input) }
        guard sqlite3_open_v2(source.path, &input, SQLITE_OPEN_READONLY, nil) == SQLITE_OK else {
            throw failure("打开备份源", input)
        }
        var output: OpaquePointer?
        defer { sqlite3_close(output) }
        guard sqlite3_open_v2(destination.path, &output,
                              SQLITE_OPEN_CREATE | SQLITE_OPEN_READWRITE, nil) == SQLITE_OK else {
            throw failure("创建备份", output)
        }
        guard let backup = sqlite3_backup_init(output, "main", input, "main") else {
            throw failure("初始化 SQLite 备份", output)
        }
        let deadline = ProcessInfo.processInfo.systemUptime + 5
        var result = sqlite3_backup_step(backup, -1)
        while (result == SQLITE_BUSY || result == SQLITE_LOCKED)
                && ProcessInfo.processInfo.systemUptime < deadline {
            sqlite3_sleep(50)
            result = sqlite3_backup_step(backup, -1)
        }
        let finished = sqlite3_backup_finish(backup)
        guard result == SQLITE_DONE, finished == SQLITE_OK else {
            throw failure("复制 SQLite 一致性快照", output)
        }
        guard sqlite3_exec(output, "PRAGMA journal_mode=DELETE", nil, nil, nil) == SQLITE_OK else {
            throw failure("完成独立备份文件", output)
        }
        var statement: OpaquePointer?
        defer { sqlite3_finalize(statement) }
        guard sqlite3_prepare_v2(output, "PRAGMA quick_check", -1, &statement, nil) == SQLITE_OK,
              sqlite3_step(statement) == SQLITE_ROW,
              let resultText = sqlite3_column_text(statement, 0),
              String(cString: resultText) == "ok",
              sqlite3_step(statement) == SQLITE_DONE else {
            throw failure("核对 SQLite 备份完整性", output)
        }
    }

    private static func failure(_ operation: String, _ database: OpaquePointer?) -> Failure {
        let message = database.map { String(cString: sqlite3_errmsg($0)) } ?? "SQLite 不可用"
        return Failure("\(operation)失败：\(message)")
    }
}
