---
name: "SmolOfis experimental console"
description: "Scoped visual system for the read-only administration console"
colors:
  bg: "#f6f5f1"
  surface: "#ffffff"
  surface-soft: "#efeee8"
  ink: "#29372f"
  muted: "#667168"
  line: "#dedfd6"
  green: "#34664b"
  green-hover: "#28533b"
  green-soft: "#e6eee2"
  amber: "#88601c"
  amber-soft: "#f7ecd3"
  red: "#a64136"
  red-soft: "#f7e5df"
  wallpaper-ink: "#293b2d"
  focus: "#316c4c"
  dark-bg: "#181e1b"
  dark-surface: "#212a24"
  dark-surface-soft: "#29332c"
  dark-ink: "#e4eae2"
  dark-muted: "#a9b5a8"
  dark-line: "#3b473d"
  dark-green: "#a3c9a6"
  dark-green-hover: "#bddbbf"
  dark-green-soft: "#2c4232"
  dark-amber: "#e9c37f"
  dark-amber-soft: "#443a27"
  dark-red: "#efa99a"
  dark-red-soft: "#462f2a"
  dark-wallpaper-ink: "#e4eadd"
  dark-focus: "#b2d4ac"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: "34px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-.035em"
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: "19px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-.018em"
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: "15px"
    lineHeight: 1.55
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif"
    fontSize: "12px"
rounded:
  surface: "16px"
  button: "9px"
  navigation: "10px"
  field: "8px"
  theme-option: "11px"
spacing:
  task-gap: "16px"
  task-gap-mobile: "12px"
  section: "32px"
  section-mobile: "28px"
components:
  button-primary:
    backgroundColor: "{colors.green}"
    textColor: "{colors.surface}"
    rounded: "{rounded.button}"
    padding: "10px 16px"
  button-primary-hover:
    backgroundColor: "{colors.green-hover}"
  button-secondary:
    backgroundColor: "{colors.surface-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.button}"
    padding: "10px 16px"
  button-secondary-hover:
    backgroundColor: "{colors.line}"
  task-card:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.surface}"
    padding: "22px 20px 20px"
  navigation-active:
    backgroundColor: "{colors.green-soft}"
    textColor: "{colors.green}"
    rounded: "{rounded.navigation}"
  diagnostics-field:
    backgroundColor: "{colors.bg}"
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    padding: "12px"
---

# Design System: SmolOfis experimental console

## Overview

**Creative North Star: "A quiet office home"**

A quiet office home that reveals the next useful administrative task. Warm ivory, white surfaces, forest text, and muted green actions make a practical operating tool approachable. System sans typography supports readable diagnostics and familiar controls.

This system applies only to the experimental `/console` routes and `src-dashboard/assets/console.css`, with `console.html`, `console-icons.html`, and `console.js`. The legacy `dashboard.html` / `app.css` dark technical world is outside this scope. The console is a loopback-only, read-only preview behind a disabled-by-default flag; People and Shared folders explicitly remain not connected. This document records visual implementation, not production acceptance, authentication, or administration capabilities.

**Key Characteristics:**
- Warm neutral surfaces with forest actions.
- Task discovery followed by responsive full-page tools.
- Text-backed service state and expandable diagnostics.

## Colors

The frontmatter records the light palette and its `dark-` counterparts; `console.css` remains the implementation source.

### Primary

Forest green identifies actions, current navigation, healthy service state, and resource meters. The softer green supplies selected-state backgrounds. The wallpaper action and brand mark deliberately retain their fixed forest/white treatment in both themes.

### Neutral

Ivory is the page canvas; surface is the main container layer; soft surface supports secondary controls and diagnostic metadata. Ink carries primary text, muted carries supporting text, and line separates rows. Dark mode replaces these with deep green neutrals and pale text. Theme follows the OS unless the browser preference selects light or dark.

Amber and red denote waiting/warning and offline/degraded states, with matching soft notice backgrounds. Four task icons use localized sand, lavender, sage, and blue-gray tints; these are destination cues rather than additional action accents. The overview band is an authored CSS sage-to-sand gradient, with a darker counterpart; exact gradients are in the sidecar.

**The State Label Rule.** Health color accompanies readable status text; color alone must not carry service state.

## Typography

