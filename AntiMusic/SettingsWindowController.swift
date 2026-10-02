//
// SettingsWindowController.swift
// AntiMusic
//
// 程序化构建的设置窗口（避免改 XIB）：
//  目标 App 选择（自动读 bundle ID，不写死 QQ音乐）、全局热键录制、
//  注入链开关、时序参数、测试注入、权限引导（需求 5.4/5.3）。
//

import Foundation
import AppKit
import CoreGraphics
import UniformTypeIdentifiers

final class SettingsWindowController: NSWindowController, NSWindowDelegate {
    var onConfigChanged: ((WakeConfig) -> Void)?
    var onTestInject: (() -> Void)?
    var onWakeNow: (() -> Void)?
    var statusProvider: (() -> String)?

    private var config = WakeConfig.load()
    private var isRecordingHotkey = false
    private var hotkeyMonitor: Any?
    private var lastNotice = ""

    private let targetLabel = NSTextField(labelWithString: "")
    private let hotkeyButton = NSButton(title: "录制组合键", target: nil, action: nil)
    private let hotkeyLabel = NSTextField(labelWithString: "")
    private let statusLabel = NSTextField(labelWithString: "")
    private var paramFields: [String: NSTextField] = [:]
    private var chainBoxes: [String: NSButton] = [:]

