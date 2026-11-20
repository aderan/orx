import XCTest
@testable import ORXAnalyticsCore

/// User preferences (design §8/§9): notifications OFF by default (system
/// permission is only requested after the user opts in), no data-dir
/// override by default (the shared analytics data-dir), panel-open
/// refresh at ~30s (G012 用户反馈 4: lowered from the original 5s).
/// Persisted through an injectable store — tests never touch the real
/// user defaults domain.
final class AppSettingsTests: XCTestCase {

    private var defaults: UserDefaults!
    private var suiteName: String!

    override func setUp() {
        suiteName = "orx-settings-tests-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
    }

    // MARK: conservative defaults

    func testFreshDefaultsAreConservative() {
        let store = UserDefaultsAppSettingsStore(defaults: defaults)
        let settings = store.load()
        XCTAssertFalse(settings.notificationsEnabled,
                       "通知默认关闭 — permission is never requested unprompted")
        XCTAssertNil(settings.dataDirectoryOverride,
                     "no custom data directory until the user chooses one")
        XCTAssertEqual(settings.panelRefreshInterval, 30,
                       "panel-open tier defaults to ~30s (G012 降低默认刷新频率)")
        XCTAssertFalse(settings.panelRefreshIntervalChosen,
                       "the default is not an explicit user choice")
    }

    func testOlderSettingsFollowTheSystemAppearanceAndUseTheDefaultPanelLayout() {
        defaults.set(60, forKey: "panel.refresh.interval")
        let settings = UserDefaultsAppSettingsStore(defaults: defaults).load()

        XCTAssertEqual(settings.appearance, .system)
        XCTAssertEqual(settings.panelDensity, .compact)
        XCTAssertEqual(settings.panelShortcuts, PanelShortcut.defaults)
        XCTAssertEqual(settings.panelRefreshInterval, 60)
    }

    func testNewAppearanceChoicesAndEmptyShortcutsPersist() {
        let store = UserDefaultsAppSettingsStore(defaults: defaults)
        store.save(AppSettings(appearance: .dark, panelDensity: .detailed, panelShortcuts: []))

        XCTAssertEqual(store.load().appearance, .dark)
        XCTAssertEqual(store.load().panelDensity, .detailed)
        XCTAssertEqual(store.load().panelShortcuts, [])
    }

    func testMenuBarAttentionBadgeIsShownWhenPreferenceIsMissing() {
        XCTAssertTrue(UserDefaultsAppSettingsStore(defaults: defaults).load().menuBarAttentionBadgeEnabled)
    }

    func testMenuBarAttentionBadgeCanStayHiddenAcrossLaunches() {
        let store = UserDefaultsAppSettingsStore(defaults: defaults)
        store.save(AppSettings(menuBarAttentionBadgeEnabled: false))

        XCTAssertFalse(UserDefaultsAppSettingsStore(defaults: defaults).load().menuBarAttentionBadgeEnabled)
    }

    func testInvalidAppearanceAndObsoleteShortcutsAreHandled() {
        defaults.set("unexpected", forKey: "appearance.theme")
        defaults.set(["usage", "old-shortcut", "usage", "history"], forKey: "panel.shortcuts")

        let settings = UserDefaultsAppSettingsStore(defaults: defaults).load()
        XCTAssertEqual(settings.appearance, .system)
        XCTAssertEqual(settings.panelShortcuts, [.usage, .history])
    }

    func testInMemoryStoreStartsAtDefaults() {
        let store = InMemoryAppSettingsStore()
        XCTAssertEqual(store.load(), AppSettings.default)
    }

    func testCurrentDomainUsesItsSavedRefreshInterval() {
        defaults.set(5.0, forKey: "panel.refresh.interval")
        let settings = UserDefaultsAppSettingsStore(defaults: defaults).load()
        XCTAssertEqual(settings.panelRefreshInterval, 5)
    }

    /// An explicit choice of the FASTEST cadence (5s was also a picker
    /// option, not just the old default) survives via the choice marker.
    func testExplicitFastChoiceIsKeptViaTheMarker() {
        defaults.set(5.0, forKey: "panel.refresh.interval")
        defaults.set(true, forKey: "panel.refresh.interval.chosen")
        let settings = UserDefaultsAppSettingsStore(defaults: defaults).load()
        XCTAssertEqual(settings.panelRefreshInterval, 5,
                       "显式选择 5 秒的用户保留 5 秒")
        XCTAssertTrue(settings.panelRefreshIntervalChosen)
    }

    // MARK: roundtrip

    func testSaveRoundtripsThroughDefaults() {
        let store = UserDefaultsAppSettingsStore(defaults: defaults)
        store.save(AppSettings(notificationsEnabled: true,
                               dataDirectoryOverride: "/tmp/alt-data",
                               panelRefreshInterval: 30))
        // a fresh store over the same domain reads it back
        let reloaded = UserDefaultsAppSettingsStore(defaults: defaults).load()
        XCTAssertTrue(reloaded.notificationsEnabled)
        XCTAssertEqual(reloaded.dataDirectoryOverride, "/tmp/alt-data")
        XCTAssertEqual(reloaded.panelRefreshInterval, 30)
    }

    func testCorruptValuesFallBackToDefaultsNotCrash() {
        defaults.set("not-a-bool" as NSString, forKey: "notifications.enabled")
        defaults.set(0.5 as Double, forKey: "panel.refresh.interval")
        let settings = UserDefaultsAppSettingsStore(defaults: defaults).load()
        XCTAssertFalse(settings.notificationsEnabled, "unparseable value is NOT enabled")
        XCTAssertEqual(settings.panelRefreshInterval, 0.5)
    }

    // MARK: socket path resolution

    func testExplicitOverrideWinsOverEnvironmentAndHome() {
        let settings = AppSettings(notificationsEnabled: false,
                                   dataDirectoryOverride: "/tmp/explicit",
                                   panelRefreshInterval: 5)
        XCTAssertEqual(
            settings.resolvedSocketPath(environment: ["ORX_ANALYTICS_HOME": "/tmp/env-home"]),
            "/tmp/explicit/engine.sock",
            "数据目录显式选择 takes precedence")
    }

    func testEnvironmentUsedWhenNoOverride() {
        let settings = AppSettings.default
        XCTAssertEqual(
            settings.resolvedSocketPath(environment: ["ORX_ANALYTICS_HOME": "/tmp/env-home"]),
            "/tmp/env-home/engine.sock")
    }

    func testEmptyOverrideFallsBack() {
        let settings = AppSettings(notificationsEnabled: false,
                                   dataDirectoryOverride: "",
                                   panelRefreshInterval: 5)
        XCTAssertEqual(
            settings.resolvedSocketPath(environment: ["ORX_ANALYTICS_HOME": "/tmp/env-home"]),
            "/tmp/env-home/engine.sock",
            "an emptied override is treated as unset")
    }
}
