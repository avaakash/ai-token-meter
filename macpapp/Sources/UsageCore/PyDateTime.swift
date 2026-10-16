import Foundation

/// An aware timestamp that behaves like Python's `datetime` where it matters
/// for sharing a database with the Python dashboard: microsecond precision,
/// `fromisoformat`-style parsing, and `isoformat()` output in the UTC offset
/// the value was created with. Stored timestamps are compared as strings in
/// SQL, so both implementations must write byte-identical text.
public struct PyDateTime: Comparable, Hashable, CustomStringConvertible {
    public var micros: Int64
    public var offset: Int

    public init(micros: Int64, offset: Int = 0) {
        self.micros = micros
        self.offset = offset
    }

    public static func == (a: PyDateTime, b: PyDateTime) -> Bool { a.micros == b.micros }
    public static func < (a: PyDateTime, b: PyDateTime) -> Bool { a.micros < b.micros }
    public func hash(into hasher: inout Hasher) { hasher.combine(micros) }
    public var description: String { isoformat() }

    /// Overridable so checks can pin "now".
    public static var clock: () -> Date = { Date() }

    public static func now() -> PyDateTime {
        PyDateTime(micros: Int64((clock().timeIntervalSince1970 * 1_000_000).rounded(.down)))
    }

    public static func fromTimestamp(_ seconds: Double) -> PyDateTime {
        PyDateTime(micros: Int64((seconds * 1_000_000).rounded(.toNearestOrEven)))
    }

    public var date: Date { Date(timeIntervalSince1970: Double(micros) / 1_000_000) }

    public func adding(micros delta: Int64) -> PyDateTime { PyDateTime(micros: micros + delta, offset: offset) }
    public func adding(seconds: Double) -> PyDateTime {
        adding(micros: Int64((seconds * 1_000_000).rounded(.toNearestOrEven)))
    }
    public func adding(hours: Double) -> PyDateTime { adding(seconds: hours * 3600) }
    public func adding(days: Int) -> PyDateTime { adding(micros: Int64(days) * Micros.day) }

    public func seconds(since other: PyDateTime) -> Double { Double(micros - other.micros) / 1_000_000 }

    // MARK: Wall-clock components in this value's own offset

    private var wallMicros: Int64 { micros + Int64(offset) * Micros.second }

    var wall: (year: Int, month: Int, day: Int, hour: Int, minute: Int, second: Int, micro: Int) {
        let w = wallMicros
        let days = floorDiv(w, Micros.day)
        let rem = w - days * Micros.day
        let (y, m, d) = civilFromDays(days)
        let secs = rem / Micros.second
        return (y, m, d, Int(secs / 3600), Int(secs % 3600 / 60), Int(secs % 60), Int(rem % Micros.second))
    }

    /// Python's `dt.replace(...)` that zeroes everything below `unit`, done
    /// on wall-clock time so it matches regardless of offset.
    func flooredWall(to unit: Int64) -> PyDateTime {
        let w = wallMicros
        return PyDateTime(micros: floorDiv(w, unit) * unit - Int64(offset) * Micros.second, offset: offset)
    }

    /// `isoformat()` — fraction omitted when zero, offset always present.
    public func isoformat() -> String {
        let c = wall
        var s = String(format: "%04d-%02d-%02dT%02d:%02d:%02d", c.year, c.month, c.day, c.hour, c.minute, c.second)
        if c.micro != 0 { s += String(format: ".%06d", c.micro) }
        let sign = offset < 0 ? "-" : "+"
        let a = abs(offset)
        s += String(format: "%@%02d:%02d", sign, a / 3600, a % 3600 / 60)
        if a % 60 != 0 { s += String(format: ":%02d", a % 60) }
        return s
    }

    /// `strftime("%Y-%m-%d")` in this value's own offset.
    public func dateString() -> String {
        let c = wall
        return String(format: "%04d-%02d-%02d", c.year, c.month, c.day)
    }

    /// Same instant, expressed in the machine's local time zone.
    public func toLocal() -> PyDateTime {
        PyDateTime(micros: micros, offset: TimeZone.current.secondsFromGMT(for: date))
    }

    /// (day, weekday with Monday = 0, hour), all from one local conversion.
    public func localDayFields() -> (day: String, weekday: Int, hour: Int) {
        let local = toLocal()
        let days = floorDiv(local.wallMicros, Micros.day)
        let weekday = Int(((days + 3) % 7 + 7) % 7)  // 1970-01-01 was a Thursday
        return (local.dateString(), weekday, local.wall.hour)
    }

    // MARK: Parsing

