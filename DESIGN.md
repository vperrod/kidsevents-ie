---
version: anydesign-1
name: Small Days
source: https://claude-dev-vperrod.westeurope.cloudapp.azure.com/kidsevents/
captured_at: 2026-09-11
description: |
  Small Days is an Irish family daybook: locally knowledgeable, weather-aware and optimistic without becoming childish. It gives busy parents calm, trustworthy routes from “what can we do?” to a realistic plan.
colors:
  primary: "#1B5C63"
  canvas: "#F5F7F2"
  surface: "#FFFFFF"
  text-primary: "#17312D"
  text-muted: "#46615B"
  border: "#9FB2AA"
  accent: "#F1C84B"
  accent-warm: "#E48A78"
  surface-tint: "#DDE8D7"
typography:
  display:
    fontFamily: "Bricolage Grotesque, Arial, sans-serif"
    fontSize: 64px
    fontWeight: 700
    letterSpacing: -0.055em
  heading:
    fontFamily: "Bricolage Grotesque, Arial, sans-serif"
    fontSize: 32px
    fontWeight: 650
  body:
    fontFamily: "Atkinson Hyperlegible, Arial, sans-serif"
    fontSize: 16px
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "Atkinson Hyperlegible, Arial, sans-serif"
    fontSize: 14px
    fontWeight: 700
spacing:
  base: 4px
  scale: [4, 8, 12, 16, 24, 32, 48, 64, 96]
rounded:
  control: 12px
  card: 20px
  pill: 999px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: 12px 18px
  day-card:
    backgroundColor: "{colors.surface}"
    border: "1px solid {colors.border}"
    rounded: "{rounded.card}"
    padding: 24px
  filter-chip:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.text-primary}"
    rounded: "{rounded.pill}"
    padding: 10px 14px
  daymark:
    backgroundColor: "{colors.primary}"
    accentColor: "{colors.accent}"
    rounded: "15px at 48px"
  dayboard:
    backgroundColor: "{colors.canvas}"
    gap: 16px
    rounded: "{rounded.card}"
---

# Design Analysis — Small Days

> Analysis generated with the `anydesign` workflow.
> Analysis emphasis: identity, design system and production reconstruction.

## Source

- **Source type:** live URL plus rendered desktop and mobile captures.
- **Capture method:** HTML/CSS inspection and Playwright screenshots of Today, Find, Holidays, Guides and Saved.
- **Detected limitation:** the live dataset has inconsistent image coverage, so the system must remain distinctive without depending on photography.

## TL;DR

The former interface mixed an acid-colour planning board with legacy editorial layouts. This system replaces that mixture with an Irish daybook: cool daylight neutrals, Atlantic teal, a single gorse highlight, rounded route-like geometry and highly readable parent-facing type.

## 1. Visual identity

### 1.1 Surface description

**Personality:** locally knowledgeable, optimistic, calm, useful, lightly playful.

**Mood:** the confidence of a well-kept pocket guide rather than the excitement of an entertainment feed.

**Information density:** balanced. Planning facts stay visible, while descriptions remain secondary.

**Implicit positioning:** a trusted Irish family utility for time-poor parents, not a children’s brand and not an influencer feed.

**Confidence:** ✅ high.

### 1.2 Brand voice / Atmosphere

Small Days assumes a worthwhile family day does not require a grand itinerary. The interface therefore helps a parent make one sound decision—where, when, cost, age suitability and weather fit—then get on with the day. It should feel informed without lecturing and cheerful without performing childhood.

Ireland appears through climate, landscape and wayfinding rather than shamrocks or tourist clichés. Cool daylight surfaces, Atlantic teal and the occasional gorse-yellow signal make the product feel native to rainy paths, coastal towns and changing plans.

### 1.3 The ONE brand thing

- **The thing:** the Daymark—an Atlantic tile containing a small gorse sun and a winding pale route.
- **Why it carries the brand:** it compresses “small day”, place and possibility into a mark that remains legible at 16px.
- **How everything else supports it:** colour elsewhere is restrained; gorse is reserved for selection, saved state and the sun.
- **Scope:** header, favicon, social avatar, map/location moments and loading states. It is not repeated as card decoration.

*Confidence:* ✅ high.

## 2. Design system

### 2.1 Colours

| Token | Hex | Role | Confidence |
| --- | --- | --- | --- |
| `primary` | `#1B5C63` | Actions, navigation, map | ✅ |
| `canvas` | `#F5F7F2` | App background | ✅ |
| `surface` | `#FFFFFF` | Cards and controls | ✅ |
| `text-primary` | `#17312D` | Headings and body | ✅ |
| `text-muted` | `#46615B` | Supporting information | ✅ |
| `surface-tint` | `#DDE8D7` | Filter and planning groups | ✅ |
| `accent` | `#F1C84B` | Selected, saved and signature sun | ✅ |
| `accent-warm` | `#E48A78` | Editorial highlight only | ✅ |

### 2.2 Typography