    convenience init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 470),
            styleMask: [.titled, .closable],
            backing: .buffered, defer: false)
        window.title = "AntiMusic 设置"
        window.center()
        self.init(window: window)
        window.delegate = self
        buildUI()
    }

    func showSettings() {
        config = WakeConfig.load()
        refreshFields()
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    // MARK: UI

    private func buildUI() {
        guard let content = window?.contentView else { return }
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 20),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -20),
        ])

        // 目标 App
        stack.addArrangedSubview(makeHeader("目标播放器"))
        let targetRow = NSStackView(views: [targetLabel, makeButton("选择 App…", #selector(chooseApp))])
        targetRow.orientation = .horizontal
        targetRow.spacing = 8
        stack.addArrangedSubview(targetRow)

        // 热键
        stack.addArrangedSubview(makeHeader("播放组合键（需与 QQ音乐内设置的「全局快捷键」一致）"))
        let hotkeyRow = NSStackView(views: [hotkeyButton, hotkeyLabel])
        hotkeyRow.orientation = .horizontal
        hotkeyRow.spacing = 8
        hotkeyButton.target = self
        hotkeyButton.action = #selector(toggleRecord)
        stack.addArrangedSubview(hotkeyRow)
        let hint = NSTextField(wrappingLabelWithString: "QQ音乐 → 设置 → 快捷键 → 启用全局快捷键，并绑定同一组合键。避开 ⌘Space、⌘⇧Space、⌘, 等系统占用键。默认 ⌘⌥P。")
        hint.textColor = .secondaryLabelColor
        hint.preferredMaxLayoutWidth = 500
        stack.addArrangedSubview(hint)

        // 注入链
        stack.addArrangedSubview(makeHeader("注入方式（按优先级降级尝试）"))
        for (code, name) in [("P1", "P1 模拟全局快捷键（主推，需 输入监控+辅助功能）"),
                             ("P1B", "P1 广播降级（定向无效时把组合键广播到系统事件流）"),
                             ("P0", "P0 定向投递系统媒体键"),
                             ("P2", "P2 URL scheme 触发播放（实测有效，播放默认电台）"),
                             ("P3", "P3 辅助功能点击播放按钮（UI 兜底）")] {
            let box = NSButton(checkboxWithTitle: name, target: self, action: #selector(chainToggled(_:)))
            box.identifier = NSUserInterfaceItemIdentifier(code)
            chainBoxes[code] = box
            stack.addArrangedSubview(box)
        }

        // 时序参数
        stack.addArrangedSubview(makeHeader("时序参数（秒）"))
        let paramRow = NSStackView()
        paramRow.orientation = .horizontal
        paramRow.spacing = 12
        for key in ["launchTimeout", "readyDelay", "retryInterval", "verifyTimeout", "settleWindow"] {
            let label = NSTextField(labelWithString: Self.paramNames[key] ?? key)
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 50, height: 22))
            field.identifier = NSUserInterfaceItemIdentifier(key)
            field.target = self
            field.action = #selector(paramEdited(_:))
            paramFields[key] = field
            let cell = NSStackView(views: [label, field])
            cell.orientation = .horizontal
            cell.spacing = 4
            paramRow.addArrangedSubview(cell)
        }
        stack.addArrangedSubview(paramRow)

        // 操作
        let actionRow = NSStackView(views: [
            makeButton("测试注入", #selector(testInject)),
            makeButton("立即唤醒并播放", #selector(wakeNow)),
            makeButton("检查权限", #selector(checkPermissions)),
        ])
        actionRow.orientation = .horizontal
        actionRow.spacing = 8
        stack.addArrangedSubview(actionRow)

        statusLabel.textColor = .secondaryLabelColor
        statusLabel.preferredMaxLayoutWidth = 500
        stack.addArrangedSubview(statusLabel)
        refreshFields()
    }

    private static let paramNames = [
        "launchTimeout": "启动等待", "readyDelay": "就绪延迟", "retryInterval": "重试间隔",
        "verifyTimeout": "出声验证", "settleWindow": "静默期",
    ]

    private func makeHeader(_ title: String) -> NSTextField {
        let l = NSTextField(labelWithString: title)
        l.font = NSFont.boldSystemFont(ofSize: 12)
        return l
    }

    private func makeButton(_ title: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: title, target: self, action: action)
        b.bezelStyle = .rounded
        return b
    }

    private func refreshFields() {
        targetLabel.stringValue = config.playerAppPath
        hotkeyLabel.stringValue = Self.describeHotkey(flags: config.hotkeyFlags, keyCode: config.hotkeyKeyCode)
        chainBoxes["P1"]?.state = config.enableP1Hotkey ? .on : .off
        chainBoxes["P1B"]?.state = config.enableP1Broadcast ? .on : .off
        chainBoxes["P0"]?.state = config.enableP0MediaKey ? .on : .off
        chainBoxes["P2"]?.state = config.enableP2URLScheme ? .on : .off
        chainBoxes["P3"]?.state = config.enableP3AXPress ? .on : .off
        paramFields["launchTimeout"]?.stringValue = String(format: "%.1f", config.launchTimeout)
        paramFields["readyDelay"]?.stringValue = String(format: "%.1f", config.readyDelay)
        paramFields["retryInterval"]?.stringValue = String(format: "%.1f", config.retryInterval)
        paramFields["verifyTimeout"]?.stringValue = String(format: "%.1f", config.verifyTimeout)
        paramFields["settleWindow"]?.stringValue = String(format: "%.1f", config.settleWindow)
        statusLabel.stringValue = lastNotice.isEmpty ? (statusProvider?() ?? "") : lastNotice
    }

    static func describeHotkey(flags: UInt64, keyCode: UInt16) -> String {
        var s = ""
        if flags & (1 << 20) != 0 { s += "⌘" }
        if flags & (1 << 19) != 0 { s += "⌥" }
        if flags & (1 << 18) != 0 { s += "⌃" }
        if flags & (1 << 17) != 0 { s += "⇧" }
        s += Self.characterName(keyCode)
        return s
    }

    private static func characterName(_ code: UInt16) -> String {
        let names: [UInt16: String] = [
            35: "P", 15: "R", 3: "F", 5: "G", 4: "H", 34: "I", 31: "O", 37: "L",
            46: "M", 45: "N", 1: "S", 17: "T", 0: "A", 11: "B", 14: "E", 40: "K",
            9: "V", 13: "W", 7: "X", 6: "Y", 16: "Z", 12: "Q", 2: "D", 8: "C", 32: "U", 38: "J",
            36: "⏎", 49: "Space", 51: "⌫",
        ]
        return names[code] ?? "key(\(code))"
    }

    // MARK: Actions

    @objc private func chooseApp() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.directoryURL = URL(fileURLWithPath: "/Applications")
        if #available(macOS 11.0, *) { panel.allowedContentTypes = [UTType.application] }
        panel.beginSheetModal(for: window!) { [self] resp in
            guard resp == .OK, let url = panel.url, let _ = window else { return }
            do {
                config.playerAppPath = try NSURL(resolvingAliasFileAt: url, options: []).path ?? url.path
            } catch {
                config.playerAppPath = url.path
            }
            lastNotice = "目标 bundle ID：\(config.targetBundleID ?? "(未知)")"
            persist()
        }
    }

    @objc private func toggleRecord() {
        if isRecordingHotkey { stopRecording(); return }
        isRecordingHotkey = true
        hotkeyButton.title = "按下一个组合键…（Esc 取消）"
        hotkeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self, self.isRecordingHotkey else { return event }
            if event.keyCode == 53 { // Esc
                self.stopRecording()
                return nil
            }
            let mask: NSEvent.ModifierFlags = [.command, .option, .control, .shift]
            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask).intersection(mask)
            guard !flags.isEmpty else { return event } // 纯修饰键忽略
            self.config.hotkeyFlags = UInt64(flags.rawValue)
            self.config.hotkeyKeyCode = event.keyCode
            self.stopRecording()
            self.lastNotice = "组合键已更新，请同步修改 QQ音乐 内的全局快捷键"
            self.persist()
            return nil
        }
    }

    private func stopRecording() {
        isRecordingHotkey = false
        hotkeyButton.title = "录制组合键"
        if let monitor = hotkeyMonitor { NSEvent.removeMonitor(monitor); hotkeyMonitor = nil }
        refreshFields()
    }

    @objc private func chainToggled(_ sender: NSButton) {
        guard let code = sender.identifier?.rawValue else { return }
        let on = sender.state == .on
        switch code {
        case "P1": config.enableP1Hotkey = on
        case "P1B": config.enableP1Broadcast = on
        case "P0": config.enableP0MediaKey = on
        case "P2": config.enableP2URLScheme = on
        case "P3": config.enableP3AXPress = on
        default: break
        }
        persist()
    }

    @objc private func paramEdited(_ sender: NSTextField) {
        guard let key = sender.identifier?.rawValue else { return }
        let value = Double(sender.stringValue) ?? 0
        guard value > 0 else { refreshFields(); return }
        switch key {
        case "launchTimeout": config.launchTimeout = value
        case "readyDelay": config.readyDelay = value
        case "retryInterval": config.retryInterval = value
        case "verifyTimeout": config.verifyTimeout = value
        case "settleWindow": config.settleWindow = value
        default: break
        }
        persist()
    }

    @objc private func testInject() { onTestInject?() }
    @objc private func wakeNow() { onWakeNow?() }

    @objc private func checkPermissions() {
        PermissionsChecker.promptIfNeeded(window: window)
    }

    private func persist() {
        config.save()
        onConfigChanged?(config)
        refreshFields()
    }
}
