# App icon credits

The app icons follow [brief §8a](../brief.md#8a-app-icons). Each one is an
**AI-generated** macro image (`<app>/source.jpg`) with a hand-drawn monochrome symbol
(`<app>/symbol.svg`) layered on top. The symbols are original to Jesinia and ship
under the project license (AGPL-3.0-or-later).

| App | Image | Generated with | Date |
|---|---|---|---|
| Notes | `notes/source.jpg` | OpenAI Codex CLI 0.144.6, image generation tool (`gpt-5.6-sol`) | 2026-10-16 |
| RSS | `rss/source.jpg` | same | 2026-10-16 |
| Review | `review/source.jpg` | same | 2026-10-16 |
| Issues | `issues/source.jpg` | same | 2026-10-16 |
| Launcher | `launcher/source.jpg` | same | 2026-10-16 |
| Settings | `settings/source.jpg` | same | 2026-10-16 |

## Prompts

Every prompt asked for one 1024×1024 square image and added the same treatment:

> very shallow depth of field so the CENTER of the frame is soft and calm (an icon
> symbol will sit there); natural diffuse light, no harsh highlights; slightly darker
> vignette at the edges; no text, no watermark, no border, photographic and tactile,
> not illustrated.

Each subject was described as follows:

- **Notes:** an extreme macro photograph of an unfurling fern fiddlehead, the tight
  spiral filling the entire frame edge to edge so it reads as texture rather than a
  whole plant; soft sage green (muted green-grey) with gentle tonal variation.
- **RSS:** an extreme macro photograph of honeybee fur dusted with golden pollen grains,
  the fuzzy hairs filling the entire frame edge to edge so it reads as texture rather
  than a whole bee; warm marigold yellow-orange with gentle tonal variation.
- **Review:** an extreme macro photograph of a jewel beetle's wing case (elytra), the
  iridescent pitted shell surface filling the entire frame edge to edge so it reads as
  texture rather than a whole beetle; deep plum and magenta-violet with subtle
  iridescence.
- **Issues:** an extreme macro photograph of blue morpho butterfly wing scales, the
  overlapping shingle-like scales filling the entire frame edge to edge so it reads as
  texture rather than a whole butterfly; cornflower blue with a soft iridescent sheen.
- **Launcher:** an extreme macro photograph of grey-green lichen growing on stone, the
  crusty rosettes filling the entire frame edge to edge so it reads as texture;
  near-neutral pale grey-green, low saturation, calm.
- **Settings:** an extreme macro photograph of a soft grey feather, the parallel barbs
  and barbules filling the entire frame edge to edge so it reads as texture rather than
  a whole feather; neutral warm grey, low saturation, calm.

To regenerate an image, rerun its prompt and update the row above. Exports (192, 512,
maskable, monochrome) are produced from these sources in Task 23.