    /// `datetime.fromisoformat(s.replace("Z", "+00:00"))`, returning nil where
    /// Python would raise. A value without an offset is taken as local time
    /// (what `astimezone()` assumes), or as UTC when `naiveIsUTC` is set.
    public static func parse(_ string: String?, naiveIsUTC: Bool = false) -> PyDateTime? {
        guard let string else { return nil }
        let b = Array(string.utf8)
        func digits(_ start: Int, _ count: Int) -> Int? {
            guard start + count <= b.count else { return nil }
            var v = 0
            for i in start..<(start + count) {
                guard b[i] >= 48 && b[i] <= 57 else { return nil }
                v = v * 10 + Int(b[i] - 48)
            }
            return v
        }
        guard b.count >= 10, let year = digits(0, 4), b[4] == 45, let month = digits(5, 2), b[7] == 45,
              let day = digits(8, 2) else { return nil }
        guard (1...12).contains(month), day >= 1, day <= daysInMonth(year, month) else { return nil }

        var hour = 0, minute = 0, second = 0, micro = 0
        var tzOffset: Int? = nil
        var i = 10
        if i < b.count {
            i += 1  // any single separator character
            guard let h = digits(i, 2) else { return nil }
            hour = h; i += 2
            if i < b.count, b[i] == 58 {
                guard let m = digits(i + 1, 2) else { return nil }
                minute = m; i += 3
                if i < b.count, b[i] == 58 {
                    guard let sec = digits(i + 1, 2) else { return nil }
                    second = sec; i += 3
                    if i < b.count, b[i] == 46 || b[i] == 44 {
                        i += 1
                        var n = 0, frac = 0
                        while i < b.count, b[i] >= 48 && b[i] <= 57 {
                            if n < 6 { frac = frac * 10 + Int(b[i] - 48) }
                            n += 1; i += 1
                        }
                        guard n > 0 else { return nil }
                        for _ in n..<max(n, 6) { frac *= 10 }
                        micro = frac
                    }
                }
            }
            if i < b.count {
                if b[i] == 90 && i == b.count - 1 {
                    tzOffset = 0; i += 1
                } else if b[i] == 43 || b[i] == 45 {
                    let sign = b[i] == 45 ? -1 : 1
                    guard let oh = digits(i + 1, 2) else { return nil }
                    var j = i + 3, om = 0, os = 0
                    if j < b.count, b[j] == 58 {
                        guard let m = digits(j + 1, 2) else { return nil }
                        om = m; j += 3
                        if j < b.count, b[j] == 58 {
                            guard let s = digits(j + 1, 2) else { return nil }
                            os = s; j += 3
                        }
                    } else if let m = digits(j, 2) {
                        om = m; j += 2
                    }
                    guard j == b.count, oh < 24, om < 60, os < 60 else { return nil }
                    tzOffset = sign * (oh * 3600 + om * 60 + os)
                    i = j
                } else {
                    return nil
                }
            }
            guard i == b.count else { return nil }
        }
        guard hour < 24, minute < 60, second < 60 else { return nil }

        let wall = daysFromCivil(year, month, day) * Micros.day
            + Int64(hour * 3600 + minute * 60 + second) * Micros.second + Int64(micro)
        if let tzOffset {
            return PyDateTime(micros: wall - Int64(tzOffset) * Micros.second, offset: tzOffset)
        }
        if naiveIsUTC { return PyDateTime(micros: wall) }
        let tz = TimeZone.current
        let guess = tz.secondsFromGMT(for: Date(timeIntervalSince1970: Double(wall) / 1_000_000))
        let off = tz.secondsFromGMT(for: Date(timeIntervalSince1970: Double(wall - Int64(guess) * Micros.second) / 1_000_000))
        return PyDateTime(micros: wall - Int64(off) * Micros.second, offset: off)
    }
}

public enum Micros {
    public static let second: Int64 = 1_000_000
    public static let minute: Int64 = 60 * second
    public static let hour: Int64 = 60 * minute
    public static let day: Int64 = 24 * hour
}

func floorDiv(_ a: Int64, _ b: Int64) -> Int64 {
    let q = a / b
    return (a % b != 0 && (a < 0) != (b < 0)) ? q - 1 : q
}

/// `datetime.now().strftime("%Y-%m-%d")`, optionally shifted by whole days of
/// naive local wall-clock time (Python's naive `now() - timedelta(days=n)`).
func localDateString(daysAgo: Int = 0) -> String {
    PyDateTime.now().toLocal().adding(days: -daysAgo).dateString()
}

private func daysInMonth(_ year: Int, _ month: Int) -> Int {
    switch month {
    case 2: return (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 ? 29 : 28
    case 4, 6, 9, 11: return 30
    default: return 31
    }
}

private func civilFromDays(_ z0: Int64) -> (Int, Int, Int) {
    let z = z0 + 719_468
    let era = (z >= 0 ? z : z - 146_096) / 146_097
    let doe = z - era * 146_097
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100)
    let mp = (5 * doy + 2) / 153
    let d = doy - (153 * mp + 2) / 5 + 1
    let m = mp < 10 ? mp + 3 : mp - 9
    return (Int(yoe + era * 400 + (m <= 2 ? 1 : 0)), Int(m), Int(d))
}

private func daysFromCivil(_ year: Int, _ month: Int, _ day: Int) -> Int64 {
    let y = Int64(month <= 2 ? year - 1 : year)
    let era = (y >= 0 ? y : y - 399) / 400
    let yoe = y - era * 400
    let mp = Int64(month > 2 ? month - 3 : month + 9)
    let doy = (153 * mp + 2) / 5 + Int64(day) - 1
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy
    return era * 146_097 + doe - 719_468
}
