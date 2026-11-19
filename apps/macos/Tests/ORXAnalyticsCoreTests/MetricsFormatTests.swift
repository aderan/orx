import XCTest
@testable import ORXAnalyticsCore

/// The frozen token-number contracts (design §5 / docs/metrics.md):
/// 完全未知显示 "—"; 只有来源明确记录为零才显示 0; 两个口径独立展示、
/// 不相加、不制造混合总数; 数字统一固定三位千位分隔（G012）。
final class MetricsFormatTests: XCTestCase {
    func testUnknownIsDashNotZero() {
        XCTAssertEqual(MetricsFormat.tokenText(nil), "—")
        XCTAssertEqual(MetricsFormat.countText(nil), "—")
    }

    func testExplicitZeroIsZero() {
        XCTAssertEqual(MetricsFormat.tokenText(0), "0")
        XCTAssertEqual(MetricsFormat.countText(0), "0")
    }

    /// G012 用户反馈：token 使用 3 位分隔显示。999 不分组，1,000 起按
    /// 固定三位分组 —— 输入、输出、缓存、计数共用同一规则。
    func testThousandsUseFixedThreeDigitCommaGrouping() {
        XCTAssertEqual(MetricsFormat.tokenText(999), "999")
        XCTAssertEqual(MetricsFormat.tokenText(1_000), "1,000")
        XCTAssertEqual(MetricsFormat.tokenText(1_200), "1,200")
        XCTAssertEqual(MetricsFormat.tokenText(100_000), "100,000")
        XCTAssertEqual(MetricsFormat.tokenText(1_234_567), "1,234,567")
        XCTAssertEqual(MetricsFormat.countText(1_234_567), "1,234,567")
    }

    /// 分组是固定行为：不随系统 locale 变化（de_DE 的 "1.200"、
    /// hi_IN 的 "12,34,567" 都不允许）。分组按算术计算，进程 locale
    /// 翻转后输出必须保持不变。
    func testGroupingSurvivesSystemLocaleChanges() {
        let savedLANG = ProcessInfo.processInfo.environment["LANG"]
        let savedLC = ProcessInfo.processInfo.environment["LC_ALL"]
        defer {
            if let savedLANG { setenv("LANG", savedLANG, 1) } else { unsetenv("LANG") }
            if let savedLC { setenv("LC_ALL", savedLC, 1) } else { unsetenv("LC_ALL") }
        }
        for locale in ["de_DE.UTF-8", "hi_IN.UTF-8", "fr_FR.UTF-8", "zh_CN.UTF-8"] {
            setenv("LANG", locale, 1)
            setenv("LC_ALL", locale, 1)
            setlocale(LC_ALL, locale)   // best-effort; output must not care
            XCTAssertEqual(MetricsFormat.tokenText(1_234_567), "1,234,567",
                           "locale \(locale) must not change the grouping")
            XCTAssertEqual(MetricsFormat.tokenText(1_000), "1,000",
                           "locale \(locale) must not change the grouping")
        }
    }

    func testProgressIsFractionOfCurrentPlanNotPercentage() {
        let progress = ProgressBlock(completed: 2, total: 7, revisionID: 103, revision: 3)
        XCTAssertEqual(MetricsFormat.progressText(progress), "2/7")
        XCTAssertEqual(MetricsFormat.progressText(nil), "—")
        // G012：进度分子分母也按同一规则分组
        let big = ProgressBlock(completed: 12_345, total: 67_890,
                                revisionID: 103, revision: 3)
        XCTAssertEqual(MetricsFormat.progressText(big), "12,345/67,890")
    }

    func testConsumptionTextCarriesCaliberLabel() {
        let consumption = ConsumptionBlock(
            caliber: "orx_recorded",
            tokens: TokenSums(inputTokens: 1_000, outputTokens: 400, cachedTokens: nil),
            observedTotal: 1_400,
            missingFields: ["input_tokens": 0, "output_tokens": 0, "cached_input": 0],
            accuracy: nil)
        XCTAssertEqual(MetricsFormat.consumptionText(consumption),
                       "已观测 token 1,400（ORX 已记录）")
        XCTAssertEqual(MetricsFormat.consumptionText(nil), "已观测 token —")
    }

