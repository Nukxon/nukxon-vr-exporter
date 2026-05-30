# Nukxon VR Exporter

**Showcase your projects, interactively.** Render walkable VR cubemap tours of your Blender scenes and export them as `.nukxon` packages — ready for the [Nukxon](https://nukxon.com) platform, where clients walk through the space you imagined, in any browser.

Built for archviz studios and independent designers who want to ship walkable VR tours of a design without writing a single line of code.

---

## What it does

- **Click-to-place VR cameras** with snap-to-row/column/intersection guides
- **Renders 6-face cubemap panoramas** per camera (1024² or 2048²) using your scene's render engine
- **Exports the scene mesh** as Draco-compressed Y-up glTF
- **Generates an orthographic floor plan** for the viewer minimap, with a live preview + framing controls
- **Camera spacing graph** so your coverage reads at a glance (and your starting camera is flagged)
- **Bundles everything** into a single `.nukxon` package — no code required, works with any render engine (Cycles, EEVEE, V-Ray, …)

## Requirements

- Blender **4.2 LTS** or newer

## Installation

**From the Blender Extensions Platform (recommended):**
Edit → Preferences → Get Extensions → search "Nukxon VR Exporter" → Install.

**From source:**
1. Download/clone this repository.
2. In Blender: Edit → Preferences → Add-ons → install the folder (or zip the `__init__.py` + `blender_manifest.toml` and install the zip).

## Quick start

1. Open your scene and find the **Nukxon** panel (View3D → side panel `N` → Nukxon).
2. **Place Cameras** — click on surfaces to drop VR camera markers.
3. (Optional) set a starting camera, teleport points, and project links.
4. Pick your resolution and hit **Export** — you get a `.nukxon` package.
5. Upload it to [nukxon.com](https://nukxon.com) to share a browser-based, walkable tour with your clients.

No account is required to use the exporter.

## Support

Found a bug or have a request? [Open an issue](../../issues).

## License

Licensed under the **GNU General Public License v3.0 or later** (GPL-3.0-or-later). See [LICENSE](LICENSE).

> **Nukxon** and the Nukxon logo are trademarks of **Nukxon, LLC**. The GPL covers this source code; it does not grant rights to use the Nukxon name or logo.
