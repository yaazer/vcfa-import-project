'use strict';
/* VCFA Import Console — design-language skins.
 *
 * A skin is a theme with its own structure, fonts and icons, not just colours
 * (see THEMES in fx.js and html[data-skin] in app.css). The icons here are
 * original drawings in each design language's style; icon() in core.js uses
 * them in place of the default set while the skin is active, and falls back to
 * the default icon for any name a skin does not redraw.
 *
 *   clarity  -- "VMware Modern": the flat, line-icon look of the HTML5 vSphere Client.
 *   classic  -- "vCenter Classic": the colourful, filled icons of the Flex-era Web Client.
 */
(function () {
  const GEAR = 'M12 2.8l1.6 2.3 2.7-.7.6 2.7 2.7.6-.7 2.7 2.3 1.6-2.3 1.6.7 2.7-2.7.6-.6 2.7-2.7-.7L12 21.2l-1.6-2.3-2.7.7-.6-2.7-2.7-.6.7-2.7L2.8 12l2.3-1.6-.7-2.7 2.7-.6.6-2.7 2.7.7z';

  window.SKINS = {
    // Thin, geometric line icons (1.6px), drawn in currentColor.
    clarity: {
      everywhere: true,       // monochrome: fits any slot
      attrs: 'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"',
      icons: {
        overview: '<path d="M3 17.5a9 9 0 0118 0"/><path d="M12 17.5l4.2-5.2"/><circle cx="12" cy="17.5" r="1.3"/><path d="M5.6 11.7l1.2 1M18.4 11.7l-1.2 1M12 8.5v1.6"/>',
        discover: '<rect x="4" y="3" width="16" height="7.5" rx="1"/><rect x="4" y="13.5" width="16" height="7.5" rx="1"/><path d="M7.5 6.75h.01M7.5 17.25h.01M11 6.75h5.5M11 17.25h5.5"/>',
        select: '<rect x="3" y="3.5" width="18" height="13.5" rx="1"/><path d="M8 21h8M12 17v4"/><path d="M6.8 8.2l1.7 4 1.7-4M12.6 12.2V8.2l1.9 2.4 1.9-2.4v4"/>',
        stage: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.8 3 2.8 15 0 18M12 3c-2.8 3-2.8 15 0 18"/>',
        waves: '<rect x="3.5" y="4" width="4.5" height="16" rx=".6"/><rect x="9.75" y="4" width="4.5" height="11" rx=".6"/><rect x="16" y="4" width="4.5" height="7" rx=".6"/>',
        execute: '<circle cx="12" cy="12" r="9"/><path d="M10 8.3v7.4l6-3.7z"/>',
        queue: '<path d="M9.5 6h11M9.5 12h11M9.5 18h11"/><path d="M3.5 6l1.3 1.3L7.2 5M3.5 12l1.3 1.3L7.2 11M3.5 18l1.3 1.3L7.2 17"/>',
        batches: '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.5"/><path d="M4.5 5.5v13c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5v-13"/><path d="M4.5 12c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5"/>',
        triage: '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.4M12 17.2v.1"/>',
        activity: '<path d="M3.6 12a8.4 8.4 0 108.4-8.4A8.3 8.3 0 005.2 7.3"/><path d="M4.6 3.4v4.2h4.2"/><path d="M12 7.6V12l3 2"/>',
        calendar: '<rect x="3" y="5" width="18" height="16" rx="1"/><path d="M3 10h18M8 3v4M16 3v4M7 14h2M11 14h2M15 14h2M7 17.5h2M11 17.5h2"/>',
        gear: '<path d="' + GEAR + '"/><circle cx="12" cy="12" r="3"/>',
        book: '<path d="M5 4.5A1.5 1.5 0 016.5 3H19v15.5H6.5A1.5 1.5 0 005 20z"/><path d="M5 20a1.5 1.5 0 001.5 1.5H19"/><path d="M9 7.5h6"/>',
        help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.3a2.6 2.6 0 015 .8c0 1.8-2.5 2.2-2.5 3.9"/><path d="M12 17.2v.1"/>',
        folder: '<path d="M3 6.5A1.5 1.5 0 014.5 5h4.3l2 2h8.7A1.5 1.5 0 0121 8.5v9A1.5 1.5 0 0119.5 19h-15A1.5 1.5 0 013 17.5z"/>',
        shield: '<path d="M12 3l8 3v5.5c0 5-3.4 8.3-8 9.5-4.6-1.2-8-4.5-8-9.5V6z"/><path d="M8.7 12.2l2.4 2.4 4.3-4.6"/>',
        search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.4 15.4L21 21"/>',
      },
    },
    // Colourful filled icons with outlines, in the Flex-era palette. Only in slots marked
    // `art` (see icon() in core.js); elsewhere the default line icons take the tone's colour.
    classic: {
      attrs: 'stroke-width="1" stroke-linecap="round" stroke-linejoin="round"',
      icons: {
        overview: '<rect x="3" y="3" width="8" height="8" rx="1.2" fill="#7cc242" stroke="#4d8a1f"/><rect x="13" y="3" width="8" height="8" rx="1.2" fill="#f5c342" stroke="#b8860b"/>'
          + '<rect x="3" y="13" width="8" height="8" rx="1.2" fill="#4a90d9" stroke="#2566a8"/><rect x="13" y="13" width="8" height="8" rx="1.2" fill="#e8eef4" stroke="#8aa0b6"/>',
        discover: '<rect x="4" y="3" width="7" height="18" rx="1" fill="#c9d3dd" stroke="#5c6f82"/><rect x="13" y="3" width="7" height="18" rx="1" fill="#dfe6ed" stroke="#5c6f82"/>'
          + '<path d="M6 7h3M6 10h3M15 7h3M15 10h3" stroke="#5c6f82"/><circle cx="7.5" cy="17" r="1.1" fill="#3a9a3a"/><circle cx="16.5" cy="17" r="1.1" fill="#3a9a3a"/>',
        select: '<rect x="2.5" y="4" width="15" height="11" rx="1" fill="#e8f1fb" stroke="#2566a8"/><rect x="4.5" y="6" width="11" height="7" fill="#4a90d9"/><path d="M7 18h6" stroke="#2566a8" stroke-width="1.6"/>'
          + '<rect x="12.5" y="10" width="9" height="11" rx="1" fill="#fff" stroke="#5c6f82"/><path d="M14.5 13h5M14.5 15.5h5M14.5 18h3" stroke="#9fb3c7"/>',
        stage: '<circle cx="12" cy="12" r="9" fill="#4a90d9" stroke="#2566a8"/>'
          + '<path d="M6.8 7.6c1.6.5 2.1 2 1.6 3.3-.5 1.2 1 2.2 2.2 1.8 1.6-.5 2.3 1.3 1.3 2.6-.8 1-.4 2.6.8 3.3" stroke="#7cc242" stroke-width="2.2" fill="none"/>'
          + '<path d="M14.5 4.9c-.5 1.5.5 2.5 2 2.3 1.4-.2 2.2 1 2.4 2" stroke="#7cc242" stroke-width="2" fill="none"/><ellipse cx="9" cy="7" rx="3.5" ry="1.6" fill="#fff" opacity=".25"/>',
        waves: '<rect x="3" y="9" width="5" height="12" fill="#f5c342" stroke="#b8860b"/><rect x="9.5" y="5" width="5" height="16" fill="#7cc242" stroke="#4d8a1f"/><rect x="16" y="12" width="5" height="9" fill="#4a90d9" stroke="#2566a8"/>',
        execute: '<circle cx="12" cy="12" r="9" fill="#3a9a3a" stroke="#256f25"/><path d="M10 8v8l6-4z" fill="#fff"/>',
        queue: '<rect x="5" y="4" width="14" height="17" rx="1.5" fill="#f3d9a4" stroke="#b8860b"/><rect x="8" y="2.5" width="8" height="3.5" rx="1" fill="#c9d3dd" stroke="#5c6f82"/>'
          + '<path d="M8 13l2.5 2.5L16 10" stroke="#3a9a3a" stroke-width="2.2" fill="none"/>',
        batches: '<path d="M5 6v12c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5V6" fill="#c9d3dd" stroke="#5c6f82"/>'
          + '<path d="M5 10c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5M5 14c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5" fill="none" stroke="#5c6f82"/><ellipse cx="12" cy="6" rx="7" ry="2.5" fill="#eef2f6" stroke="#5c6f82"/>',
        triage: '<rect x="2.5" y="4" width="19" height="15" rx="1" fill="#fff" stroke="#5c6f82"/><rect x="2.5" y="4" width="19" height="3.5" fill="#4a90d9" stroke="#2566a8"/>'
          + '<path d="M14 9.8l5 8.6H9z" fill="#e8413c" stroke="#a8201c"/><path d="M14 12.8v2.5M14 16.9v.1" stroke="#fff" stroke-width="1.6"/>',
        activity: '<path d="M6 2.5h9l4 4v15H6z" fill="#fff" stroke="#5c6f82"/><path d="M15 2.5v4h4" fill="#dfe6ed" stroke="#5c6f82"/><path d="M8.5 10h8M8.5 13h8M8.5 16h5" stroke="#4a90d9" stroke-width="1.4"/>',
        calendar: '<rect x="3" y="4.5" width="18" height="16.5" rx="1.5" fill="#fff" stroke="#5c6f82"/><path d="M3 6a1.5 1.5 0 011.5-1.5h15A1.5 1.5 0 0121 6v3.5H3z" fill="#e8413c" stroke="#a8201c"/>'
          + '<path d="M8 3v3.5M16 3v3.5" stroke="#46586a" stroke-width="1.6"/><path d="M7 13h2M11 13h2M15 13h2M7 16.5h2M11 16.5h2" stroke="#4a90d9" stroke-width="1.6"/>',
        gear: '<path d="' + GEAR + '" fill="#b9c4cf" stroke="#5c6f82"/><circle cx="12" cy="12" r="3" fill="#f5c342" stroke="#b8860b"/>',
        book: '<path d="M4 4.5A1.5 1.5 0 015.5 3H19v15H5.5A1.5 1.5 0 004 19.5z" fill="#4a90d9" stroke="#2566a8"/><path d="M4 19.5A1.5 1.5 0 005.5 21H19v-3H5.5" fill="#fff" stroke="#2566a8"/>'
          + '<path d="M8 7.5h7" stroke="#fff" stroke-width="1.6"/>',
        help: '<circle cx="12" cy="12" r="9" fill="#4a90d9" stroke="#fff" stroke-width="1.4"/><path d="M9.6 9.3a2.5 2.5 0 014.9.7c0 1.7-2.5 2.1-2.5 3.8" stroke="#fff" stroke-width="2" fill="none"/><circle cx="12" cy="17" r="1.1" fill="#fff"/>',
        folder: '<path d="M3 6.5A1.5 1.5 0 014.5 5h4.3l2 2h8.7A1.5 1.5 0 0121 8.5v9A1.5 1.5 0 0119.5 19h-15A1.5 1.5 0 013 17.5z" fill="#f5c342" stroke="#b8860b"/><path d="M3 9.5h18" stroke="#b8860b"/>',
        shield: '<path d="M12 3l8 3v5.5c0 5-3.4 8.3-8 9.5-4.6-1.2-8-4.5-8-9.5V6z" fill="#4a90d9" stroke="#2566a8"/><path d="M8.7 12.2l2.4 2.4 4.3-4.6" stroke="#fff" stroke-width="2" fill="none"/>',
      },
    },
  };
})();
