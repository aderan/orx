import XCTest
@testable import ORXAnalyticsCore

// Distribution-channel identity tests (G011 T005): the channel block
// written into Info.plist by scripts/build_app.sh — channel, audience,
// Gatekeeper limits and the COMPLETE source commit — read back honestly.
// Unknown channels stay unknown (never promoted); missing keys stay
// unlabeled (never guessed).

final class AppChannelInfoTests: XCTestCase {
    private let fullCommit = "07ab676e3f6499f8b636354c670de7b11ac15e69"

    func testInternalPreviewMetadataReadsBackWithLimits() {
        let info = AppChannelInfo.from(infoDictionary: [
            AppChannelInfo.channelKey: "internal-preview",
            AppChannelInfo.audienceKey: "internal testing only (内部测试)",
            AppChannelInfo.gatekeeperKey:
                "Ad-hoc signed, NOT notarized: Gatekeeper blocks first launch.",
            AppChannelInfo.commitKey: fullCommit,
        ])
        XCTAssertEqual(info?.channel, "internal-preview")
        XCTAssertEqual(info?.isInternalPreview, true)
        XCTAssertEqual(info?.isOfficial, false)
        XCTAssertTrue(info?.audience?.contains("内部测试") ?? false)
        XCTAssertTrue(info?.gatekeeperNotice?.contains("NOT notarized") ?? false)
        XCTAssertEqual(info?.sourceCommit, fullCommit)
        XCTAssertEqual(info?.hasCompleteSourceCommit, true)
        XCTAssertTrue(info?.summaryText.contains("内部预览") ?? false)
        XCTAssertTrue(info?.summaryText.contains("未公证") ?? false)
    }

    func testOfficialMetadataReadsBack() {
        let info = AppChannelInfo.from(infoDictionary: [
            AppChannelInfo.channelKey: "official",
            AppChannelInfo.commitKey: fullCommit,
        ])
        XCTAssertEqual(info?.channel, "official")
        XCTAssertEqual(info?.isOfficial, true)
        XCTAssertEqual(info?.isInternalPreview, false)
        XCTAssertTrue(info?.summaryText.contains("正式分发") ?? false)
    }

    func testDevelopmentChannelIsNotADistributionArtifact() {
        let info = AppChannelInfo.from(infoDictionary: [
            AppChannelInfo.channelKey: "development",
        ])
        XCTAssertEqual(info?.channel, "development")
        XCTAssertEqual(info?.isInternalPreview, false)
        XCTAssertEqual(info?.isOfficial, false)
        XCTAssertNil(info?.sourceCommit)
        XCTAssertEqual(info?.hasCompleteSourceCommit, false)
        XCTAssertTrue(info?.summaryText.contains("开发运行") ?? false)
    }

    func testUnknownChannelIsTreatedConservativelyNotPromoted() {
        let info = AppChannelInfo.from(infoDictionary: [
            AppChannelInfo.channelKey: "mystery-channel",
        ])
        XCTAssertEqual(info?.channel, "mystery-channel")
        XCTAssertEqual(info?.isInternalPreview, false)
        XCTAssertEqual(info?.isOfficial, false)
        // Unknown stays unknown and is NOT presented as formal.
        XCTAssertTrue(info?.summaryText.contains("未知渠道") ?? false)
        XCTAssertFalse(info?.summaryText.contains("正式分发渠道") ?? true)
    }

    func testMissingChannelKeyIsUnlabeledNeverGuessed() {
        XCTAssertNil(AppChannelInfo.from(infoDictionary: [:]))
        // Empty string is as good as absent.
        XCTAssertNil(AppChannelInfo.from(infoDictionary: [
            AppChannelInfo.channelKey: "",
        ]))
    }

    func testTruncatedCommitIsReportedIncompleteNotFull() {
        let info = AppChannelInfo.from(infoDictionary: [
            AppChannelInfo.channelKey: "internal-preview",
            AppChannelInfo.commitKey: "07ab676",   // truncated: not complete
        ])
        XCTAssertEqual(info?.sourceCommit, "07ab676")
        XCTAssertEqual(info?.hasCompleteSourceCommit, false)
        // Non-hex junk of the right length is still not a commit.
        let junk = AppChannelInfo.from(infoDictionary: [
            AppChannelInfo.channelKey: "official",
            AppChannelInfo.commitKey: String(repeating: "z", count: 40),
        ])
        XCTAssertEqual(junk?.hasCompleteSourceCommit, false)
    }

    func testEmptyOptionalValuesBecomeNil() {
        let info = AppChannelInfo.from(infoDictionary: [
            AppChannelInfo.channelKey: "internal-preview",
            AppChannelInfo.audienceKey: "",
            AppChannelInfo.gatekeeperKey: "",
            AppChannelInfo.commitKey: "",
        ])
        XCTAssertNil(info?.audience)
        XCTAssertNil(info?.gatekeeperNotice)
        XCTAssertNil(info?.sourceCommit)
    }
}
