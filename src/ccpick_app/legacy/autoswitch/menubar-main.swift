
import Cocoa
import Darwin

enum UsageCheckSource: CaseIterable {
    case startup, wake, timer, manualRefresh, accountSwitch
}

func usageCheckArguments(script: String, source: UsageCheckSource) -> [String] {
    [script, source == .manualRefresh ? "--force-check" : "--now"]
}

enum AccountSwitchMode: Int, CaseIterable {
    case watchOnly, automatic

    var title: String { self == .watchOnly ? "只看不切" : "又看又切" }
    var detail: String {
        self == .watchOnly ? "监控额度并提醒，账号由你手动选择"
            : "监控额度并自动选择账号，跟随选择的会话在安全节点切换"
    }
}

func watchOnlyOverride(_ environment: [String: String]) -> Bool {
    ["1", "true", "yes", "on"].contains(
        (environment["CCSWITCH_WATCH_ONLY"] ?? "").lowercased())
}

func watchOnlyPath(home: String) -> String {
    home + "/Library/Logs/claude-autoswitch.watch-only"
}

func accountSwitchMode(home: String, environment: [String: String]) -> AccountSwitchMode {
    watchOnlyOverride(environment) || FileManager.default.fileExists(atPath: watchOnlyPath(home: home))
        ? .watchOnly : .automatic
}

enum MenuActionError: Error { case forcedWatchOnly, invalidMarker, invalidRuntime }

func saveAccountSwitchMode(_ mode: AccountSwitchMode, home: String,
                           environment: [String: String]) throws {
    if mode == .automatic && watchOnlyOverride(environment) { throw MenuActionError.forcedWatchOnly }
    let fm = FileManager.default
    let path = watchOnlyPath(home: home)
    if let attrs = try? fm.attributesOfItem(atPath: path),
       attrs[.type] as? FileAttributeType != .typeRegular { throw MenuActionError.invalidMarker }
    if mode == .watchOnly {
        try fm.createDirectory(atPath: (path as NSString).deletingLastPathComponent,
                               withIntermediateDirectories: true)
        try Data("watch-only\n".utf8).write(to: URL(fileURLWithPath: path), options: .atomic)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path)
    } else if fm.fileExists(atPath: path) {
        try fm.removeItem(atPath: path)
    }
}

func accountPython(home: String) throws -> String {
    return "/usr/bin/python3"
}

func shouldCheckAfterSwitch(exitCode: Int32) -> Bool { exitCode == 0 }

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
    case "advise":   return ("arrow.left.arrow.right.circle.fill", [.systemOrange, .systemYellow])
    case "cooling":  return ("hourglass.circle.fill", [.systemTeal, .systemBlue])
    case "stalled":  return ("clock.badge.exclamationmark.fill", [.systemPurple, .systemIndigo])
    default:         return ("questionmark.circle.fill", [.systemGray, .systemBrown])
    }
}