One system sans stack serves headings, controls, and body text. This is an operating interface; no downloaded font is required. Frontmatter captures the recurring headline, title, body, and label roles. Headings balance wrapping; paragraphs generally stop at 70ch. Numeric telemetry uses tabular figures.

The page headline reduces to 29px on mobile. Subheadings use 16px/600/1.4; controls use 13px/550/1.4. Contextual overview headings are 29px on desktop and 25px at narrower widths; setup headings use 26px, then 24px on mobile. Supporting copy ranges from 12px to 14px.

## Layout

Desktop uses a fixed 232px sidebar and a main area capped at 1400px, with 48px horizontal padding. Four equal task columns use the frontmatter task gap. The overview's lower region splits 1.15:1 with a 32px gap. Service and settings data remain full-page tools.

At 1250px and below, main padding becomes 32px, tasks become two columns, and the lower overview stacks. At 1000px, the sidebar narrows to 210px, main padding becomes 28px, and settings and setup instructions become single-column.

At 760px and below, a sticky 70px brand header replaces the side rail; five navigation items sit in a fixed bottom bar with safe-area padding. Main padding is 22px 21px with bottom clearance of `100px + env(safe-area-inset-bottom)`. Tasks retain two columns with 12px gaps and 213px minimum height. Health summary uses a 48px icon column and a flexible text column, with the online count below the text. Diagnostic metadata stacks label/value rows; service actions wrap. The supported CSS minimum viewport is 320px.

## Elevation & Depth

Surfaces have no box shadows. Tonal backgrounds, restrained dividers, and the overview gradient establish depth. Task hover lifts by 2px and changes surface tone. Navigation, button colors, task movement, and disclosure rotation transition over 160ms using the shared ease curve. Reduced-motion preferences remove transitions, animations, and task lift.

## Shapes

Major containers share the surface radius. Smaller controls use the button, navigation, field, and theme-option radii in frontmatter. Icon tiles are softly squared: task tiles use 13px corners on desktop and 12px on mobile; standard service icons use 11px. Status dots are circular. Outlined authored SVG icons use rounded caps and joins, typically 21px with 1.7px strokes. No generated raster assets ship; review screenshots are evidence artifacts.

## Components

### Buttons

Primary buttons use forest and surface colors; secondary buttons use soft surface and ink. Both use the shared padding and radius, with 42px minimum height. The overview action, service actions, and setup actions reach 44px on mobile. Hover uses the recorded variants; disabled buttons show 60% opacity and a waiting cursor. Icon buttons are 44px squares with a soft-surface hover.

### Navigation

Desktop links pair outlined icons with 14px labels in minimum 46px rows. Current-page navigation uses green on soft green with heavier text. Mobile links stack icons and 12px short labels in minimum 52px rows. `aria-current` identifies the active page.

### Task cards and containers

Task cards expose a tinted destination icon, title, short explanation, state, and arrow. White/surface containers and line-separated rows organize launcher lists, resource meters, settings, and service diagnostics. Most containers use the surface radius; settings lists use 13px. Setup states explicitly explain unavailable connections rather than imitating working CRUD.

### Status and health summary

Small text statuses use 12px/550 and green, amber, or red as appropriate. The health summary combines a 48px state icon, heading, explanatory copy, and online count; its mobile count occupies a separate row. Connection loss displays an amber notice and last-known data context.

### Service disclosure

Native details/summary rows disclose impact, latest check, response time, and copy/open controls. Summary rows are at least 96px tall. The chevron rotates 90 degrees when open. Diagnostic strings wrap anywhere; mobile details remove the desktop 55px indent.

### Inputs and appearance

The existing text field is a read-only diagnostics copy fallback, with a line border, page background, 8px corners, and 145px minimum height. Appearance is a three-option native radio group rendered as outlined tiles; checked tiles use green border and soft green fill. There is no general editable account or share form in this preview.

All focusable controls retain a 3px focus outline with 4px offset (appearance inputs use 3px offset). Preserve the skip link, labels, native disclosure semantics, polite action feedback, and labeled meters. These are implemented affordances, not a claim of a complete accessibility audit.

## Do's and Don'ts

### Do:
- Do keep these tokens scoped to the experimental console.
- Do pair health colors with readable state labels.
- Do retain the five mobile destinations and safe-area clearance.

### Don't:
- Don't apply this palette to the legacy panel without a separate approved redesign.
- Don't present unconnected People or Shared folders as empty working administration.
- Don't replace full-page tools with movable windows.
