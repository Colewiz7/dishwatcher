# The TigerHub design system, portable

Extracted from a working app so the rules are the ones actually enforced there,
not aspirations. Everything below is platform independent. `tokens.css` is the
drop-in for web; `tokens.json` is the same values for anything else.

The point of the look is that it is **one coherent scheme rendered**, not a set
of colours picked per widget. That is the thing to carry over.

---

## 1. Colour comes from one seed, and comes last

Pick a single brand colour. Expand it with Material 3's algorithm into a full
role set for light and dark. Do not hand pick a second colour.

TigerHub's seed is RIT orange `#F76902`. Note it does not appear anywhere in the
output: the algorithm turns it into `--primary: #8d4e2c` in light. Seeds are
inputs, not swatches.

Generate your own with the Material Theme Builder, or reuse `tokens.css` and
swap the seed later.

**Roles, not colours.** Never write a hex in a component. `--surface`,
`--on-surface`, `--outline`, `--primary`. The whole reason a scheme holds
together is that every surface has a matching "on" colour that is guaranteed to
be readable on it.

**The surface ramp is for nesting.** Page on `--surface`, cards on
`--surface-container-low`, things inside cards on `--surface-container-high`.
Do not use borders to express depth when a step of the ramp will do.

## 2. Status colour is separate, and never travels alone

Status is not part of the categorical palette and never reuses it.

| Token | Means | Camera app |
|---|---|---|
| `--ok` | good, running, open | online, healthy |
| `--warn` | busy, degraded, attention | recording, motion detected |
| `--bad` | stopped, closed, failed | offline, error |

Three hard rules, each learned by getting it wrong:

- **Always ship a text label with the colour.** Never colour alone. A dot that
  is only red is unreadable to a lot of people and invisible in a screenshot.
- **Separate by lightness as well as hue.** Red against green is the fundamental
  colourblindness case and no hue choice fixes it. TigerHub's status colours sit
  deliberately outside a uniform lightness band for exactly this reason.
- **Validate, do not eyeball.** An earlier version harmonised the status colours
  toward the brand hue and collapsed them to **dE 1.0 apart in deuteranopia**.
  They looked fine. They were the same colour.

## 3. Categorical colour, when you need to tell N things apart

Use `--cat-1` through `--cat-4`. Assign in fixed order and never cycle: a fifth
category is not a generated hue, it folds into "Other" or becomes its own view.

These are validated: worst adjacent pair **dE 18.8 in deuteranopia** (light) and
**19.7 in protanopia** (dark). If you change them, re-validate rather than
trusting your eyes.

**Colour follows the entity, never its position.** If a filter removes one
camera, the others must keep their colours.

## 4. Shape

Nothing is rounded at 8 or below. That single rule does most of the work.

```
--radius-card: 30px    outer containers
--radius-inner: 20px   anything inside a card
--radius-small: 14px   small controls
--radius-pill: 999px   chips, buttons, indicators
```

Chips and status indicators are **full pills**, not slightly rounded rectangles.

Dropdowns use a split control: a wide pill holding the value, a separate small
accent pill holding the chevron.

## 5. Type

Rubik, or any variable font with a weight axis. The hierarchy is carried by
**size and weight contrast**, not by colour.

- The key number on a card is **oversized and light** (weight 300). Everything
  else recedes.
- Labels under big numbers are **small, muted, wide tracked**.
- Body text is regular weight at normal tracking. Do not bold body text to make
  it important; make the thing next to it quieter instead.

## 6. Density and hierarchy

**One hero per card.** A card answers one question. Pick the number that
answers it and make that the only large thing.

For a camera app: the card for a camera answers "is it up and is anything
happening", so the hero is the status, not the resolution or the codec.

**A badge only when the status is not the default.** TigerHub learned this the
hard way: thirteen identical green OPEN pills in a column carry no information
and drown out the three rows that have something to say. If most things are
fine, only mark the ones that are not.

**Say it in words where words are clearer.** "until midnight" beats "23:59".
"recording for 2 hours" beats a timestamp.

## 7. Motion

```
--motion-swap-out: 140ms   old value fading out
--motion-swap-in: 180ms    new value fading in
--motion-value: 260ms      a number counting to a new value
--ease: cubic-bezier(0.22, 1, 0.36, 1)
```

- **Only changed values animate.** A refresh that returns the same numbers does
  nothing visible.
- **Never replay an entrance on refresh.** Stagger the first paint of a list;
  never again after that.
- **Status never pulses.** A blinking red dot is not more informative, it is
  just harder to ignore.
- **Honour `prefers-reduced-motion`** by jumping to the final state. Not by
  slowing things down.

## 8. Loading and empty states

- **Paint from cache first.** Never show a cold-start spinner over data you
  already have.
- **Reserve the space.** A skeleton must not change the final layout by more
  than a few pixels, or the page jumps when data lands.
- **An error screen is only ever right when there is genuinely nothing to
  show.** Stale data plus a quiet "updated 5 minutes ago" beats an error.
- **Empty and broken are different.** "No cameras added yet" and "Cannot reach
  the recorder" need different words and different actions.

## 9. Things that will bite you

Each of these cost real debugging time in TigerHub.

- **Test at phone width with real data.** An empty state is the one layout that
  cannot overflow, so testing empty proves nothing.
- **Test at 2x text size.** Every fixed-height row in TigerHub overflowed at
  1.3x. Fixed heights and user text sizes are fundamentally in tension: scale
  the container, or cap the text inside a fixed shape, but decide which.
- **Fixed-size shapes need their text capped.** A number inside a circle cannot
  grow with the system font or it leaves the circle.
- **Contrast against the surface it is actually on**, not against white. A
  1.2:1 fill is invisible whatever the swatch looked like in isolation.
