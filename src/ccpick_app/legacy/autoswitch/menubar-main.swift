
import Cocoa

let STALE_SECONDS: TimeInterval = 10 * 60

func checkInterval(_ snap: Snapshot) -> (base: TimeInterval, jitter: TimeInterval) {
    if snap.state == "switched" { return (45, 10) }
    if let n = snap.nextCheckS, n > 0 {
        return (min(max(n, 20), 300), min(15, max(3, n * 0.2)))
    }
    guard let used = snap.usedPct else { return (60, 20) }   // 读不到就按较紧的来
    let assumedRate = 20.0                       // 百分点/分钟, 2026-09-05 实测峰值
    let assumedEta = max(0, 100 - used) / assumedRate * 60
    return (min(max(assumedEta / 4, 20), 300), 15)
}

let statusPath = ("~/Library/Logs/claude-autoswitch-status.json" as NSString)
    .expandingTildeInPath
let logPath = ("~/Library/Logs/claude-account-autoswitch.log" as NSString)
    .expandingTildeInPath

struct Snapshot {
    var state: String        // ok / switched / blocked / error / offline / stalled / missing
    var message: String
    var extra: String
    var age: TimeInterval?   // 状态文件多旧
    var usedPct: Double?     // 最紧那道闸已用 % —— 决定下一轮多久后查
    var win5h: Double?       // 5 小时窗口 已用 %
    var win7d: Double?       // 7 天窗口   已用 %
    var winModel: Double?    // 每模型 (Fable 等) 周窗口 已用 %
    var cooling: Bool        // 刚切过, cswap 在冷却期内拒绝再切
    var binding: String? = nil       // 最紧那道算数的闸: 5h / 7d / Fable…
    var modelName: String? = nil     // 每模型窗口的真名 (原来写死成「Opus 周」, 那是 09-15 订正过的误归因)
    var modelCounted: Bool? = nil    // 每模型窗口算不算数
    var nextCheckS: Double?  // 决策脚本要求的下一轮间隔(秒)
    var etaS: Double?        // 按当前速率还有多少秒撞到 100%
    var burnRate: Double?    // 燃烧速率, 百分点/分钟
}

func readSnapshot(_ path: String = statusPath) -> Snapshot {
    guard let data = FileManager.default.contents(atPath: path),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        return Snapshot(state: "missing",
                        message: "还没有状态",
                        extra: "后台任务一轮都没跑完；装好后第一轮最多等 10 分钟",
                        age: nil, usedPct: nil, win5h: nil, win7d: nil,
                        winModel: nil, cooling: false,
                        nextCheckS: nil, etaS: nil, burnRate: nil)
    }
    let ts = (obj["ts"] as? Double) ?? 0
    let age = ts > 0 ? Date().timeIntervalSince1970 - ts : nil
    var state = (obj["state"] as? String) ?? "missing"
    let message = (obj["message"] as? String) ?? ""
    let extra = (obj["extra"] as? String) ?? ""

    if let age = age, age > STALE_SECONDS { state = "stalled" }
    return Snapshot(state: state, message: message, extra: extra, age: age,
                    usedPct: obj["usedPct"] as? Double,
                    win5h: obj["win5h"] as? Double,
                    win7d: obj["win7d"] as? Double,
                    winModel: obj["winModel"] as? Double,
                    cooling: (obj["cooling"] as? Bool) ?? false,
                    binding: obj["binding"] as? String,
                    modelName: obj["modelName"] as? String,
                    modelCounted: obj["modelCounted"] as? Bool,
                    nextCheckS: obj["nextCheckS"] as? Double,
                    etaS: obj["etaS"] as? Double,
                    burnRate: obj["burnRate"] as? Double)
}

