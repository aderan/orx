# ORX Menu design system

ORX Menu is a macOS tool for quickly spotting execution changes and investigating the run, task, usage, or configuration behind them. The approved desktop design uses a monochrome palette, system typography, compact tables, and a small set of semantic status colors. The 368 pt menu-bar panel is for scanning. The resizable ORX window is for history, investigation, and routing.

## Source of truth

[ORXTheme.swift](Sources/ORXAnalyticsApp/DesignSystem/ORXTheme.swift) defines the runtime tokens. AppKit follows the system by default. Preferences can select light or dark for every ORX window and the quick panel.

| Token family | Use |
| --- | --- |
| `ORXColor` | `canvas` is the window background, `surface` holds primary content, and `inset` distinguishes grouped information. `hover` and `selected` identify interaction states. Use `text` and `secondary` for readable emphasis. |
| `ORXTone` | Neutral for routine states, warning for attention or degradation, danger for confirmed failure or destructive actions, success for confirmed completion. Pair color with the status text. |
| `ORXType` | `page`, `title` and `section` organize content. Use `body`, `label` and `caption` for controls and metadata. Use `code` for paths, IDs and config values, and the number styles for measurements. |
| `ORXSpace`, `ORXRadius`, `ORXLayout` | Use the shared rhythm, corners and named window, column, and control sizes. Add a semantic dimension when a layout needs a new breakpoint or fixed size. |

The palette resolves each semantic color for both appearances. Keep literal visual values in the token definitions and use the semantic role in the view. Native macOS affordances such as window controls, pickers, text inputs, and alerts retain system behavior.

## Shared components

[ORXComponents.swift](Sources/ORXAnalyticsApp/DesignSystem/ORXComponents.swift) supplies `ORXPageHeader`, `ORXBadge`, `ORXNotice`, `ORXEmptyState`, `ORXField`, `ORXTextInput`, `ORXValueRow`, `ORXRule`, `orxCard`, and `orxSurface`. [ORXButtonStyles.swift](Sources/ORXAnalyticsApp/DesignSystem/ORXButtonStyles.swift) supplies standard, primary, quiet, and destructive actions, plus selectable rows. All actions are real SwiftUI controls with keyboard access. Icon-only controls have readable labels.

Compose these components before adding a pattern. Put a pattern in DesignSystem once it is shared across surfaces. Keep domain concerns such as run identity, query filtering, and routing edits in their owning views or models. Use the normal app entry points and check both appearances, minimum window width, keyboard focus, and the affected loading, empty, and error states.

## Data presentation

- The menu bar shows the ORX icon with an optional attention badge that can be turned off in General settings. The quick panel shows each Run in a single section and keeps Goal control, Run execution, data health, and failure detail readable on their own.
- A Run is identified by project and Run ID. A Task also requires the revision and task ID. History uses engine filtering, order, and paging. Preserve its current results and scroll position when opening a Run.
- Task progress is for the current plan. Run usage includes all revisions. Keep the recorded zero distinct from unknown, make estimated and partial observations visible, and show the observation time when using cached data.
- ORX-recorded usage, native-session preview, and Controller attribution use their own engine-provided fields. Keep these values independent. A shared Controller session carries its cumulative usage and attribution explanation.
- Current model candidates and configuration sources live in routing. Attempt evidence uses the recorded dispatch profile, requested model and effort, and actual model and effort. A missing observation is labeled as such.
- Routing changes use the CLI revision guard and the preview-before-save flow. Use the reported configuration target and revision. The topology and matrix are projections of those configured candidates, and the model inspector displays the fields reported by ORX.