    func testCoverageAndMissingTextsAreHonest() {
        XCTAssertEqual(MetricsFormat.coverageText(
            CoverageBlock(observed: 3, attempts: 5, missingRecorded: 1, unassessed: 1)),
                       "已观测 3/5 次尝试")
        XCTAssertEqual(MetricsFormat.coverageText(nil), "已观测 —/— 次尝试")
        // G012：覆盖率的分子分母同样分组
        XCTAssertEqual(MetricsFormat.coverageText(
            CoverageBlock(observed: 1_234, attempts: 5_678, missingRecorded: 0, unassessed: 0)),
                       "已观测 1,234/5,678 次尝试")
        XCTAssertEqual(MetricsFormat.missingText(["input_tokens": 0, "output_tokens": 2,
                                                  "cached_input": 3]),
                       "cached_input 缺 3 行，output_tokens 缺 2 行")
        // G012：缺失行数是计数，也走统一格式器
        XCTAssertEqual(MetricsFormat.missingText(["input_tokens": 2_000]),
                       "input_tokens 缺 2,000 行")
        XCTAssertEqual(MetricsFormat.missingText(["input_tokens": 0]), "无缺失记录")
        XCTAssertEqual(MetricsFormat.missingText(nil), "字段缺失 —")
    }

    /// G012 诚实性修复：partial 的标准化部分和里 `rowsMissing` 为 nil 是
    /// 未知，不允许补零显示"缺 0 行"；已知计数走统一千位分组，显式 0
    /// 保留，非部分和不追加后缀。
    func testPartialNormalizedSuffixKeepsUnknownRowsMissingUnknown() {
        let unknown = NormalizedProcessing(version: 1, value: 1_000, partial: true,
                                           rowsDerived: nil, rowsMissing: nil,
                                           cacheSemantics: nil, note: nil)
        XCTAssertEqual(MetricsFormat.partialSuffix(unknown), "（部分和，缺 — 行）")
        let known = NormalizedProcessing(version: 1, value: 1_000, partial: true,
                                         rowsDerived: 38, rowsMissing: 1_200,
                                         cacheSemantics: nil, note: nil)
        XCTAssertEqual(MetricsFormat.partialSuffix(known), "（部分和，缺 1,200 行）")
        let explicitZero = NormalizedProcessing(version: 1, value: 1_000, partial: true,
                                                rowsDerived: 38, rowsMissing: 0,
                                                cacheSemantics: nil, note: nil)
        XCTAssertEqual(MetricsFormat.partialSuffix(explicitZero), "（部分和，缺 0 行）")
        let complete = NormalizedProcessing(version: 1, value: 1_000, partial: false,
                                            rowsDerived: 39, rowsMissing: nil,
                                            cacheSemantics: nil, note: nil)
        XCTAssertEqual(MetricsFormat.partialSuffix(complete), "")
    }

    func testTwoCalibersRenderAsSeparateRowsNeverAMergedTotal() {
        let calibers = UsageCalibers(recordedTotal: 1_000, nativePreviewTotal: 40)
        XCTAssertEqual(calibers.rows.count, 2)
        XCTAssertEqual(calibers.rows.map { $0.label }, ["ORX 已记录", "原生会话预览"])
        XCTAssertEqual(calibers.rows.map { $0.text }, ["1,000", "40"])
        // The two numbers may describe the same tokens — no row combines them.
        XCTAssertFalse(calibers.rows.map { $0.text }.contains("1,040"))
    }

    func testUnknownCaliberFieldStaysDash() {
        let calibers = UsageCalibers(recordedTotal: nil, nativePreviewTotal: nil)
        XCTAssertEqual(calibers.rows.map { $0.text }, ["—", "—"])
    }
}