func headerWindowsText(_ snap: Snapshot) -> String? {
    let modelLabel = (snap.modelName.map { "\($0) 周" } ?? "模型周")
        + (snap.modelCounted == false ? "（不计入）" : "")
    let windows: [(key: String, label: String, v: Double?)] = [
        ("5h", "5 小时", snap.win5h), ("7d", "7 天", snap.win7d), ("model", modelLabel, snap.winModel),
    ]
    guard windows.contains(where: { $0.v != nil }) else { return nil }
    var star: String?
    if let b = snap.binding, !b.isEmpty {
        star = (b == "5h" || b == "7d") ? b : "model"
    } else {
        star = windows.filter { $0.v != nil && !($0.key == "model" && snap.modelCounted == false) }
            .max(by: { ($0.v ?? 0) < ($1.v ?? 0) })?.key
    }
    return windows.map { w -> String in
        guard let v = w.v else { return "\(w.label) —" }
        return "\(w.label) \(Int(v.rounded()))%" + (w.key == star ? "★" : "")
    }.joined(separator: "   ")
}

func icon(for state: String) -> (String, [NSColor]) {
    switch state {
    case "ok":       return ("checkmark.circle.fill", [.systemGreen, .systemTeal])
    case "switched": return ("arrow.triangle.2.circlepath.circle.fill", [.systemBlue, .systemCyan])
    case "blocked":  return ("exclamationmark.triangle.fill", [.systemYellow, .systemRed])
    case "error":    return ("xmark.octagon.fill", [.systemRed, .systemPink])
    case "offline":  return ("wifi.exclamationmark.circle.fill", [.systemOrange, .systemYellow])
    case "cooling":  return ("hourglass.circle.fill", [.systemTeal, .systemBlue])
    case "stalled":  return ("clock.badge.exclamationmark.fill", [.systemPurple, .systemIndigo])
    default:         return ("questionmark.circle.fill", [.systemGray, .systemBrown])
    }
}

func headline(for s: Snapshot) -> String {
    switch s.state {
    case "ok":       return "自动切账号：在盯着"
    case "switched": return "自动切账号：刚切过"
    case "blocked":  return "全部账号额度用尽"
    case "error":    return "出错了"
    case "offline":  return "连不上 claude.ai"
    case "cooling":  return "刚切过，冷却中"
    case "stalled":  return "★后台任务停摆了★"
    default:         return "状态未知"
    }
}

func ageText(_ age: TimeInterval?) -> String {
    guard let age = age else { return "从未更新" }
    if age < 90 { return "刚刚检查过" }
    if age < 3600 { return "\(Int(age / 60)) 分钟前检查" }
    return String(format: "%.1f 小时前检查", age / 3600)
}

extension FileManager {
    func createFile(atPath path: String, contents: String) {
        try? contents.write(toFile: path, atomically: true, encoding: .utf8)
    }
}



struct WindowRow {
    var name: String     // 5h / 7d / Fable…
    var used: Double     // 已用 %
    var at: String       // 什么时候恢复 (人话, 如 "今天 17:39")
    var counted: Bool = true   // 算不算切换判据 (helper 给; 老 helper 不给 ⇒ 当算数, 同老显示)
}

struct Account {
    var slot: String
    var email: String
    var active: Bool
    var headroom: Double?        // 最紧那道闸还剩多少 —— 只用来排序
    var windows: [WindowRow]     // ★逐窗口原样显示, 不汇总★
}

let accountFetchTimeoutS: TimeInterval = 30

