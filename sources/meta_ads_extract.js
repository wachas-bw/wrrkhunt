/* Pool 1 harvester — click-to-WhatsApp advertisers from the Meta Ad Library.
 *
 * WHY A .js FILE AND NOT A .py: the Ad Library returns 403 to plain HTTP clients and
 * its async search endpoint 404s, so this has to run inside a real browser. Playwright
 * MCP is not callable from Python, so the flow is:
 *
 *   1. mcp__playwright__browser_navigate  -> a URL from queryUrl() below
 *   2. mcp__playwright__browser_evaluate  -> paste extract() as the function body
 *   3. append the returned advertisers to wrrkhunt/data/pool1_ctwa_raw.json
 *
 * Per the global CLAUDE.md rule: if you fan this out across parallel agents, give each
 * one a DIFFERENT playwright server (playwright, playwright-2, -3, -4). One server is
 * one Chrome profile, and two clients on the same server collide on the profile lock.
 *
 * No login is required and no ad spend data is touched. Everything read here is what
 * Meta publishes publicly for ad transparency.
 */

// ── query builder ────────────────────────────────────────────────────────────
// Pair the literal token "whatsapp" with a vertical. The quoted token is what makes
// the result set CTWA-dense: a plain vertical search sorts by impressions and returns
// big brands driving to websites (measured: 1/84 WhatsApp hits for "skincare"),
// whereas '"whatsapp" interior design' measured 17/27.
function queryUrl(country, query) {
  const p = new URLSearchParams({
    active_status: 'active',
    ad_type: 'all',
    country: country,          // IN, AE, SA, SG, ID, PH, GB ...
    q: `"whatsapp" ${query}`,
    search_type: 'keyword_unordered',
    media_type: 'all',
  });
  return `https://www.facebook.com/ads/library/?${p}`;
}

// Verticals measured to be CTWA-heavy. Expand rather than replace; yield varies by market.
const VERTICALS = {
  IN: ['interior design', 'modular kitchen', 'study abroad consultant', 'immigration consultant',
       'dental clinic', 'skin clinic', 'coaching classes', 'real estate', 'gym membership',
       'wedding photographer', 'solar rooftop', 'car dealership'],
  AE: ['interior fit out', 'recruitment agency', 'manpower supply', 'real estate',
       'cleaning services', 'car rental', 'medical center', 'business setup'],
  SG: ['renovation', 'tuition centre', 'aesthetic clinic'],
  ID: ['interior', 'klinik', 'properti'],
};

// ── extractor: paste the BODY of this into browser_evaluate ──────────────────
function extract() {
  const CTWA = /whatsapp|send message|message us|chat now|enquire now/i;
  const out = [];
  const markers = Array.from(document.querySelectorAll('*')).filter(e =>
    e.children.length === 0 && /^Library ID:/.test(e.textContent || ''));

  for (const m of markers) {
    // Walk up to the ad card. The card is the first ancestor with real body text;
    // the Library ID node itself sits ~7 levels below it in obfuscated wrappers,
    // so anchor on text length rather than on Meta's generated class names.
    let card = m;
    for (let i = 0; i < 9 && card.parentElement; i++) {
      card = card.parentElement;
      if ((card.innerText || '').length > 250) break;
    }
    const t = card.innerText || '';
    if (!/Library ID:\s*\d+/.test(t)) continue;

    const page = Array.from(card.querySelectorAll('a[href*="facebook.com/"]'))
      .map(a => a.getAttribute('href'))
      .find(h => h && !h.includes('l.facebook.com') && !h.includes('/ads/'));

    // The advertiser's real website is hidden inside Meta's link shim.
    let dest = null;
    const redir = Array.from(card.querySelectorAll('a[href*="l.facebook.com"]'))
      .map(a => a.getAttribute('href'))[0];
    if (redir) {
      const u = (redir.match(/[?&]u=([^&]+)/) || [])[1];
      if (u) { try { dest = decodeURIComponent(u); } catch (e) { dest = u; } }
    }

    out.push({
      name: (t.match(/\n([^\n]{2,60})\nSponsored/) || [])[1] || null,
      page, dest,
      started: (t.match(/Started running on ([^\n]+)/) || [])[1] || null,
      wa: CTWA.test(t) || /whatsapp|wa\.me/i.test(dest || ''),
      body: t.replace(/\s+/g, ' ').slice(0, 220),
    });
  }

  // One row per advertiser, not per ad: a single business often runs the same
  // creative many times and would otherwise dominate the batch.
  const seen = new Set(), uniq = [];
  for (const c of out.filter(x => x.wa && x.name)) {
    if (seen.has(c.name)) continue;
    seen.add(c.name);
    uniq.push(c);
  }
  return { total: out.length, uniqueWaAdvertisers: uniq.length, advertisers: uniq };
}

/* Scroll first if you want more than the ~25-30 cards Meta renders on load:
 *     window.scrollTo(0, document.body.scrollHeight)
 * wait ~2s, repeat 3-4 times, then run extract().
 *
 * Measured yields, 2026-08-11:
 *   IN "skincare"                    ->  1/84  WhatsApp (wrong shape: big D2C, web dest)
 *   IN "whatsapp" interior design    -> 17/27
 *   IN "whatsapp" study abroad       -> 15/23
 *   AE "whatsapp" interior fit out   -> 20/29
 */
