# White Pill Studio plan

White Pill Studio is the authoring environment and canonical home of the
Mekanikos visual system. Product repositories consume generated adapters; they
do not define the visual language.

## Direction

```text
design-system/tokens.toml
          │
          ├── browser studio and specimens
          ├── Omarchy theme fragments and QML adapters
          ├── Hunk themes
          └── future Pi, terminal, editor, and macOS adapters
```

The dependency only points downward. Consumer configuration may reveal a
missing semantic concept, but values are resolved in the design system before
being mapped back into consumers.

## Ownership

This repository owns:

- canonical abstract, semantic, and recipe tokens;
- design-system documentation and historical explorations;
- the interactive browser studio;
- adapter generation and drift validation;
- deterministic fixtures and native reference captures.

The dotfiles repository owns the generated files installed by each subsystem.
Those files remain reviewable there, but carry generated-file headers and are
never hand-edited.

## Token model

1. **Foundations** — palette, typography, spacing, radius, border, shadow,
   blur, and motion.
2. **Semantic roles** — canvas, raised surface, primary/secondary/muted text,
   accent, focus, status, selection, and diff colors.
3. **Component recipes** — bar, panel, tooltip, notification, OSD, controls,
   and code review. Recipe names describe UI intent rather than a particular
   application.
4. **Adapters** — deliberately boring mappings from recipes to Omarchy, Hunk,
   and other consumer formats. Adapters generate values, not product behavior.

## Fidelity model

The studio has two complementary forms of truth:

- **Live specimens** are token-driven HTML/CSS reconstructions. They update
  immediately and are the primary tuning loop.
- **Native references** are deterministic screenshots from real consumers.
  They are slower, captured on demand, and used to check font rendering,
  compositor blur, shadows, geometry, and fallback behavior.

Native screenshots verify adapters. They never become token sources.

## Delivery phases

### 1. Canonical studio — in progress

- Move the tokens, generator, docs, and selected explorations here.
- Render shell, controls, foundations, and Hunk specimens from live tokens.
- Edit scalar tokens with immediate light/dark preview.
- Save atomically, regenerate downstream files, and optionally reload Omarchy.
- Add unit tests for token edits and generator discovery.

### 2. Recipes and adapter boundaries

- Separate component recipes from consumer mappings where they are still
  implicit in the generator.
- Add a machine-readable adapter manifest showing every generated target.
- Display token provenance in the inspector: foundation → semantic role →
  recipe → consumer field.
- Add validation for colors, opacity, dimensions, contrast, and unknown keys.

### 3. Native capture harness

- Build fixed-state Omarchy fixtures for the bar, audio panel, notification,
  tooltip, and OSD.
- Build a deterministic Hunk diff fixture.
- Capture light and dark references at known dimensions and scale.
- Show browser/native pairs and an image comparison slider in the studio.
- Record capture metadata: commit, theme, display scale, Qt version, and date.

### 4. Broader adapters

- Add Pi, terminal/editor syntax, and browser adapters one family at a time.
- Add macOS shell chrome only after its semantic mapping is reviewed.
- Keep unsupported consumer behavior documented rather than cloning or
  patching large upstream components.

### 5. Review and release

- Add visual regression baselines for live specimens.
- Make `check` verify generated outputs in every configured consumer repo.
- Produce a concise change report before applying a token revision.
- Tag coherent design-system revisions independently from dotfiles changes.

## Near-term definition of done

The first useful version is complete when a user can open the studio, tune a
token, compare light and dark shell/Hunk specimens, save the canonical TOML,
regenerate dotfiles, and reload the shell without editing a generated file.