func loadAccounts() -> (accounts: [Account], fetchedAt: Double?) {
    let helper = ("~/bin/claude-autoswitch-helper.py" as NSString).expandingTildeInPath
    guard FileManager.default.isReadableFile(atPath: helper) else { return ([], nil) }
    let p = Process()
    p.launchPath = "/usr/bin/python3"
    p.arguments = [helper, "accounts"]
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = FileHandle.nullDevice
    p.standardInput = FileHandle.nullDevice
    var env = ProcessInfo.processInfo.environment
    env["HOME"] = NSHomeDirectory()
    p.environment = env
    do { try p.run() } catch { return ([], nil) }

    var data = Data()
    let done = DispatchSemaphore(value: 0)
    DispatchQueue.global().async {
        data = pipe.fileHandleForReading.readDataToEndOfFile()
        done.signal()
    }
    if done.wait(timeout: .now() + accountFetchTimeoutS) == .timedOut {
        p.terminate()
        mbDebug("★loadAccounts 超时 \(Int(accountFetchTimeoutS)) 秒, 已杀子进程")
        return ([], nil)
    }
    p.waitUntilExit()
    guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let raw = obj["accounts"] as? [[String: Any]] else { return ([], nil) }
    let list = raw.map { r -> Account in
        let wins = (r["windows"] as? [[String: Any]] ?? []).map { w in
            WindowRow(name: (w["name"] as? String) ?? "?",
                      used: (w["used"] as? Double) ?? 0,
                      at: (w["at"] as? String) ?? "",
                      counted: (w["counted"] as? Bool) ?? true)
        }
        return Account(slot: (r["slot"] as? String) ?? "?",
                       email: (r["email"] as? String) ?? "?",
                       active: (r["active"] as? Bool) ?? false,
                       headroom: r["headroom"] as? Double,
                       windows: wins)
    }
    return (list, obj["fetchedAt"] as? Double)
}

func mbDebug(_ msg: String) {
    let path = NSHomeDirectory() + "/Library/Logs/claude-autoswitch-menubar.debug.log"
    let line = "\(Date()) \(msg)\n"
    if let fh = FileHandle(forWritingAtPath: path) {
        fh.seekToEndOfFile()
        fh.write(line.data(using: .utf8)!)
        try? fh.close()
    } else {
        FileManager.default.createFile(atPath: path, contents: line)
    }
}