`{typography.display}` uses Bricolage Grotesque for warm, compact headings. `{typography.body}` uses Atkinson Hyperlegible for event details, filters and long-form reading. No third typeface is permitted.

### 2.3 Spacing

Use `{spacing.base}` (4px) and its declared scale. Page gutters are 20px mobile, 32px tablet and 56px desktop.

### 2.4 Radii

Controls use `{rounded.control}` (12px), content surfaces use `{rounded.card}` (20px), and removable filters use `{rounded.pill}` (999px). The logo tile uses its own 15/48 construction ratio.

### 2.5 Elevation system

| Level | Treatment | Use |
| --- | --- | --- |
| 0 | No shadow | Canvas, map artwork |
| 1 | `0 1px 2px rgba(23,49,45,.06), 0 8px 24px rgba(23,49,45,.05)` | Cards |
| 2 | `0 16px 48px rgba(23,49,45,.18)` | Detail sheet |

Depth comes from white surfaces on a cool canvas, not from multiple coloured panels.

### 2.6 Borders

Use `{colors.border}` (#9FB2AA) on controls and quiet separators. Focus uses a 3px gorse outer ring plus a dark inner edge.

### 2.7 Accessibility quick-check

Core text/surface combinations pass WCAG AA; see `design-a11y.md`. Gorse and coral always carry dark ink.

## 3. Components inventory

### 3.1 Generic components

#### Button primary

44px minimum height, Atlantic fill, white label, 12px radius, immediate 0.98 press response.

#### Day card

White planning surface, 20px radius, thin border and subtle elevation. It always presents date/location, title, venue, category/cost/age chips and save action in that order.

#### Filter chip

Pill control with visible text and native checkbox behavior. Selected state uses Atlantic fill or gorse highlight plus an explicit label.

### 3.2 Signature components

#### Daymark

The brand glyph described in Section 1.3. It is a functional identity asset, not a decorative pattern.

#### Dayboard

A responsive group composed of one feature plan and supporting cards. The geometry persists across Today, Holidays and Guides, while content changes to match each route.

## 4. Layout and composition

### 4.1 Grid and containers

The app caps at 1320px. Shared header and page gutters align all screens. Desktop uses a 12-column mental model; mobile uses one column with occasional two-up compact controls only when content remains legible.

### 4.2 Composition patterns

Every screen follows: persistent mast → page introduction → contextual tools → content surface → purposeful next step. Page-specific tools may change, but alignment and spacing do not.

### 4.3 Responsive behavior

| Range | Behavior |
| --- | --- |
| `< 520px` | Single-column cards, sticky five-item bottom navigation, full-width filters |
| `520–839px` | Two-column supporting cards where content allows |
| `≥ 840px` | Desktop mast, sidebar filters, multi-column boards |

All touch targets remain at least 44px. Detail content becomes a bottom sheet on phone and a centred panel on desktop.

### 4.4 Image behavior

Photography is optional, documentary and place-led when available. Never use generic smiling-family stock. Cards without images use the same hierarchy and do not show decorative placeholders.

## 5. Reconstruction notes

**Stack:** existing semantic HTML, vanilla CSS and JavaScript. The application is small enough that a framework migration would add risk without improving the experience.

**Quick wins:** replace legacy colour/type tokens, install the Daymark, unify all page headers, and normalize card/filter states.

**Tricky bits:** long imported titles, incomplete price/age data, mobile filter disclosure, and focus management in the detail sheet.

### Confidence map

| Layer | Confidence | Why |
| --- | --- | --- |
| Identity | ✅ | Product purpose and audience are established |
| Colours | ✅ | New semantic system tested for contrast |
| Typography | ✅ | Open-source web fonts selected explicitly |
| Components | ✅ | All existing public routes inventoried |
| Layout | ✅ | Desktop and mobile behavior specified |

## 6. Do’s and Don’ts

### Do

- Reserve `{colors.accent}` (#F1C84B) for the Daymark sun, selection and saved confirmation.
- Keep planning facts visible on every activity card.
- Use the Dayboard geometry across events, holidays and editorial content.
- Use sentence case and direct parent-facing verbs.
- Let neutral white and cool canvas occupy most of every screen.

### Don't

- Don’t use shamrocks, rainbows, childish handwriting or toy-store primaries.
- Don’t return to acid green, beige-plus-coral, monospace metadata or Space Grotesk.
- Don’t colour each card independently; colour signals meaning, not variety.
- Don’t hide source, cost, age or location inside a second click when the data exists.
- Don’t depend on photography to make an incomplete listing feel designed.

## 7. Open questions

- A documentary photography library is not yet available; the UI is designed to work without one.
- The final legal owner and trademark status of the identity should be confirmed before wide commercial rollout.
- Dark mode is intentionally out of scope until the light system has been approved and real users request it.

## 8. Companion files

- `design-tokens.json`
- `design-a11y.md`
- `web/assets/small-days-logo.svg`
- `web/assets/small-days-mark.svg`
- `web/assets/small-days-mark-mono.svg`