func headline(for s: Snapshot, mode: AccountSwitchMode? = nil) -> String {
    switch s.state {
    case "ok":
        let watch = mode.map { $0 == .watchOnly } ?? s.message.hasPrefix("只看不切")
        return watch ? "只看不切：在盯着（自动切号已关）" : "又看又切：在盯着"
    case "advise":   return mode == .automatic ? "上次检查建议换号，等待下一轮" : "该换号了（自动切号已关）"
    case "switched": return "账号刚切过"
    case "blocked":
        if s.message.hasPrefix("切换没成功") { return "自动切号没切成" }
        if s.message.hasPrefix("当前号疑似被封") || s.message.hasPrefix("原号疑似被封") { return "账号疑似被封" }
        return "全部账号额度用尽"
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

struct UnavailableWindowRow {
    var name: String
    var expired: Bool

    var text: String {
        let label = name == "5h" ? "5 小时" : "7 天  "
        return "        \(label)  " + (expired ? "上一周期已结束，待更新" : "尚无数据，待更新")
    }
}

func numericWindowRows(_ raw: [[String: Any]]) -> [WindowRow] {
    raw.compactMap { w in
        guard let used = w["used"] as? Double, used.isFinite else { return nil }
        return WindowRow(name: (w["name"] as? String) ?? "?", used: used,
                         at: (w["at"] as? String) ?? "",
                         counted: (w["counted"] as? Bool) ?? true)
    }
}

func unavailableWindowRows(_ raw: [[String: Any]]) -> [UnavailableWindowRow] {
    raw.compactMap { w in
        guard let name = w["name"] as? String, ["5h", "7d"].contains(name),
              let status = w["status"] as? String, ["expired", "missing"].contains(status) else { return nil }
        return UnavailableWindowRow(name: name, expired: status == "expired")
    }
}

func windowResetText(_ at: String) -> String {
    at.isEmpty || at == "—" ? "恢复时间未提供" : "\(at) 恢复"
}

struct Account {
    var slot: String
    var email: String
    var active: Bool
    var headroom: Double?        // 最紧那道闸还剩多少 —— 只用来排序
    var windows: [WindowRow]     // ★逐窗口原样显示, 不汇总★
    var plan: String? = nil
    var capacity: Double? = nil
    var blocked: Bool = false
    var blockedWhy: String = ""
    var unavailableWindows: [UnavailableWindowRow] = []
    var autoSwitchEnabled: Bool = true
}

func menuAccounts(_ cached: [Account], home: String) -> [Account] {
    let active = cached.filter { $0.active }
    let selected = active.count == 1 ? active[0].email.lowercased() : nil
    return markedMenuAccounts(cached, selectedEmail: selected)
}

func markedMenuAccounts(_ cached: [Account], selectedEmail: String?) -> [Account] {
    let matches = cached.indices.filter { cached[$0].email.lowercased() == selectedEmail }
    let chosen = matches.count == 1 ? matches[0] : nil
    return cached.enumerated().map { index, account in
        var row = account
        row.active = index == chosen
        return row
    }
}

func accountTitle(_ account: Account) -> String {
    var tags: [String] = []
    if let plan = account.plan, !plan.isEmpty, plan != "?" { tags.append(plan) }
    if let cap = account.capacity, cap.isFinite { tags.append(String(format: "%.0f 点", cap)) }
    if !account.autoSwitchEnabled { tags.append("仅手动") }
    return (account.active ? "● " : "   ") + account.email
        + (tags.isEmpty ? "" : "    " + tags.joined(separator: " · "))
}

func accountAutoSwitchEnabled(_ value: Any?) -> Bool {
    guard let flag = value as? NSNumber, CFGetTypeID(flag) == CFBooleanGetTypeID() else { return true }
    return flag.boolValue
}

let accountFetchTimeoutS: TimeInterval = 30

func loadAccounts() -> (accounts: [Account], fetchedAt: Double?) {
    let helper = ("~/bin/claude-autoswitch-helper.py" as NSString).expandingTildeInPath
    guard FileManager.default.isReadableFile(atPath: helper) else { return ([], nil) }
    let p = Process()
    guard let python = try? accountPython(home: NSHomeDirectory()) else { return ([], nil) }
    p.launchPath = python
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
        let wins = numericWindowRows(r["windows"] as? [[String: Any]] ?? [])
        return Account(slot: (r["slot"] as? String) ?? "?",
                       email: (r["email"] as? String) ?? "?",
                       active: (r["active"] as? Bool) ?? false,
                       headroom: r["headroom"] as? Double,
                       windows: wins,
                       plan: r["plan"] as? String,
                       capacity: r["cap"] as? Double,
                       blocked: (r["blocked"] as? Bool) ?? false,
                       blockedWhy: (r["blockedWhy"] as? String) ?? "",
                       unavailableWindows: unavailableWindowRows(r["unavailableWindows"] as? [[String: Any]] ?? []),
                       autoSwitchEnabled: accountAutoSwitchEnabled(r["autoSwitchEnabled"]))
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
    private var switchInFlight = false
    private var actionNotice: String?

    private var currentMode: AccountSwitchMode {
        accountSwitchMode(home: NSHomeDirectory(), environment: ProcessInfo.processInfo.environment)
    }

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

        runCheck(source: .startup)
        fetchAccounts(reason: "启动")        // 预热, 让第一次打开面板就有东西

        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            self?.runCheck(source: .wake)
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
                              accessibilityDescription: headline(for: snap, mode: currentMode))
            img?.isTemplate = false
            if #available(macOS 12.0, *) {
                let cfg = NSImage.SymbolConfiguration(paletteColors: colors)
                    .applying(.init(pointSize: 15, weight: .semibold))
                img = img?.withSymbolConfiguration(cfg)
            }
            button.image = img
            button.contentTintColor = nil
            button.toolTip = "模式：\(currentMode.title)\n\(headline(for: snap, mode: currentMode))\n\(ageText(snap.age))"
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
        menu.autoenablesItems = false
        let mode = currentMode
        let modeStatus = NSMenuItem(title: "当前模式：\(mode.title)", action: nil, keyEquivalent: "")
        modeStatus.isEnabled = false
        menu.addItem(modeStatus)
        for option in AccountSwitchMode.allCases {
            let mi = NSMenuItem(title: option.title, action: #selector(changeMode(_:)), keyEquivalent: "")
            mi.target = self
            mi.tag = option.rawValue
            mi.state = mode == option ? .on : .off
            mi.toolTip = option.detail
            if option == .automatic && watchOnlyOverride(ProcessInfo.processInfo.environment) {
                mi.isEnabled = false
                mi.toolTip = "CCSWITCH_WATCH_ONLY 环境变量固定为只看不切"
            }
            menu.addItem(mi)
        }
        if let notice = actionNotice {
            let mi = NSMenuItem(title: notice, action: nil, keyEquivalent: "")
            mi.isEnabled = false
            menu.addItem(mi)
        }
        menu.addItem(.separator())
        menu.addItem(withTitle: headline(for: snap, mode: mode), action: nil, keyEquivalent: "")
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
        let (cachedAccounts, fetched) = accountCache ?? ([], nil)
        let accounts = menuAccounts(cachedAccounts, home: NSHomeDirectory())
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
                let mi = NSMenuItem(title: accountTitle(a),
                                    action: #selector(switchTo(_:)), keyEquivalent: "")
                mi.target = self
                mi.representedObject = a.email
                mi.isEnabled = !a.active && !switchInFlight
                if a.blocked { mi.toolTip = a.blockedWhy }
                if !a.autoSwitchEnabled {
                    mi.toolTip = "只在手动选择时使用，不参与自动切号。"
                        + (a.blockedWhy.isEmpty ? "" : " " + a.blockedWhy)
                }
                mi.attributedTitle = NSAttributedString(
                    string: mi.title,
                    attributes: [.font: NSFont.systemFont(
                        ofSize: 12, weight: a.active ? .semibold : .regular),
                        .foregroundColor: a.autoSwitchEnabled ? NSColor.labelColor : NSColor.systemPurple])
                menu.addItem(mi)
                if a.blocked, !a.blockedWhy.isEmpty {
                    let reason = NSMenuItem(title: "        " + String(a.blockedWhy.prefix(80)),
                                            action: nil, keyEquivalent: "")
                    reason.isEnabled = false
                    menu.addItem(reason)
                }
                for w in a.windows {
                    let label = w.name == "5h" ? "5 小时" : (w.name == "7d" ? "7 天  " : w.name)
                    let line = String(format: "        %@  已用 %3d%%   %@%@",
                                      label, Int(w.used.rounded()),
                                      windowResetText(w.at),
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
                for w in a.unavailableWindows where !a.windows.contains(where: { $0.name == w.name }) {
                    let sub = NSMenuItem(title: w.text, action: nil, keyEquivalent: "")
                    sub.isEnabled = false
                    sub.attributedTitle = NSAttributedString(string: w.text, attributes: [
                        .font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular),
                        .foregroundColor: NSColor.secondaryLabelColor])
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
            self?.runCheck(source: .timer)
        }
        checkTimer?.tolerance = 15
        Self.debug(String(format: "已排下一轮: %.0f 秒后 (基准 %.0f 抖动 ±%.0f)", delay, base, spread))
    }

    private func runCheck(source: UsageCheckSource) {
        checkGeneration &+= 1
        let gen = checkGeneration
        let home = NSHomeDirectory()
        let script = home + "/bin/claude-account-autoswitch.sh"
        let p = Process()
        p.launchPath = "/bin/bash"
        p.arguments = usageCheckArguments(script: script, source: source)
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
            Self.debug("spawn ok source=\(source) script=\(script)")
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
        runCheck(source: .manualRefresh)
    }

    @objc private func changeMode(_ sender: NSMenuItem) {
        guard let mode = AccountSwitchMode(rawValue: sender.tag) else { return }
        do {
            try saveAccountSwitchMode(mode, home: NSHomeDirectory(), environment: ProcessInfo.processInfo.environment)
            actionNotice = "已选择「\(mode.title)」，下一轮检查生效"
            Self.debug("模式改为：\(mode.title)；下轮检查生效")
        } catch {
            actionNotice = "模式保存未完成或已被环境锁定；以当前勾选为准"
            Self.debug("模式修改失败；未执行检查或切号")
        }
        refresh()
    }

    @objc private func switchTo(_ sender: NSMenuItem) {
        guard !switchInFlight, let email = sender.representedObject as? String else { return }
        let entry = ("~/.claude/tools/ccpick/ccpick.py" as NSString).expandingTildeInPath
        guard FileManager.default.fileExists(atPath: entry) else { return }
        let p = Process()
        guard let python = try? accountPython(home: NSHomeDirectory()) else {
            actionNotice = "切号未开始：本机账号管理器的 Python 不可用"
            Self.debug("手动切号未开始：登记运行环境不可用")
            return
        }
        p.launchPath = python
        p.arguments = [entry, "switch", email]
        var env = ProcessInfo.processInfo.environment
        env["HOME"] = NSHomeDirectory()
        p.environment = env
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.switchInFlight = false
                if shouldCheckAfterSwitch(exitCode: proc.terminationStatus) {
                    self.actionNotice = "账号已选择；跟随选择的会话在安全节点续接"
                    self.runCheck(source: .accountSwitch)
                } else {
                    self.actionNotice = "切号未完成，当前账号与任务已保留；详情见下方状态"
                    self.refresh()
                    self.fetchAccounts(reason: "切号未完成")
                }
                Self.debug("手动切号结束 (exit \(proc.terminationStatus))")
            }
        }
        do {
            try p.run()
            switchInFlight = true
            actionNotice = "正在核对目标账号，完成后才会切换…"
        } catch {
            actionNotice = "切号未开始：无法启动账号管理器"
            Self.debug("手动切号未开始：启动失败")
        }
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

    let script = "/fixture home/bin/claude-account-autoswitch.sh"
    for source in UsageCheckSource.allCases {
        let expected = source == .manualRefresh ? "--force-check" : "--now"
        let got = usageCheckArguments(script: script, source: source)
        if got != [script, expected] { failures.append("检查来源-\(source): 错误参数 \(got)") }
    }

    for f in failures {
        FileHandle.standardError.write(("★ " + f + "\n").data(using: .utf8)!)
    }
    let total = 12 + UsageCheckSource.allCases.count
    print("pace selfcheck (swift): \(total - failures.count)/\(total) passed")
    return failures.isEmpty
}

func selfcheckSettings() -> Bool {
    let fm = FileManager.default
    let home = NSTemporaryDirectory() + "ccpick-menu-selfcheck-" + UUID().uuidString
    defer { try? fm.removeItem(atPath: home) }
    var failures: [String] = []
    var total = 0
    func check(_ label: String, _ success: Bool) {
        total += 1
        if !success { failures.append(label) }
    }
    do {
        let logs = home + "/Library/Logs"
        try fm.createDirectory(atPath: logs, withIntermediateDirectories: true)
        let retained = ["claude-autoswitch-status.json", "claude-autoswitch.pause",
                        "claude-autoswitch-cooldown.json", ".claude-autoswitch.lock"]
        for name in retained { try Data("retained fixture".utf8).write(to: URL(fileURLWithPath: logs + "/" + name)) }
        check("两种持久模式", AccountSwitchMode.allCases.count == 2)
        check("无标记沿用自动模式", accountSwitchMode(home: home, environment: [:]) == .automatic)
        try saveAccountSwitchMode(.watchOnly, home: home, environment: [:])
        check("只看标记持久保存", accountSwitchMode(home: home, environment: [:]) == .watchOnly)
        let attrs = try fm.attributesOfItem(atPath: watchOnlyPath(home: home))
        check("标记权限", attrs[.posixPermissions] as? Int == 0o600)
        try saveAccountSwitchMode(.watchOnly, home: home, environment: [:])
        check("重复选择只看幂等", accountSwitchMode(home: home, environment: [:]) == .watchOnly)
        do {
            try saveAccountSwitchMode(.automatic, home: home, environment: ["CCSWITCH_WATCH_ONLY": "TRUE"])
            check("环境锁定拒绝开启自动", false)
        } catch MenuActionError.forcedWatchOnly {
            check("环境锁定拒绝开启自动", true)
        }
        check("拒绝操作保留标记", fm.fileExists(atPath: watchOnlyPath(home: home)))
        try saveAccountSwitchMode(.automatic, home: home, environment: [:])
        check("自动模式删除同一个标记", accountSwitchMode(home: home, environment: [:]) == .automatic)
        try saveAccountSwitchMode(.automatic, home: home, environment: [:])
        check("重复选择自动幂等", !fm.fileExists(atPath: watchOnlyPath(home: home)))
        for value in ["1", "true", "TRUE", "yes", "on"] {
            check("识别环境开关 " + value, accountSwitchMode(home: home, environment: ["CCSWITCH_WATCH_ONLY": value]) == .watchOnly)
        }
        for value in ["", "0", "false", "off", " true "] {
            check("环境非开关值 " + value, accountSwitchMode(home: home, environment: ["CCSWITCH_WATCH_ONLY": value]) == .automatic)
        }
        for name in retained {
            check("保留后台文件 " + name, fm.contents(atPath: logs + "/" + name) == Data("retained fixture".utf8))
        }
        let sentinel = logs + "/claude-autoswitch-status.json"
        try fm.createSymbolicLink(atPath: watchOnlyPath(home: home), withDestinationPath: sentinel)
        do {
            try saveAccountSwitchMode(.automatic, home: home, environment: [:])
            check("不移除替换的标记链接", false)
        } catch MenuActionError.invalidMarker {
            check("不移除替换的标记链接", true)
        }
        check("拒绝后原文件保留", fm.contents(atPath: sentinel) == Data("retained fixture".utf8))

        let basic = Account(slot: "1", email: "account-0001@example.com", active: true, headroom: 40, windows: [])
        check("旧 helper 账号行", accountTitle(basic) == "● account-0001@example.com")
        var capacity = basic
        capacity.plan = "20x"; capacity.capacity = 144
        check("套餐与额度点", accountTitle(capacity) == "● account-0001@example.com    20x · 144 点")
        capacity.autoSwitchEnabled = false
        check("仅手动保留当前选中与额度", accountTitle(capacity) == "● account-0001@example.com    20x · 144 点 · 仅手动")
        capacity.active = false
        check("仅手动未选中仍为普通账号行", accountTitle(capacity) == "   account-0001@example.com    20x · 144 点 · 仅手动")
        let policyJSON = try JSONSerialization.jsonObject(with: Data("{\"on\":true,\"off\":false,\"number\":0,\"string\":\"false\"}".utf8)) as! [String: Any]
        check("仅明确false显示仅手动", !accountAutoSwitchEnabled(policyJSON["off"]) && accountAutoSwitchEnabled(policyJSON["on"]))
        check("缺省与畸形偏好兼容旧账号", accountAutoSwitchEnabled(nil) && accountAutoSwitchEnabled(policyJSON["number"]) && accountAutoSwitchEnabled(policyJSON["string"]))
        let marked = markedMenuAccounts([basic, capacity], selectedEmail: nil)
        check("更新选中不改变手动偏好", marked[0].autoSwitchEnabled && !marked[1].autoSwitchEnabled && !marked[1].active)
        let windowJSON = Data("{\"windows\":[{\"name\":\"5h\",\"used\":null},{\"name\":\"7d\",\"used\":0}],\"unavailableWindows\":[{\"name\":\"5h\",\"status\":\"expired\"}]}".utf8)
        let windowObject = try JSONSerialization.jsonObject(with: windowJSON) as! [String: Any]
        let numeric = numericWindowRows(windowObject["windows"] as! [[String: Any]])
        check("未知额度不显示成零", numeric.count == 1 && numeric.first?.name == "7d" && numeric.first?.used == 0)
        let unavailable = unavailableWindowRows(windowObject["unavailableWindows"] as! [[String: Any]])
        check("已过周期五小时仍有行", unavailable.first?.text == "        5 小时  上一周期已结束，待更新")
        let missing = unavailableWindowRows([["name": "7d", "status": "missing"]])
        check("尚无数据明确提示", missing.first?.text == "        7 天    尚无数据，待更新")
        check("非有限数值不显示", numericWindowRows([["name": "5h", "used": Double.nan]]).isEmpty)
        check("旧helper缺附加字段仍兼容", unavailableWindowRows([]).isEmpty)
        check("未知占位不混入窗口", unavailableWindowRows([["name": "Fable", "status": "missing"]]).isEmpty)
        check("缺少恢复时间不暗示已知", windowResetText("—") == "恢复时间未提供" && windowResetText("") == "恢复时间未提供")
        check("成功切号后才检查", shouldCheckAfterSwitch(exitCode: 0))
        check("失败切号保留回执", !shouldCheckAfterSwitch(exitCode: 1))
        var old = readSnapshot(home + "/missing")
        old.state = "ok"; old.message = "只看不切"
        check("模式显示不沿用旧状态", headline(for: old, mode: .automatic) == "又看又切：在盯着")
        check("已关闭自动立即显示", headline(for: old, mode: .watchOnly).contains("自动切号已关"))
    } catch {
        failures.append("临时文件自检未完成")
        total += 1
    }
    for failure in failures { FileHandle.standardError.write(Data(("★ " + failure + "\n").utf8)) }
    print("menu settings selfcheck (swift): \(total - failures.count)/\(total) passed")
    return failures.isEmpty
}

func selfcheckSelection() -> Bool {
    let first = Account(slot: "a", email: "alpha@example.com", active: true, headroom: 80, windows: [])
    let second = Account(slot: "b", email: "beta@example.com", active: false, headroom: 60, windows: [])
    func marks(_ rows: [Account]) -> [String] { rows.filter { $0.active }.map { $0.slot } }
    var conflicting = second; conflicting.active = true
    var idle = first; idle.active = false
    let checks = [
        marks(menuAccounts([first, second], home: "/fixture")) == ["a"],
        marks(menuAccounts([first, conflicting], home: "/fixture")).isEmpty,
        marks(menuAccounts([idle, second], home: "/fixture")).isEmpty,
        marks(menuAccounts([first, first], home: "/fixture")).isEmpty,
        markedMenuAccounts([first, second], selectedEmail: nil).allSatisfy { !$0.active }
    ]
    print("menu selection selfcheck (swift): \(checks.filter { $0 }.count)/\(checks.count) passed")
    return checks.allSatisfy { $0 }
}

if CommandLine.arguments.contains("--selfcheck-pace") {
    exit(selfcheckPace() ? 0 : 1)
}
if CommandLine.arguments.contains("--selfcheck-settings") {
    exit(selfcheckSettings() ? 0 : 1)
}
if CommandLine.arguments.contains("--selfcheck-selection") {
    exit(selfcheckSelection() ? 0 : 1)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)     // 只在菜单栏, 不进 Dock、不抢焦点
let controller = Controller()
app.delegate = controller
app.run()