final class Controller: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var item: NSStatusItem!
    private var timer: Timer?        // 刷图标 (5 秒)
    private var checkTimer: Timer?   // 查额度/切账号 (间隔由决策脚本给, 20~300 秒)
    private var checkGeneration = 0
    private var accountCache: (accounts: [Account], fetchedAt: Double?)?
    private var accountFetchInFlight = false

    func applicationDidFinishLaunching(_ n: Notification) {
        let me = ProcessInfo.processInfo.processIdentifier
        let mine = Bundle.main.bundleIdentifier
        let dupes = NSWorkspace.shared.runningApplications.filter {
            $0.processIdentifier != me
                && (mine != nil ? $0.bundleIdentifier == mine
                                : $0.executableURL?.lastPathComponent == "claude-autoswitch-menubar")
        }
        if !dupes.isEmpty {
            mbDebug("已有实例在跑 (pid=\(dupes.map { $0.processIdentifier })), 本实例退出")
            NSApp.terminate(nil)
            return
        }

        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.autosaveName = "io.github.tykisgod.ccpick.menubar.item"
        let placeholder = NSMenu()
        placeholder.delegate = self
        item.menu = placeholder
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        timer?.tolerance = 2      // 允许合并唤醒, 省电

        runCheck(immediate: true)
        fetchAccounts(reason: "启动")        // 预热, 让第一次打开面板就有东西

        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            self?.runCheck(immediate: true)
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { self.reportPlacement() }
    }

    private func reportPlacement() {
        guard let w = item.button?.window, let screen = NSScreen.main else { return }
        var verdict = "可见"
        if let l = screen.auxiliaryTopLeftArea, let r = screen.auxiliaryTopRightArea {
            let mid = w.frame.midX
            if mid > l.maxX && mid < r.minX { verdict = "★被刘海挡住★" }
        }
        let line = String(format: "placement x=%.0f 宽=%.0f 判定=%@ 屏宽=%.0f",
                          w.frame.minX, w.frame.width, verdict, screen.frame.width)
        FileManager.default.createFile(atPath: Self.placementPath, contents: line)
    }

    static let placementPath =
        ("~/Library/Logs/claude-autoswitch-menubar.placement" as NSString).expandingTildeInPath

    private func refresh() {
        let snap = readSnapshot()
        let (symbol, colors) = icon(for: snap.state)
        if let button = item.button {
            var img = NSImage(systemSymbolName: symbol,
                              accessibilityDescription: headline(for: snap))
            img?.isTemplate = false
            if #available(macOS 12.0, *) {
                let cfg = NSImage.SymbolConfiguration(paletteColors: colors)
                    .applying(.init(pointSize: 15, weight: .semibold))
                img = img?.withSymbolConfiguration(cfg)
            }
            button.image = img
            button.contentTintColor = nil
            button.toolTip = "\(headline(for: snap))\n\(ageText(snap.age))"
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        rebuildMenu(readSnapshot())          // 立刻返回, 面板马上弹出来
        fetchAccounts(reason: "面板打开")     // 顺手刷一份, 给下次用
    }

    private func fetchAccounts(reason: String) {
        if accountFetchInFlight { return }
        accountFetchInFlight = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let r = loadAccounts()
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.accountFetchInFlight = false
                if !r.accounts.isEmpty {
                    self.accountCache = (r.accounts, r.fetchedAt)
                }
                Self.debug("账号余量刷新(\(reason)): \(r.accounts.count) 个")
            }
        }
    }

    private func rebuildMenu(_ snap: Snapshot) {
        let menu = NSMenu()
        menu.addItem(withTitle: headline(for: snap), action: nil, keyEquivalent: "")
        menu.items.last?.isEnabled = false

        if let text = headerWindowsText(snap) {
            let mi = NSMenuItem(title: "   \(text)   （★=最紧的一道，已用）",
                                action: nil, keyEquivalent: "")
            mi.isEnabled = false
            mi.attributedTitle = NSAttributedString(
                string: mi.title,
                attributes: [.font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular)])
            menu.addItem(mi)
        }

        for line in [snap.message, snap.extra].filter({ !$0.isEmpty }) {
            let mi = NSMenuItem(title: "   \(line)", action: nil, keyEquivalent: "")
            mi.isEnabled = false
            menu.addItem(mi)
        }
        let ageItem = NSMenuItem(title: "   \(ageText(snap.age))", action: nil, keyEquivalent: "")
        ageItem.isEnabled = false
        menu.addItem(ageItem)

        if snap.state == "stalled" {
            let hint = NSMenuItem(title: "   → 用下面「立刻检查一次」试着唤醒",
                                  action: nil, keyEquivalent: "")
            hint.isEnabled = false
            menu.addItem(hint)
        }

        menu.addItem(.separator())
        let (accounts, fetched) = accountCache ?? ([], nil)
        if accounts.isEmpty {
            let mi = NSMenuItem(title: accountFetchInFlight ? "  正在读取账号余量…（下次打开就是现成的）"
                                                            : "  读不到账号余量",
                                action: nil, keyEquivalent: "")
            mi.isEnabled = false
            menu.addItem(mi)
        } else {
            let head = NSMenuItem(title: "账号（点账号名即切换）", action: nil, keyEquivalent: "")
            head.isEnabled = false
            menu.addItem(head)
            for a in accounts {
                let mark = a.active ? "● " : "   "
                let mi = NSMenuItem(title: "\(mark)\(a.email)",
                                    action: #selector(switchTo(_:)), keyEquivalent: "")
                mi.target = self
                mi.representedObject = a.email
                mi.isEnabled = !a.active
                mi.attributedTitle = NSAttributedString(
                    string: mi.title,
                    attributes: [.font: NSFont.systemFont(
                        ofSize: 12, weight: a.active ? .semibold : .regular)])
                menu.addItem(mi)
                for w in a.windows {
                    let label = w.name == "5h" ? "5 小时" : (w.name == "7d" ? "7 天  " : w.name)
                    let line = String(format: "        %@  已用 %3d%%   %@%@",
                                      label, Int(w.used.rounded()),
                                      w.at.isEmpty ? "—" : "\(w.at) 恢复",
                                      w.counted ? "" : "   （不计入切换判据）")
                    let sub = NSMenuItem(title: line, action: nil, keyEquivalent: "")
                    sub.isEnabled = false
                    sub.attributedTitle = NSAttributedString(
                        string: line,
                        attributes: [.font: NSFont.monospacedDigitSystemFont(
                            ofSize: 11, weight: .regular),
                            .foregroundColor: !w.counted ? NSColor.secondaryLabelColor
                                : (w.used >= 95 ? NSColor.systemRed
                                : (w.used >= 80 ? NSColor.systemOrange : NSColor.secondaryLabelColor))])
                    menu.addItem(sub)
                }
            }
            if let f = fetched {
                let age = Date().timeIntervalSince1970 - f
                let t = NSMenuItem(title: "  数据 \(ageText(age))，点上面「立刻检查一次」可刷新",
                                   action: nil, keyEquivalent: "")
                t.isEnabled = false
                menu.addItem(t)
            }
        }

        menu.addItem(.separator())
        menu.addItem(withTitle: "立刻检查一次（会联网刷新）", action: #selector(checkNow), keyEquivalent: "r")
            .target = self
        menu.addItem(withTitle: "打开日志", action: #selector(openLog), keyEquivalent: "l")
            .target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出（之后只剩每 15 分钟兜底检查）",
                     action: #selector(quit), keyEquivalent: "q").target = self
        menu.delegate = self
        item.menu = menu
    }

    private func scheduleNextCheck() {
        let (base, spread) = checkInterval(readSnapshot())
        let jitter = TimeInterval.random(in: -spread...spread)
        let delay = max(20, base + jitter)
        checkTimer?.invalidate()
        checkTimer = Timer.scheduledTimer(withTimeInterval: delay, repeats: false) { [weak self] _ in
            self?.runCheck(immediate: false)
        }
        checkTimer?.tolerance = 15
        Self.debug(String(format: "已排下一轮: %.0f 秒后 (基准 %.0f 抖动 ±%.0f)", delay, base, spread))
    }

    private func runCheck(immediate: Bool) {
        checkGeneration &+= 1
        let gen = checkGeneration
        let home = NSHomeDirectory()
        let script = home + "/bin/claude-account-autoswitch.sh"
        let p = Process()
        p.launchPath = "/bin/bash"
        p.arguments = [script, "--now"]
        var env = ProcessInfo.processInfo.environment
        env["HOME"] = home
        if (env["PATH"] ?? "").isEmpty { env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin" }
        p.environment = env
        p.standardOutput = FileHandle.nullDevice
        let errLog = home + "/Library/Logs/claude-autoswitch-child.stderr.log"
        if !FileManager.default.fileExists(atPath: errLog) {
            FileManager.default.createFile(atPath: errLog, contents: "")
        }
        if let fh = FileHandle(forWritingAtPath: errLog) {
            fh.seekToEndOfFile()
            p.standardError = fh
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self = self, self.checkGeneration == gen else { return }
                Self.debug("本轮结束 (exit \(proc.terminationStatus)), 按本轮结果排下一轮")
                self.scheduleNextCheck()
                self.fetchAccounts(reason: "本轮结束")   // 顺带把面板要的余量刷新
            }
        }
        do {
            try p.run()
            Self.debug("spawn ok immediate=\(immediate) script=\(script)")
            armWatchdog(gen)
        } catch {
            Self.debug("★spawn 失败: \(error) script=\(script)")
            scheduleNextCheck()     // spawn 都没成功, 也得把下一轮排上, 否则整套停摆
        }
    }

    private func armWatchdog(_ gen: Int) {
        checkTimer?.invalidate()
        checkTimer = Timer.scheduledTimer(withTimeInterval: 300, repeats: false) { [weak self] _ in
            guard let self = self, self.checkGeneration == gen else { return }
            Self.debug("★本轮 300 秒还没收尾, 看门狗接手排下一轮")
            self.checkGeneration &+= 1
            self.scheduleNextCheck()
        }
        checkTimer?.tolerance = 15
    }

    static func debug(_ msg: String) { mbDebug(msg) }

    @objc private func checkNow() {
        runCheck(immediate: true)
    }

    @objc private func switchTo(_ sender: NSMenuItem) {
        guard let email = sender.representedObject as? String else { return }
        let cswap = ("~/.local/bin/cswap" as NSString).expandingTildeInPath
        let p = Process()
        p.launchPath = cswap
        p.arguments = ["switch", email]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { _ in
            DispatchQueue.main.async { self.checkNow() }
        }
        try? p.run()
    }

    @objc private func openLog() {
        NSWorkspace.shared.open(URL(fileURLWithPath: logPath))
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

func selfcheckPace() -> Bool {
    let dir = NSTemporaryDirectory()
        + "ccpick-pace-selfcheck-\(ProcessInfo.processInfo.processIdentifier)"
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(atPath: dir) }
    var failures: [String] = []
    let now = Date().timeIntervalSince1970

    func snap(_ body: String, _ name: String) -> Snapshot {
        let path = dir + "/" + name + ".json"
        try? "{\"ts\":\(now),\(body)}".write(toFile: path, atomically: true, encoding: .utf8)
        return readSnapshot(path)
    }
    func expect(_ label: String, _ got: TimeInterval, _ want: TimeInterval) {
        if abs(got - want) > 0.01 { failures.append("\(label): 期望 \(want) 实际 \(got)") }
    }

    expect("听脚本的 nextCheckS",
           checkInterval(snap("\"state\":\"ok\",\"usedPct\":72,\"nextCheckS\":25", "a")).base, 25)
    expect("刚落地紧盯",
           checkInterval(snap("\"state\":\"switched\",\"usedPct\":10,\"nextCheckS\":300", "b")).base, 45)
    expect("下限", checkInterval(snap("\"state\":\"ok\",\"usedPct\":95,\"nextCheckS\":5", "c")).base, 20)
    expect("上限", checkInterval(snap("\"state\":\"ok\",\"usedPct\":5,\"nextCheckS\":9999", "d")).base, 300)
    expect("没速率-高水位", checkInterval(snap("\"state\":\"ok\",\"usedPct\":86", "e")).base, 20)
    expect("没速率-中水位", checkInterval(snap("\"state\":\"ok\",\"usedPct\":60", "f")).base, 30)
    expect("没速率-低水位", checkInterval(snap("\"state\":\"ok\",\"usedPct\":10", "g")).base, 67.5)
    expect("读不到按紧的", checkInterval(readSnapshot(dir + "/没有这个文件.json")).base, 60)

    func header(_ label: String, _ body: String, _ want: String) {
        let got = headerWindowsText(snap(body, "h-" + label)) ?? "nil"
        if got != want { failures.append("表头-\(label): 期望 \(want) 实际 \(got)") }
    }
    header("Fable 不计入", "\"win5h\":10,\"win7d\":20,\"winModel\":99,\"binding\":\"7d\",\"modelName\":\"Fable\",\"modelCounted\":false",
           "5 小时 10%   7 天 20%★   Fable 周（不计入） 99%")
    header("Fable 计入", "\"win5h\":10,\"win7d\":20,\"winModel\":99,\"binding\":\"Fable\",\"modelName\":\"Fable\",\"modelCounted\":true",
           "5 小时 10%   7 天 20%   Fable 周 99%★")
    header("老状态文件", "\"win5h\":10,\"win7d\":20,\"winModel\":99",
           "5 小时 10%   7 天 20%   模型周 99%★")
    header("老壳没给 binding 但说了不计入", "\"win5h\":10,\"win7d\":20,\"winModel\":99,\"modelCounted\":false",
           "5 小时 10%   7 天 20%★   模型周（不计入） 99%")

    for f in failures {
        FileHandle.standardError.write(("★ " + f + "\n").data(using: .utf8)!)
    }
    print("pace selfcheck (swift): \(12 - failures.count)/12 passed")
    return failures.isEmpty
}

if CommandLine.arguments.contains("--selfcheck-pace") {
    exit(selfcheckPace() ? 0 : 1)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)     // 只在菜单栏, 不进 Dock、不抢焦点
let controller = Controller()
app.delegate = controller
app.run()
